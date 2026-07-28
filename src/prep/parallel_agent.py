"""
ParallelPrepAgent — DeepSeek KV-cache-warmed prep.

Replaces the unified path's single Pro call (one ~50K-token input, up to
128K-token output, ~20s) with: one Pro call for character_roster + name_map,
one sequential Flash call that finishes warming the cache for the exact
prefix every remaining call will reuse, then 12 Flash calls fired in
parallel — each returning a single small block instead of re-echoing the
whole document.

Pro's own call and every Flash call share one system prompt and a leading
[jp_chapters + bible_context] prefix (block_prompts.build_shared_system_
prompt / build_shared_prefix) — the THEORY being that Pro's call warms
cache for every Flash call afterward, so jp_chapters only gets processed
fresh once per run instead of twice.

That theory is UNCONFIRMED in practice, not proven — flag this honestly
rather than assert it. dev/test_cross_model_cache.py's Test B (a trivial
hand-rolled system prompt) showed a Pro call warming a Flash call's cache
cleanly. Test C, immediately after, ran the exact same cross-model check
through these real builders (real ~2K-token ROLE+LOCALIZATION_POLICY system
prompt, real task-suffix content) and got a clean miss instead — Flash did
NOT hit on Pro's warmed prefix. No documented explanation for the
discrepancy; DeepSeek's docs don't go deep enough into interval-carving
internals to say why a longer system prompt would behave differently.

This is harmless either way — Pro and Flash still each produce correct,
separately-scoped output regardless of whether the cross-model hit lands,
and the Phase 2a warming call before the 12 parallel calls is unchanged and
still required. Worst case this shared-system-prompt restructuring is a
no-op that occasionally saves a fresh jp_chapters pass; it is NOT something
to build cost projections around until re-verified. See
PARALLEL_PREP_GUIDE.md for the base caching mechanics this is built on.

Not wired to run standalone from the CLI — dispatched from
src.prep.agent.run_prep() when config.yaml's prep.parallel.enabled is true.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.common.atomic_io import atomic_write_text
from src.common.config import PIPELINE_ROOT, WORK_DIR, get_config_section
from src.prep.agent import (
    PrepError,
    _BLOCK_NAMES,
    _finalize_and_write,
    _read_jp_chapters,
    _wrap_jp_chapters,
    discover_series_bible,
)
from src.prep.block_assembler import AssemblyError, assemble_context_xml, validate_cross_block_consistency
from src.prep.block_prompts import (
    FLASH_BLOCKS,
    PRO_BLOCKS,
    build_flash_common_prefix,
    build_flash_user_message,
    build_pro_user_message,
    build_shared_prefix,
    build_shared_system_prompt,
)
from src.prep.block_assembler import parse_fragment

logger = logging.getLogger(__name__)


class ParallelPrepError(RuntimeError):
    """Raised when the cache-warmed parallel prep path cannot proceed."""


# ══════════════════════════════════════════════════════════════════════════
# Low-level call — deliberately does NOT reuse agent.py's os.environ dance.
# That pattern mutates process-global state, which is fine for the unified
# path's single call but a race condition the moment 12 of these run
# concurrently under ThreadPoolExecutor. Passing api_key explicitly and then
# force-nulling the instance's auth_token sidesteps both the env race and
# the stray-Bearer-token 401 the original pattern was written to avoid.
# ══════════════════════════════════════════════════════════════════════════

def _build_client(base_url: str, api_key: str, timeout_seconds: float):
    import anthropic as _anthropic_mod

    client = _anthropic_mod.Anthropic(
        api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=0,
    )
    # anthropic.Anthropic.__init__ only skips ANTHROPIC_AUTH_TOKEN env lookup
    # when auth_token is left as the sentinel None at construction time; a
    # per-instance overwrite afterward is the only thread-safe way to force
    # it off without touching os.environ.
    client.auth_token = None
    return client


def _call_deepseek(
    *,
    model: str,
    system: str,
    user_message: str,
    max_output_tokens: int,
    thinking_budget: int,
    effort: str,
    timeout_seconds: float,
    base_url: str,
    api_key: str,
    volume_id: str,
    call_label: str,
) -> Tuple[str, Dict[str, int]]:
    client = _build_client(base_url, api_key, timeout_seconds)
    kwargs: Dict[str, Any] = dict(
        model=model,
        max_tokens=max_output_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
        output_config={"effort": effort},
    )
    if thinking_budget > 0:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    response = client.messages.create(**kwargs)

    text_parts: List[str] = []
    thinking_parts: List[str] = []
    for block in getattr(response, "content", []):
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")

    # DeepSeek's own docs name these prompt_cache_hit_tokens/prompt_cache_miss_tokens,
    # but that's the native-endpoint schema. Calling through the Anthropic-compatible
    # /anthropic endpoint (this client's base_url), the SDK reports cache stats under
    # Anthropic's own Usage field names instead: cache_read_input_tokens (hit) and
    # cache_creation_input_tokens (miss / tokens just written to cache). Confirmed by
    # hitting the live endpoint directly and inspecting the raw Usage object — see
    # dev/test_cross_model_cache.py's readout.
    #
    # cache_creation_input_tokens is Anthropic's OWN explicit-cache_control field —
    # DeepSeek's automatic caching never populates it (observed 0 across every live
    # call, hit or miss, in the probe run). The real "did this call pay for tokens
    # instead of reading them from cache" signal is usage.input_tokens: the count of
    # tokens actually processed fresh, separate from cache_read_input_tokens. Using
    # cache_creation_input_tokens as the miss denominator (the original mistake here)
    # makes the ratio meaningless — it's always hit/(hit+0), i.e. either 0% or 100%
    # depending on whether ANY hit occurred, never a real proportion.
    usage = getattr(response, "usage", None)
    cache_stats = {
        "cache_hit_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_miss_tokens": int(getattr(usage, "input_tokens", 0) or 0),
    } if usage is not None else {"cache_hit_tokens": 0, "cache_miss_tokens": 0}

    from src.common.token_telemetry import count_tokens, log_call
    output_tokens_billed = count_tokens("".join(text_parts) + "".join(thinking_parts), model)
    log_call(
        phase="prep",
        volume_id=volume_id,
        call_label=call_label,
        model=model,
        cache_hit_tokens=cache_stats["cache_hit_tokens"],
        fresh_tokens=cache_stats["cache_miss_tokens"],
        output_tokens=output_tokens_billed,
    )

    return "".join(text_parts).strip(), cache_stats


# ══════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════

def run_parallel_prep(volume_id: str, series_id: Optional[str] = None) -> Dict[str, Any]:
    work_dir = WORK_DIR / volume_id
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise PrepError(f"No context.xml at {context_path} — run extract first.")

    existing_context_xml = context_path.read_text(encoding="utf-8")
    chapters = _read_jp_chapters(work_dir)

    prep_cfg = get_config_section("prep")
    parallel_cfg = prep_cfg.get("parallel", {}) or {}

    bible_dir = Path(prep_cfg.get("bible_dir", "bibles/"))
    if not bible_dir.is_absolute():
        bible_dir = PIPELINE_ROOT / bible_dir
    bible_match = discover_series_bible(existing_context_xml, bible_dir, series_id)
    resolved_series_id = bible_match[0] if bible_match else series_id
    bible_block = ""
    if bible_match:
        _, bible_data = bible_match
        bible_block = (
            "<bible_context>\n" + json.dumps(bible_data, ensure_ascii=False, indent=2)
            + "\n</bible_context>"
        )

    jp_chapters_block = _wrap_jp_chapters(chapters)
    shared_prefix = build_shared_prefix(jp_chapters_block, bible_block)
    system_shared = build_shared_system_prompt()  # identical for Pro and every Flash call

    api_key_env = str(prep_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise PrepError(f"{api_key_env} not set — cannot run prep.")
    base_url = str(prep_cfg.get("endpoint", "https://api.deepseek.com/anthropic")).rstrip("/")
    timeout_seconds = float(prep_cfg.get("http_timeout_seconds", 900))

    pro_model = str(prep_cfg.get("model", "deepseek-v4-pro"))
    pro_thinking = int(prep_cfg.get("thinking_budget", 48000))
    pro_max_tokens = int(prep_cfg.get("max_output_tokens", 128000))
    pro_effort = str(prep_cfg.get("effort", "max"))

    flash_model = str(parallel_cfg.get("flash_model", "deepseek-v4-flash"))
    flash_thinking = int(parallel_cfg.get("flash_thinking_budget", 8000))
    flash_max_tokens = int(parallel_cfg.get("flash_max_output_tokens", 32000))
    flash_effort = str(parallel_cfg.get("flash_effort", "max"))
    warming_block = str(parallel_cfg.get("warming_block", "voice_fingerprints"))
    if warming_block not in FLASH_BLOCKS:
        raise ParallelPrepError(
            f"prep.parallel.warming_block '{warming_block}' is not one of {FLASH_BLOCKS}"
        )
    max_workers = int(parallel_cfg.get("max_concurrent_flash_calls", 12))

    cache_log: List[Dict[str, Any]] = []

    # ── Phase 1: Pro root extraction ────────────────────────────────────────
    logger.info(
        "[PREP:parallel] %s — Phase 1: Pro root extraction (%d chapters, sequel=%s)",
        volume_id, len(chapters), bool(bible_match),
    )
    pro_raw, pro_cache = _call_deepseek(
        model=pro_model,
        system=system_shared,
        user_message=build_pro_user_message(shared_prefix),
        max_output_tokens=pro_max_tokens,
        thinking_budget=pro_thinking,
        effort=pro_effort,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
        api_key=api_key,
        volume_id=volume_id,
        call_label="phase1_pro_root",
    )
    cache_log.append({"call": "phase1_pro_root", **pro_cache})

    try:
        pro_fragments = parse_fragment(pro_raw, PRO_BLOCKS)
    except AssemblyError as exc:
        raise ParallelPrepError(f"Phase 1 (Pro root extraction) failed: {exc}") from exc
    injected_roster_xml = ET.tostring(pro_fragments["character_roster"], encoding="unicode")

    # ── Phase 2a: Flash cache-warming (sequential, exactly one call) ────────
    # system_shared + shared_prefix already hit off Phase 1's own call (see
    # module docstring) — this call only pays fresh for the trailing
    # injected_character_roster + its own block task.
    common_prefix = build_flash_common_prefix(shared_prefix, injected_roster_xml)

    logger.info("[PREP:parallel] %s — Phase 2a: Flash cache-warming call (%s)", volume_id, warming_block)
    warm_raw, warm_cache = _call_deepseek(
        model=flash_model,
        system=system_shared,
        user_message=build_flash_user_message(common_prefix, warming_block),
        max_output_tokens=flash_max_tokens,
        thinking_budget=flash_thinking,
        effort=flash_effort,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
        api_key=api_key,
        volume_id=volume_id,
        call_label=f"phase2_warm_{warming_block}",
    )
    cache_log.append({"call": f"phase2_warm_{warming_block}", **warm_cache})

    flash_fragments_raw: Dict[str, str] = {warming_block: warm_raw}
    remaining_blocks = [b for b in FLASH_BLOCKS if b != warming_block]

    # ── Phase 2b: remaining Flash blocks in parallel ────────────────────────
    logger.info("[PREP:parallel] %s — Phase 2b: %d Flash calls in parallel", volume_id, len(remaining_blocks))

    def _fill_block(block_name: str) -> Tuple[str, str, Dict[str, int]]:
        raw, cache_stats = _call_deepseek(
            model=flash_model,
            system=system_shared,
            user_message=build_flash_user_message(common_prefix, block_name),
            max_output_tokens=flash_max_tokens,
            thinking_budget=flash_thinking,
            effort=flash_effort,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
            api_key=api_key,
            volume_id=volume_id,
            call_label=f"phase2_parallel_{block_name}",
        )
        return block_name, raw, cache_stats

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fill_block, name) for name in remaining_blocks]
        for future in concurrent.futures.as_completed(futures):
            block_name, raw, cache_stats = future.result()
            flash_fragments_raw[block_name] = raw
            cache_log.append({"call": f"phase2_parallel_{block_name}", **cache_stats})

    # Debug dump — kept only if assembly below fails, mirroring the unified
    # path's "raw response saved for inspection" behavior on malformed XML.
    tmp_dir = work_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    atomic_write_text(tmp_dir / "phase1_pro_root.xml", pro_raw)
    for block_name, raw in flash_fragments_raw.items():
        atomic_write_text(tmp_dir / f"{block_name}.xml", raw)

    # ── Phase 3: Assembly ────────────────────────────────────────────────────
    logger.info(
        "[PREP:parallel] %s — Phase 3: assembling %d blocks",
        volume_id, len(PRO_BLOCKS) + len(flash_fragments_raw),
    )
    try:
        root = assemble_context_xml(existing_context_xml, pro_raw, flash_fragments_raw)
    except AssemblyError as exc:
        raise PrepError(
            f"{exc} — raw model responses kept for inspection under {tmp_dir}"
        ) from exc

    validation_warnings = validate_cross_block_consistency(root)
    for warning in validation_warnings:
        logger.warning("[PREP:parallel] %s — cross-block check: %s", volume_id, warning)

    receipt = _finalize_and_write(
        work_dir, context_path, root,
        resolved_series_id=resolved_series_id, chapters=chapters,
        bible_match=bible_match, generated_by="deepseek_prep_parallel",
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)  # assembly succeeded — drop the debug dump

    total_hit = sum(c.get("cache_hit_tokens", 0) for c in cache_log)
    total_miss = sum(c.get("cache_miss_tokens", 0) for c in cache_log)
    receipt["parallel_prep"] = {
        "flash_model": flash_model,
        "cache_hit_tokens_total": total_hit,
        "cache_miss_tokens_total": total_miss,
        "cache_hit_ratio": round(total_hit / (total_hit + total_miss), 4) if (total_hit + total_miss) else 0.0,
        "calls": cache_log,
        "validation_warnings": validation_warnings,
    }
    logger.info(
        "[PREP:parallel] %s — done. %d/%d blocks populated, cache hit ratio %.1f%%.",
        volume_id, len(receipt["blocks_populated"]), len(_BLOCK_NAMES),
        receipt["parallel_prep"]["cache_hit_ratio"] * 100,
    )
    return receipt
