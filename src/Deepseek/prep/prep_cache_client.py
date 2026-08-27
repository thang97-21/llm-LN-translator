"""MTLS prep block-fill cache loop — one shared prefix, sixteen deltas.

Fills every context.xml block by sending the SAME byte-identical system
prefix followed by a block-specific user suffix, returning machine-readable
JSON fragments that the assembler concatenates into context.xml. The repeated
prefix rides each provider's automatic prefix caching (MiniMax passive
caching at >=512 input tokens, no API change; DeepSeek official reports cache
reuse in usage).

Provider order (defaults mirror DeepSeek Harness configuration):
- PRIMARY: zhi-api custom provider (settings.yaml `llm-pi-ai.providers.zhi-api`)
    base_url  https://zhi-api.com/v1
    api_key_env ZHI_API_API_KEY
    api_style openai (openai-completions wire)
    model     MiniMax-M3   (contextWindow 1,000,000 / maxTokens 128,000)
    cache:    automatic (passive), no cache_control needed
- FALLBACK: deepseek-official (dsh-llm-deepseek package contract)
    base_url  https://api.deepseek.com
    api_key_env DEEPSEEK_API_KEY
    api_style openai
    model     deepseek-v4-pro
    "An unchanged assembled prefix is eligible for DeepSeek cache reuse,
     which this adapter reports in usage." — dsh-llm-deepseek README

Cache references:
- https://platform.minimax.io/docs/api-reference/text-prompt-caching
- https://platform.minimax.io/docs/api-reference/anthropic-api-compatible-cache

Usage (real):
    python -m src.Deepseek.prep.prep_cache_client \
      --vol-id <vol_id> --work-dir D:/MTLS/work/<vol_id> [--series-id X] \
      [--blocks 3,5,6] [--no-fallback]

Usage (dry-run — no network, verifies prefix byte-identity + composition):
    python -m src.Deepseek.prep.prep_cache_client --dry-run \
      --work-dir D:/MTLS/work/<vol_id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[3]  # D:/MTLS
PREFIX_FILE = REPO / ".dsh" / "prep" / "prefix.md"
LIB = REPO / ".dsh" / "subagents"


def _load_env() -> None:
    """Load D:/MTLS/.env into os.environ WITHOUT overwriting existing values.

    The agent path (mtl.py) loads .env through its config import chain, but a
    direct `python -m src.Deepseek.prep.prep_cache_client` run never imports
    that chain — without this, a direct run would miss ZHI_API_API_KEY and
    silently fall back to DeepSeek. Stdlib only; keys already present win."""
    env_path = REPO / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except OSError:
        pass

# Block roster: block name -> role file (single source = the prompt library).
BLOCKS: list[dict[str, str]] = [
    {"name": "opf_metadata", "role": "blk-01-opf-metadata"},
    {"name": "validation_audit", "role": "blk-02-validation-audit"},
    {"name": "volume_identity", "role": "blk-03-volume-identity"},
    {"name": "world_setting", "role": "blk-04-world-setting"},
    {"name": "character_roster", "role": "blk-05-character-roster"},
    {"name": "name_map", "role": "blk-06-name-map"},
    {"name": "relationship_graph", "role": "blk-07-relationship-graph"},
    {"name": "verbatim_anchors", "role": "blk-08-verbatim-anchors"},
    {"name": "character_attribute_anchors", "role": "blk-09-character-attribute-anchors"},
    {"name": "voice_fingerprints", "role": "blk-10-voice-fingerprints"},
    {"name": "cultural_glossary", "role": "blk-11-cultural-glossary"},
    {"name": "eps_arc_tracker", "role": "blk-12-eps-arc-tracker"},
    {"name": "scene_plans", "role": "blk-13-scene-plans"},
    {"name": "eps_signals", "role": "blk-14-eps-signals"},
    {"name": "illustration_context", "role": "blk-15-illustration-context"},
    {"name": "translation_brief", "role": "blk-16-translation-brief"},
    {"name": "metadata_localization", "role": "blk-17-metadata-localization"},
]

TELEMETRY_KEYS = ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]

# Model capability profiles — sourced from DeepSeek Harness's own registry
# (settings.yaml `llm-pi-ai.providers.zhi-api`) and the dsh-llm-deepseek
# package README. MiniMax-M3: contextWindow 1,000,000 / maxTokens 512,000 —
# exactly as the harness defines it. DeepSeek official: 1,000,000-token
# context, adapter output cap 256,000.
MODEL_PROFILES: dict[str, dict[str, int]] = {
    "MiniMax-M3": {"context_window": 1_000_000, "max_output": 512_000},
    "MiniMax-M3-highspeed": {"context_window": 1_000_000, "max_output": 512_000},
    "deepseek-v4-pro": {"context_window": 1_000_000, "max_output": 256_000},
    "deepseek-v4-flash": {"context_window": 1_000_000, "max_output": 256_000},
}


def build_suffix(block: dict[str, str], vol_id: str, series_id: str, work_dir: Path, web_results: str = "") -> str:
    """Per-block suffix — SELF-CONTAINED. The block schema is embedded in full
    (the raw-API model has no filesystem), and blk-17's Bookwalker results are
    embedded when supplied. The volume source package lives in the cached
    system region, never here."""
    role_path = LIB / f"{block['role']}.md"
    try:
        schema = role_path.read_text(encoding="utf-8")
    except OSError:
        schema = f"(schema file missing: {role_path})"
    lines = [
        f"You are filling block <{block['name']}> for volume {vol_id}.",
        f"Delegation payload: vol_id={vol_id}, series_id={series_id or '(empty — standalone)'}.",
        "The volume source package is in the system context. Fill ONLY your block.",
        "",
        "=== BLOCK SCHEMA (embedded, authoritative) ===",
        schema,
    ]
    if web_results:
        lines += ["", "=== BOOKWALKER SEARCH RESULTS (blk-17) ===", web_results]
    lines += [
        "",
        "Return ONLY the JSON artifact per the shared prefix's output contract.",
    ]
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """CJK-aware token proxy: Japanese/Chinese text is ~1 token per character;
    everything else ~4 chars per token. The guard's 1M ceiling must not be
    understated by an English-only heuristic."""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uff00" <= ch <= "\uffef")
    return cjk + (len(text) - cjk) // 4


def build_source_package(work_dir: Path) -> tuple[str, dict]:
    """The volume source package: JP chapters (sorted), barebone context.xml,
    manifest.json — byte-identical across every block call, so it rides the
    cached system region. Returns (package_text, stats)."""
    sections: list[str] = []
    stats: dict = {"jp_chapters": 0, "jp_chars": 0, "jp_tokens_estimate": 0, "context_xml_chars": 0, "manifest_chars": 0}
    jp_dir = work_dir / "JP"
    if jp_dir.is_dir():
        for chapter in sorted(jp_dir.glob("CHAPTER_*.md")):
            text = chapter.read_text(encoding="utf-8")
            stats["jp_chapters"] += 1
            stats["jp_chars"] += len(text)
            stats["jp_tokens_estimate"] += estimate_tokens(text)
            sections.append(f"--- {chapter.name} ---\n{text}")
    for name, key in (("context.xml", "context_xml_chars"), ("manifest.json", "manifest_chars")):
        path = work_dir / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            stats[key] = len(text)
            sections.append(f"--- {name} ---\n{text}")
    return "\n\n".join(sections), stats


def endpoint(base_url: str, api_style: str) -> str:
    """Wire endpoint per style. openai-completions: <base>/chat/completions;
    anthropic: <base>/v1/messages (both MiniMax and DeepSeek follow this)."""
    base = base_url.rstrip("/")
    return f"{base}/chat/completions" if api_style == "openai" else f"{base}/v1/messages"


def build_body(prefix: str, source_package: str, suffix: str, model: str, max_tokens: int, api_style: str, cache_mode: str, reasoning_split: bool) -> dict:
    """The full system context = global prefix + volume source package; BOTH
    are byte-identical across all block calls and therefore ride the passive
    prefix cache. Only the user suffix varies per block."""
    system_text = f"{prefix}\n\n{source_package}" if source_package else prefix
    if api_style == "openai":
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": suffix},
            ],
        }
        if reasoning_split:
            body["reasoning_split"] = True  # MiniMax M3: separate thinking from final content
        return body
    # anthropic shape
    system = system_text if cache_mode == "passive" else [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
    ]
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": [{"type": "text", "text": suffix}]}],
    }


def headers_for(api_key: str, api_style: str) -> dict[str, str]:
    if api_style == "openai":
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    return {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}


def extract_text(data: dict, api_style: str) -> str:
    if api_style == "openai":
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def extract_reasoning(data: dict, api_style: str) -> str:
    """The model's thinking chain, from whichever shape the provider returns:
    - MiniMax (split honored):        `reasoning_details` field;
    - DeepSeek (OpenAI wire):         `reasoning_content` field;
    - native inline format:           `<think>` tags inside `content`;
    - anthropic wire:                 `thinking` content blocks."""
    if api_style == "openai":
        parts: list[str] = []
        try:
            details = data["choices"][0]["message"].get("reasoning_details") or []
            parts = [d.get("text", "") for d in details if isinstance(d, dict)]
        except (KeyError, IndexError, TypeError):
            parts = []
        if not parts:
            try:
                rc = data["choices"][0]["message"].get("reasoning_content") or ""
            except (KeyError, IndexError, TypeError):
                rc = ""
            if rc:
                parts = [rc]
        if not parts:
            content = ""
            try:
                content = data["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                content = ""
            parts = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
        return "\n".join(parts)
    blocks = data.get("content", [])
    return "\n".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")


def extract_usage(data: dict, api_style: str) -> dict[str, int]:
    usage = data.get("usage", {}) or {}
    if api_style == "openai":
        details = usage.get("prompt_tokens_details", {}) or {}
        return {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
            "cache_read_input_tokens": int(details.get("cached_tokens", 0)),
            "cache_creation_input_tokens": 0,  # passive caching bills no writes
        }
    return {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens", 0)),
    }


def parse_fragment(text: str, block_name: str) -> dict:
    """Strip fences/whitespace, strip inline thinking, slice the outermost JSON
    object, validate contract."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned
        cleaned = cleaned.strip()
    # Inline thinking (native OpenAI format — relays may strip reasoning_split):
    # remove <think>...</think> so braces inside the reasoning cannot corrupt
    # the JSON slice, and so the reasoning isn't silently discarded.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first == -1 or last == -1:
        raise ValueError(f"no JSON object in response for {block_name}")
    fragment = json.loads(cleaned[first : last + 1])
    if fragment.get("block") != block_name or fragment.get("status") != "completed":
        raise ValueError(f"bad fragment contract for {block_name}")
    return fragment


def call_provider(block: dict[str, str], prefix: str, source_package: str, suffix: str, cfg: argparse.Namespace, *, fallback: bool) -> tuple[dict | None, dict[str, int], str | None, str]:
    """One block fill against a provider config.
    Returns (fragment, usage, error, thinking)."""
    base_url = cfg.fallback_base_url if fallback else cfg.base_url
    api_key_env = cfg.fallback_api_key_env if fallback else cfg.api_key_env
    model = cfg.fallback_model if fallback else cfg.model
    api_style = cfg.fallback_api_style if fallback else cfg.api_style
    max_tokens = cfg.fallback_max_tokens if fallback else cfg.max_tokens
    cache_mode = "passive" if api_style == "openai" else cfg.cache_mode

    url = endpoint(base_url, api_style)
    body = build_body(prefix, source_package, suffix, model, max_tokens, api_style, cache_mode, cfg.reasoning_split)
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return None, {k: 0 for k in TELEMETRY_KEYS}, f"{api_key_env} is not set", ""

    last_error: str | None = None
    for attempt in range(cfg.retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers_for(api_key, api_style), timeout=cfg.timeout)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            text = extract_text(data, api_style)
            usage = extract_usage(data, api_style)
            thinking = extract_reasoning(data, api_style)
            fragment = parse_fragment(text, block["name"])
            return fragment, usage, None, thinking
        except Exception as exc:  # noqa: BLE001 — per-block retry keeps the warm cache
            last_error = str(exc)
            if attempt < cfg.retries:
                time.sleep(cfg.retry_delay)
    return None, {k: 0 for k in TELEMETRY_KEYS}, last_error, ""


def run_blocks(args: argparse.Namespace, prefix: str, source_package: str, work_dir: Path, *, fallback: bool) -> tuple[dict[str, dict], dict[str, int], list[str], dict[str, int]]:
    fragments: dict[str, dict] = {}
    totals = {k: 0 for k in TELEMETRY_KEYS}
    failed: list[str] = []
    reasoning_blocks = 0
    reasoning_chars = 0
    for block in BLOCKS:
        if args.blocks and block["name"] not in args.blocks:
            continue
        web_results = ""
        if block["name"] == "metadata_localization" and args.web_results_file:
            try:
                data = json.loads(Path(args.web_results_file).read_text(encoding="utf-8"))
                web_results = data.get("results", "") if isinstance(data, dict) else ""
            except OSError as exc:
                print(f"[prep-cache] cannot read web results: {exc}", file=sys.stderr)
        suffix = build_suffix(block, args.vol_id, args.series_id, work_dir, web_results=web_results)
        model = args.fallback_model if fallback else args.model
        profile = MODEL_PROFILES.get(model)
        estimate = estimate_tokens(prefix + source_package + suffix) + 256
        if profile and estimate > profile["context_window"]:
            failed.append(block["name"])
            print(f"[prep-cache] {block['name']}: input estimate {estimate} exceeds {model}'s {profile['context_window']} context window", file=sys.stderr)
            continue
        fragment, usage, error, thinking = call_provider(block, prefix, source_package, suffix, args, fallback=fallback)
        for k in TELEMETRY_KEYS:
            totals[k] += usage[k]
        if fragment is not None:
            fragments[block["name"]] = fragment
            (work_dir / ".context" / "prep_blocks" / f"{block['name']}.json").write_text(
                json.dumps(fragment, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if thinking:
                reasoning_blocks += 1
                reasoning_chars += len(thinking)
            if args.thinking_dir and thinking:
                thinking_dir = Path(args.thinking_dir)
                thinking_dir.mkdir(parents=True, exist_ok=True)
                (thinking_dir / f"{block['name']}.md").write_text(thinking, encoding="utf-8")
        else:
            failed.append(block["name"])
            print(f"[prep-cache] {'fallback' if fallback else 'primary'} FAILED {block['name']}: {error}", file=sys.stderr)
    return fragments, totals, failed, {
        "blocks_with_reasoning": reasoning_blocks,
        "reasoning_chars": reasoning_chars,
        "thinking_dir_requested": bool(args.thinking_dir),
    }


def dry_run(work_dir: Path, series_id: str, args: argparse.Namespace) -> dict:
    prefix = PREFIX_FILE.read_text(encoding="utf-8")
    source_package, source_stats = build_source_package(work_dir)
    suffix0 = build_suffix(BLOCKS[0], "<vol_id>", series_id, work_dir)
    primary_body = build_body(prefix, source_package, suffix0, args.model, args.max_tokens, args.api_style, args.cache_mode, args.reasoning_split)
    fallback_body = build_body(prefix, source_package, suffix0, args.fallback_model, args.fallback_max_tokens, args.fallback_api_style, "passive", args.reasoning_split)
    prefix_tokens_estimate = estimate_tokens(prefix)
    source_tokens_estimate = estimate_tokens(source_package)
    cached_region_tokens = prefix_tokens_estimate + source_tokens_estimate
    suffix_tokens_estimate = estimate_tokens(suffix0)
    return {
        "primary": {
            "provider": "zhi-api (MiniMax-M3)",
            "base_url": args.base_url,
            "api_key_env": args.api_key_env,
            "api_style": args.api_style,
            "model": args.model,
            "max_tokens": args.max_tokens,
            "context_window": MODEL_PROFILES.get(args.model, {}).get("context_window"),
            "max_output": MODEL_PROFILES.get(args.model, {}).get("max_output"),
            "endpoint": endpoint(args.base_url, args.api_style),
            "cache": "automatic passive (no cache_control on openai wire)",
        },
        "fallback": {
            "provider": "deepseek-official",
            "base_url": args.fallback_base_url,
            "api_key_env": args.fallback_api_key_env,
            "api_style": args.fallback_api_style,
            "model": args.fallback_model,
            "max_tokens": args.fallback_max_tokens,
            "context_window": MODEL_PROFILES.get(args.fallback_model, {}).get("context_window"),
            "max_output": MODEL_PROFILES.get(args.fallback_model, {}).get("max_output"),
            "endpoint": endpoint(args.fallback_base_url, args.fallback_api_style),
            "cache": "DeepSeek reports prefix cache reuse in usage",
        },
        "prefix_bytes": len(prefix.encode("utf-8")),
        "prefix_tokens_estimate": prefix_tokens_estimate,
        "caching_threshold_ok": prefix_tokens_estimate >= 512,
        "source_package": source_stats,
        "source_tokens_estimate": source_tokens_estimate,
        "cached_region_tokens": cached_region_tokens,
        "input_tokens_estimate_per_block": cached_region_tokens + suffix_tokens_estimate + 256,
        "blocks_composed": len(BLOCKS),
        "sample_system_prefix": str(primary_body.get("system") or primary_body["messages"][0]["content"])[:120],
        "sample_suffix": suffix0[:200],
        "telemetry_expected": TELEMETRY_KEYS,
    }


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description="MTLS prep block-fill cache loop — zhi-api (MiniMax-M3) primary, DeepSeek official fallback.")
    parser.add_argument("--dry-run", action="store_true", help="compose + verify without any network call")
    parser.add_argument("--vol-id", default="")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--series-id", default="")
    parser.add_argument("--blocks", default="", help="comma-separated block names to fill (subset re-run)")
    parser.add_argument("--no-source", action="store_true",
                        help="do not embed the volume source package in the system context (cache-loop fill is otherwise blind)")
    parser.add_argument("--web-results-file", default="",
                        help="JSON file {\"results\": \"...\"} with Bookwalker results for blk-17 metadata_localization")
    parser.add_argument("--thinking-dir", default="",
                        help="archive each block's reasoning_details/thinking chain to <dir>/<block>.md")

    # Primary: zhi-api (settings.yaml `zhi-api` block defaults)
    parser.add_argument("--base-url", default="https://zhi-api.com/v1")
    parser.add_argument("--api-key-env", default="ZHI_API_API_KEY")
    parser.add_argument("--api-style", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--model", default="MiniMax-M3")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="output cap; defaults to the model profile (MiniMax-M3: 512,000)")
    parser.add_argument("--reasoning-split", action="store_true", default=True,
                        help="MiniMax M3: separate thinking from final content (vendor-documented)")
    parser.add_argument("--no-reasoning-split", action="store_true",
                        help="keep thinking inline in content instead of reasoning_split")

    # Fallback: deepseek-official (dsh-llm-deepseek package contract)
    parser.add_argument("--no-fallback", action="store_true", help="disable the DeepSeek fallback")
    parser.add_argument("--fallback-base-url", default="https://api.deepseek.com")
    parser.add_argument("--fallback-api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--fallback-api-style", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--fallback-model", default="deepseek-v4-pro")
    parser.add_argument("--fallback-max-tokens", type=int, default=None,
                        help="defaults to the fallback model profile (deepseek-v4-pro: 256,000)")

    # Tuning
    parser.add_argument("--cache-mode", choices=["passive", "explicit"], default="passive",
                        help="only meaningful with --api-style anthropic; openai wire is always passive")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    if args.blocks:
        args.blocks = {b.strip() for b in args.blocks.split(",") if b.strip()}

    if args.max_tokens is None:
        args.max_tokens = MODEL_PROFILES.get(args.model, {}).get("max_output", 128000)
    if args.fallback_max_tokens is None:
        args.fallback_max_tokens = MODEL_PROFILES.get(args.fallback_model, {}).get("max_output", 256000)
    if args.no_reasoning_split:
        args.reasoning_split = False

    if args.dry_run:
        print(json.dumps(dry_run(Path(args.work_dir), args.series_id, args), indent=2))
        return

    if not args.vol_id:
        parser.error("--vol-id is required unless --dry-run")

    prefix = PREFIX_FILE.read_text(encoding="utf-8")
    work_dir = Path(args.work_dir)
    (work_dir / ".context" / "prep_blocks").mkdir(parents=True, exist_ok=True)
    (work_dir / ".receipts").mkdir(parents=True, exist_ok=True)

    source_package, source_stats = ("", {"jp_chapters": 0, "jp_chars": 0, "context_xml_chars": 0, "manifest_chars": 0}) if args.no_source else build_source_package(work_dir)

    fragments, totals, failed, reasoning_stats = run_blocks(args, prefix, source_package, work_dir, fallback=False)
    fallback_report: dict = {"used": False}
    if failed and not args.no_fallback:
        print(f"[prep-cache] fallback -> deepseek-official for: {', '.join(failed)}", file=sys.stderr)
        fb_fragments, fb_totals, fb_failed, fb_reasoning = run_blocks(args, prefix, source_package, work_dir, fallback=True)
        fragments.update(fb_fragments)
        for k in TELEMETRY_KEYS:
            totals[k] += fb_totals[k]
        reasoning_stats["blocks_with_reasoning"] += fb_reasoning["blocks_with_reasoning"]
        reasoning_stats["reasoning_chars"] += fb_reasoning["reasoning_chars"]
        failed = fb_failed
        fallback_report = {"used": True, "model": args.fallback_model, "base_url": args.fallback_base_url,
                           "blocks_failed_on_fallback": fb_failed}

    read_ratio = totals["cache_read_input_tokens"] / max(
        1, totals["cache_read_input_tokens"] + totals["cache_creation_input_tokens"] + totals["input_tokens"]
    )
    report = {
        "vol_id": args.vol_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_package": source_stats,
        "primary": {"provider": "zhi-api", "model": args.model, "api_style": args.api_style, "base_url": args.base_url},
        "fallback": fallback_report,
        "blocks_requested": len(args.blocks or [b["name"] for b in BLOCKS]),
        "blocks_completed": len(fragments),
        "blocks_failed": failed,
        "telemetry": totals,
        "thinking": reasoning_stats,
        "cache_read_ratio": round(read_ratio, 4),
        "fragments_dir": str(work_dir / ".context" / "prep_blocks"),
    }
    (work_dir / ".receipts" / f"prep_cache_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
