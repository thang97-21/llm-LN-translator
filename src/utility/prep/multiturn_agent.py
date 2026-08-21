"""
MultiTurnPrepAgent — DeepSeek prep over one persisted, sequential conversation.

Third prep path, alongside agent.py's unified single-call path and
parallel_agent.py's cache-warmed concurrent fan-out. Where the parallel path
fires ~13 Flash calls concurrently (each blind to the others' output — see
block_assembler.validate_cross_block_consistency, which exists precisely
because that path can drift), this path fires one call PER BLOCK, in strict
order, inside a single DeepSeekConversationManager conversation — the exact
mechanism src/Deepseek/translator/deepseek_conversation.py already uses for
chapter-by-chapter translation. Because every turn is sequential, every later
turn can see every earlier turn's actual committed output in its own
conversation history, not just a shared static prefix — that ordering is
what buys consistency the parallel path can only check for after the fact.

Each turn returns its block as a small JSON node envelope (see
json_xml_node.py), persisted to work/<vol>/.context/multiturn/<block>.json —
that file IS the "JSON artifact" referenced in config.yaml's prep.multi_turn
docs, and doubles as DeepSeekConversationManager's canonical output_path so a
crash mid-run resumes cleanly. Only after every turn succeeds does the
deterministic assembly step (assemble_from_multiturn_artifacts) convert and
wire every artifact into context.xml — it never talks to a model.

Not wired to run standalone from the CLI — dispatched from
src.utility.prep.agent.run_prep() when config.yaml's prep.multi_turn.enabled is true.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.config import PIPELINE_ROOT, WORK_DIR, get_config_section
from src.utility.prep.agent import (
    PrepError,
    _finalize_and_write,
    _read_jp_chapters,
    _wrap_jp_chapters,
    discover_series_bible,
)
from src.utility.prep.block_assembler import _replace_block, validate_cross_block_consistency
from src.utility.prep.block_prompts import (
    MULTITURN_BLOCK_ORDER,
    _PRO_TASK_NOTE,
    build_multiturn_system_prompt,
    build_multiturn_task_suffix,
)
from src.utility.prep.json_xml_node import JsonNodeError, extract_json_node, node_to_element
from src.utility.prep.parallel_agent import _build_client
from src.utility.prep.web_search_chain import MetadataSearchChain
from src.Deepseek.translator.deepseek_conversation import DeepSeekConversationManager

logger = logging.getLogger(__name__)

# Section names DeepSeekConversationManager._validate_checkpoint requires —
# hardcoded there (deepseek_conversation.py's _CHECKPOINT_REQUIRED_SECTIONS),
# not configurable, so a prep checkpoint has to use these exact names even
# though most describe translation-specific concepts this run doesn't have.
_CHECKPOINT_SECTION_NAMES = (
    "chapter_coverage", "plot_state", "relationship_state", "unresolved_threads",
    "names_and_terms", "voice_and_pov", "callbacks", "translation_decisions",
    "anchor_excerpts",
)


class MultiTurnPrepError(RuntimeError):
    """Raised when the multi-turn conversation prep path cannot proceed."""


# ══════════════════════════════════════════════════════════════════════════
# Low-level call — takes a prebuilt message list (the conversation manager
# owns history construction) instead of parallel_agent._call_deepseek's
# single user_message. Reuses parallel_agent._build_client: sequential calls
# have no thread-safety need of their own, but the explicit-api-key pattern
# there is just as correct here and avoids a second copy of the env-var dance.
# ══════════════════════════════════════════════════════════════════════════

def _call_deepseek_turn(
    *,
    model: str,
    system: Any,
    messages: List[Dict[str, Any]],
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
        messages=messages,
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

    # Same Anthropic-format usage schema parallel_agent._call_deepseek reads —
    # see that function's docstring comment for why cache_creation_input_tokens
    # is not the miss denominator.
    usage = getattr(response, "usage", None)
    cache_stats = {
        "cache_hit_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_miss_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    } if usage is not None else {"cache_hit_tokens": 0, "cache_miss_tokens": 0, "output_tokens": 0}

    from src.Deepseek.common.token_telemetry import count_tokens, log_call
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
# Compaction-ladder checkpoint callback — required by
# DeepSeekConversationManager.prepare_turn(), but structurally unreachable
# under config.yaml's default prep.multi_turn.conversation.
# recent_verbatim_chapters (15, i.e. >= the turn count MULTITURN_BLOCK_ORDER
# ever produces): the ladder's paid rung only fires once turns exceed that
# count. Implemented for real anyway rather than stubbed, so a user who tunes
# that value down gets correct behavior instead of a crash.
# ══════════════════════════════════════════════════════════════════════════

def _generate_prep_checkpoint(
    *,
    model: str,
    thinking_budget: int,
    effort: str,
    timeout_seconds: float,
    base_url: str,
    api_key: str,
    volume_id: str,
    previous_checkpoint: Optional[str],
    evicted_turns: List[Dict[str, Any]],
    required_chapter_ids: List[str],
    max_output_tokens: int,
    validation_errors: List[str],
) -> Dict[str, Any]:
    transcript = [
        {"block": turn.get("chapter_id"), "output": turn.get("assistant_response")}
        for turn in evicted_turns
    ]
    correction = ""
    if validation_errors:
        correction = (
            "\nThe prior attempt failed validation for: "
            + "; ".join(validation_errors)
            + ". Correct every defect."
        )
    checkpoint_prompt = (
        "Compress the accepted prep-block transcript into one dense, loss-minimizing "
        "continuity checkpoint for the REMAINING block turns in this prep run. Return XML "
        "only — no preamble, no commentary. Use root <deepseek_volume_checkpoint> and "
        "exactly these named sections: " + ", ".join(_CHECKPOINT_SECTION_NAMES) + ". Every "
        "section: <section name=\"...\"><![CDATA[...]]></section>. Mention every required "
        f"block name verbatim: {', '.join(required_chapter_ids)}. This is a PREP run, not a "
        "translation — plot_state/relationship_state/unresolved_threads/callbacks/"
        "translation_decisions rarely apply here; write NONE for any that genuinely have "
        "nothing to carry rather than inventing content. chapter_coverage: list the block "
        "names compressed here. names_and_terms: the exact canonical_name/en spellings "
        "already locked so later blocks never re-derive or rename them. voice_and_pov: "
        "archetype/register/contraction-rate facts already locked per character. "
        "anchor_excerpts: verbatim_anchors already locked, copied byte-for-byte."
        f"{correction}\n\n"
        f"<previous_checkpoint>{previous_checkpoint or ''}</previous_checkpoint>\n"
        "<accepted_turns_json>\n"
        f"{json.dumps(transcript, ensure_ascii=False, sort_keys=True)}\n"
        "</accepted_turns_json>"
    )
    raw, cache_stats = _call_deepseek_turn(
        model=model,
        system="You maintain deterministic prep continuity. Output only the requested XML checkpoint.",
        messages=[{"role": "user", "content": checkpoint_prompt}],
        max_output_tokens=max_output_tokens,
        thinking_budget=min(thinking_budget, max(1024, int(max_output_tokens) // 2)),
        effort=effort,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
        api_key=api_key,
        volume_id=volume_id,
        call_label="multiturn_checkpoint",
    )
    return {
        "content": raw,
        "finish_reason": "end_turn",
        "input_tokens": 0,
        "output_tokens": cache_stats.get("output_tokens", 0),
        "cache_read_tokens": cache_stats.get("cache_hit_tokens", 0),
        "cache_miss_tokens": cache_stats.get("cache_miss_tokens", 0),
    }


def _make_checkpoint_callback(**bound: Any) -> Callable[..., Dict[str, Any]]:
    def _callback(**turn_kwargs: Any) -> Dict[str, Any]:
        return _generate_prep_checkpoint(**bound, **turn_kwargs)
    return _callback


# ══════════════════════════════════════════════════════════════════════════
# Assembly — deterministic, never talks to a model. Reads artifacts from the
# in-memory dict the run just produced, OR (artifacts=None) from disk, so a
# run that finished every turn but crashed before this step can be re-wired
# without re-spending a single API call.
# ══════════════════════════════════════════════════════════════════════════

def _artifacts_dir(work_dir: Path) -> Path:
    return work_dir / ".context" / "multiturn"


def assemble_from_multiturn_artifacts(
    work_dir: Path,
    existing_context_xml: str,
    artifacts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[ET.Element, List[str]]:
    if artifacts is None:
        artifacts = {}
        for path in sorted(_artifacts_dir(work_dir).glob("*.json")):
            try:
                artifacts[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise MultiTurnPrepError(f"could not read artifact {path}: {exc}") from exc

    missing = [name for name in MULTITURN_BLOCK_ORDER if name not in artifacts]
    if missing:
        raise MultiTurnPrepError(
            f"missing block artifact(s) {missing} — cannot assemble context.xml. "
            f"Raw artifacts kept under {_artifacts_dir(work_dir)} for inspection."
        )

    root = ET.fromstring(existing_context_xml)
    for block_name in MULTITURN_BLOCK_ORDER:
        try:
            element = node_to_element(artifacts[block_name])
        except JsonNodeError as exc:
            raise MultiTurnPrepError(
                f"block '{block_name}' artifact is malformed: {exc}. Raw artifacts kept "
                f"under {_artifacts_dir(work_dir)} for inspection."
            ) from exc
        _replace_block(root, block_name, element)

    validation_warnings = validate_cross_block_consistency(root)
    for warning in validation_warnings:
        logger.warning("[PREP:multiturn] cross-block check: %s", warning)

    return root, validation_warnings


def _extract_opf_search_fields(context_xml_text: str) -> Tuple[str, str]:
    """Read only the title and author needed for one metadata search."""
    try:
        root = ET.fromstring(context_xml_text)
        opf = root.find("opf_metadata")
        data = json.loads(opf.text or "{}") if opf is not None else {}
    except (ET.ParseError, json.JSONDecodeError, TypeError):
        return "", ""
    return (
        str(data.get("dc_title_jp") or data.get("title") or "").strip(),
        str(data.get("author_jp") or data.get("author") or "").strip(),
    )


# ══════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════

def run_multiturn_prep(volume_id: str, series_id: Optional[str] = None) -> Dict[str, Any]:
    work_dir = WORK_DIR / volume_id
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise PrepError(f"No context.xml at {context_path} — run extract first.")

    existing_context_xml = context_path.read_text(encoding="utf-8")
    chapters = _read_jp_chapters(work_dir)

    prep_cfg = get_config_section("prep")
    mt_cfg = prep_cfg.get("multi_turn", {}) or {}

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

    api_key_env = str(prep_cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise PrepError(f"{api_key_env} not set — cannot run prep.")
    base_url = str(prep_cfg.get("endpoint", "https://api.deepseek.com/anthropic")).rstrip("/")
    timeout_seconds = float(prep_cfg.get("http_timeout_seconds", 900))

    model = str(mt_cfg.get("flash_model", "deepseek-v4-flash"))
    thinking_budget = int(mt_cfg.get("thinking_budget", 16000))
    effort = str(mt_cfg.get("effort", "high"))
    max_output_tokens = int(mt_cfg.get("max_output_tokens", 32000))

    cache_monitor_cfg = mt_cfg.get("cache_monitor", {}) or {}
    cache_monitor_enabled = bool(cache_monitor_cfg.get("enabled", True))
    cache_warn_threshold = float(cache_monitor_cfg.get("warn_threshold_cache_hit_ratio", 0.70))

    conversation_cfg = dict(mt_cfg.get("conversation", {}) or {})
    conversation_cfg["conversation_kind"] = "prep"

    title_jp, author_jp = _extract_opf_search_fields(existing_context_xml)
    search_result = MetadataSearchChain(
        prep_cfg.get("web_search", {}) or {}
    ).search(title_jp, author_jp)
    web_search_block = search_result.to_prompt_block()
    atomic_write_json(
        work_dir / ".context" / "prep_web_search.json",
        search_result.to_dict(),
    )

    from src.Deepseek.common.token_telemetry import count_tokens
    manager = DeepSeekConversationManager(
        work_dir=work_dir,
        volume_id=volume_id,
        model=model,
        endpoint=base_url,
        config=conversation_cfg,
        token_counter=lambda text: count_tokens(text, model),
    )

    checkpoint_callback = _make_checkpoint_callback(
        model=model, thinking_budget=thinking_budget, effort=effort,
        timeout_seconds=timeout_seconds, base_url=base_url, api_key=api_key,
        volume_id=volume_id,
    )

    system_prompt = build_multiturn_system_prompt(
        existing_context_xml=existing_context_xml,
        jp_chapters_block=jp_chapters_block,
        bible_block=bible_block,
        web_search_block=web_search_block,
    )
    artifacts_dir = _artifacts_dir(work_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    artifacts: Dict[str, Dict[str, Any]] = {}
    cache_log: List[Dict[str, Any]] = []

    logger.info(
        "[PREP:multiturn] %s — %d sequential turns (sequel=%s)",
        volume_id, len(MULTITURN_BLOCK_ORDER), bool(bible_match),
    )

    for index, block_name in enumerate(MULTITURN_BLOCK_ORDER):
        extra_note = _PRO_TASK_NOTE if block_name == "name_map" else ""
        task_suffix = build_multiturn_task_suffix(block_name, extra_note=extra_note)
        prompt = task_suffix

        logger.info(
            "[PREP:multiturn] %s — turn %d/%d: %s",
            volume_id, index + 1, len(MULTITURN_BLOCK_ORDER), block_name,
        )

        prepared = manager.prepare_turn(
            prompt=prompt,
            system=system_prompt,
            max_output_tokens=max_output_tokens,
            checkpoint_callback=checkpoint_callback,
        )

        raw, cache_stats = _call_deepseek_turn(
            model=model,
            system=prepared["system"],
            messages=prepared["messages"],
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
            effort=effort,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
            api_key=api_key,
            volume_id=volume_id,
            call_label=f"multiturn_{block_name}",
        )
        cache_log.append({"call": f"multiturn_{block_name}", **cache_stats})

        try:
            node = extract_json_node(raw, expected_tag=block_name)
        except JsonNodeError as exc:
            raise MultiTurnPrepError(
                f"turn '{block_name}' returned an invalid JSON artifact: {exc}. Raw "
                f"artifacts for prior turns kept under {artifacts_dir} for inspection."
            ) from exc

        artifact_path = artifacts_dir / f"{block_name}.json"
        atomic_write_text(artifact_path, json.dumps(node, ensure_ascii=False, indent=2))
        artifacts[block_name] = node

        manager.commit_turn(
            chapter_id=block_name,
            user_prompt=prompt,
            assistant_response=raw,
            canonical_output=raw,
            output_path=artifact_path,
            cache_telemetry=cache_stats,
        )

        if cache_monitor_enabled and index > 0:
            denom = cache_stats.get("cache_hit_tokens", 0) + cache_stats.get("cache_miss_tokens", 0)
            if denom > 0:
                ratio = cache_stats["cache_hit_tokens"] / denom
                if ratio < cache_warn_threshold:
                    logger.warning(
                        "[PREP:multiturn] %s — turn '%s' cache hit ratio %.1f%% below "
                        "warn threshold %.0f%% (hit=%s, miss=%s)",
                        volume_id, block_name, ratio * 100, cache_warn_threshold * 100,
                        f"{cache_stats['cache_hit_tokens']:,}", f"{cache_stats['cache_miss_tokens']:,}",
                    )

    logger.info(
        "[PREP:multiturn] %s — all %d turns committed; assembling context.xml",
        volume_id, len(MULTITURN_BLOCK_ORDER),
    )
    root, validation_warnings = assemble_from_multiturn_artifacts(
        work_dir, existing_context_xml, artifacts
    )

    receipt = _finalize_and_write(
        work_dir, context_path, root,
        resolved_series_id=resolved_series_id, chapters=chapters,
        bible_match=bible_match, generated_by="deepseek_prep_multiturn",
    )

    shutil.rmtree(artifacts_dir, ignore_errors=True)  # assembly succeeded — drop the JSON artifacts

    total_hit = sum(c.get("cache_hit_tokens", 0) for c in cache_log)
    total_miss = sum(c.get("cache_miss_tokens", 0) for c in cache_log)
    receipt["multiturn_prep"] = {
        "model": model,
        "turn_count": len(MULTITURN_BLOCK_ORDER),
        "cache_hit_tokens_total": total_hit,
        "cache_miss_tokens_total": total_miss,
        "cache_hit_ratio": round(total_hit / (total_hit + total_miss), 4) if (total_hit + total_miss) else 0.0,
        "calls": cache_log,
        "validation_warnings": validation_warnings,
        "conversation_state_path": str(manager.state_path),
        "web_search": search_result.to_dict(),
    }
    logger.info(
        "[PREP:multiturn] %s — done. %d/%d blocks populated, cache hit ratio %.1f%%.",
        volume_id, len(receipt["blocks_populated"]),
        len(receipt["blocks_populated"]) + len(receipt["blocks_pending"]),
        receipt["multiturn_prep"]["cache_hit_ratio"] * 100,
    )
    return receipt
