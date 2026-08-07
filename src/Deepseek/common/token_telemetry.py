"""
Token & cost telemetry — the single source of truth both prep and translator
log through, so LOG/token_log.md is one unified ledger instead of two
independently-drifting ones.

Three responsibilities live here, and nowhere else:

1. **Pricing** (PRICING_PER_MTOK) — moved from src/translator/deepseek_client.py's
   old _DEEPSEEK_RATES_PER_MTOK / _DEEPSEEK_FLASH_RATES_PER_MTOK module
   constants. That file now delegates estimate_usage_cost_usd() here instead
   of keeping its own copy — one pricing table, not two that can silently
   drift apart after the next DeepSeek price change.
   Source: https://api-docs.deepseek.com/quick_start/pricing (verified live
   2026-07-27 — permanent 75%-off pricing effective 2026-05-31).

2. **Token counting** (count_tokens) — local only, no network, no auth.
   Briefly routed through DeepSeek's official HuggingFace tokenizers
   (deepseek-ai/DeepSeek-V4-Flash/-Pro) for a closer count than an
   approximation gives; reverted when that turned out to need HF_TOKEN in
   practice (gated rate limits, not the unauthenticated-works case the first
   local test happened to hit) — a token-counting utility silently depending
   on an unrelated auth secret is a worse trade than a slightly coarser
   count. Back to tiktoken's o200k_base as primary (the same "closest
   approximation" deepseek_client.py always used, pre-dating this module),
   cl100k_base as a secondary encoding fallback, a char-based heuristic
   as the true last resort. Never raises, always degrades, never touches
   the network.

   "Cached" token counts can NEVER come from local tokenization — that's
   server-side knowledge (which bytes DeepSeek's disk cache actually served)
   no local tokenizer can reconstruct. Every caller here sources cache-hit
   counts from the API's own usage.cache_read_input_tokens, never derives it.

3. **Logging** (log_call) — thread-safe append to
   WORK/<volume_id>/LOG/token_log_<run-stamp>.md, ONE FILE PER RUN, not a
   single pipeline-root ledger (the original design) and not even a single
   per-volume ledger (the next one) — both let an unrelated later rerun's
   rows land in the same file as an earlier run's, with nothing but the
   per-row timestamp column to tell them apart after the fact. The run
   stamp is established once, on the first log_call() this process makes
   for a given volume_id, and reused for every subsequent call in that same
   run (see _session_stamp_for_volume) — so one prep run's 14 calls, or one
   translate run's N chapters, land in one file, and the next invocation
   (a genuinely new process) gets its own. The header identifies the
   project from context.xml's own OPF-sourced metadata (title/author/
   publisher — see _read_opf_metadata) plus the JP source's raw size
   (character count) and an estimated input-token count for the whole
   volume, both via count_tokens(). Prep's parallel Flash calls (up to 12
   concurrent, see src/prep/parallel_agent.py) and the translator can both
   write without interleaving/corrupting rows.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from xml.etree import ElementTree as ET

from src.Deepseek.common.config import WORK_DIR

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════
# Pricing — USD per 1M tokens. Re-verify against the pricing page before
# trusting this after any real gap in time; DeepSeek's rates move.
# ══════════════════════════════════════════════════════════════════════════

PRICING_PER_MTOK: Dict[str, Dict[str, float]] = {
    "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
    "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14, "output": 0.28},
    # QwenCloud text-only pricing (USD / 1M tokens), verified 2026-08-06 against
    # https://www.qwencloud.com/models/{qwen3.8-max,qwen3.7-plus,qwen3.7-flash}.
    # cache_hit = implicit (automatic) cache read; cache_write = explicit cache
    # creation (Context Cache API). Explicit cache read has its own lower rate
    # ($0.17 / $0.04 / $0.003) but we don't track that distinction yet.
    # qwen3.7-plus currently also has a 20%-off promo ($0.32/$1.28/$0.064/$0.4);
    # list prices below, not promo.
    "qwen3.8-max":  {"cache_hit": 0.25, "cache_miss": 2.0, "cache_write": 2.5,  "output": 6.0},
    "qwen3.7-plus": {"cache_hit": 0.08, "cache_miss": 0.4, "cache_write": 0.5,  "output": 1.6},
    "qwen3.7-flash":{"cache_hit": 0.006,"cache_miss": 0.03,"cache_write": 0.038,"output": 0.13},
}


def _rates_for_model(model_name: str) -> Dict[str, float]:
    name = str(model_name or "").strip().lower()
    if name in PRICING_PER_MTOK:
        return PRICING_PER_MTOK[name]
    if name.startswith("qwen"):
        if "flash" in name:
            return PRICING_PER_MTOK["qwen3.7-flash"]
        if "plus" in name:
            return PRICING_PER_MTOK["qwen3.7-plus"]
        # Frontier-class fallback: any other qwen model (qwen3.7-max and
        # whatever succeeds it) bills at max-tier rates. Deliberately the
        # priciest Qwen tier — a cost ledger that guesses low is worse than
        # useless, because nobody audits a number that looks cheap.
        return PRICING_PER_MTOK["qwen3.8-max"]
    if "flash" in name:
        return PRICING_PER_MTOK["deepseek-v4-flash"]
    return PRICING_PER_MTOK["deepseek-v4-pro"]  # default: the pricier tier, never silently undercount


def cost_breakdown_usd(
    *,
    model_name: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Full cost breakdown dict — same shape src/translator/deepseek_client.py's
    estimate_usage_cost_usd() has always returned, so that method can delegate
    here without changing its contract for existing callers.

    For DeepSeek, cache_creation_tokens is accepted for signature compatibility
    only — DeepSeek's automatic caching bills cache writes at $0.00. For Qwen
    explicit cache, cache creation is billed when the provider reports
    cache_creation_input_tokens.
    """
    rates = _rates_for_model(model_name)
    uncached_input = max(0, int(input_tokens) - int(cache_read_tokens))

    input_cost = uncached_input * rates["cache_miss"] / 1_000_000
    cache_read_cost = int(cache_read_tokens) * rates["cache_hit"] / 1_000_000
    cache_write_rate = float(rates.get("cache_write", 0.0) or 0.0)
    cache_creation_cost = int(cache_creation_tokens) * cache_write_rate / 1_000_000
    output_cost = int(output_tokens) * rates["output"] / 1_000_000
    total = input_cost + cache_read_cost + cache_creation_cost + output_cost

    return {
        "input_cost_usd": round(input_cost, 8),
        "output_cost_usd": round(output_cost, 8),
        "cache_read_cost_usd": round(cache_read_cost, 8),
        "cache_creation_cost_usd": round(cache_creation_cost, 8),
        "cache_total_cost_usd": round(cache_read_cost + cache_creation_cost, 8),
        "total_cost_usd": round(total, 8),
        "input_rate_per_mtok": rates["cache_miss"],
        "output_rate_per_mtok": rates["output"],
        "cache_read_rate_per_mtok": rates["cache_hit"],
    }


def compute_cost_usd(model: str, *, cache_hit_tokens: int, fresh_tokens: int, output_tokens: int) -> float:
    """Total-only convenience wrapper around cost_breakdown_usd() for callers
    (prep) that don't need the per-component split."""
    return cost_breakdown_usd(
        model_name=model,
        input_tokens=int(fresh_tokens) + int(cache_hit_tokens),
        cache_read_tokens=cache_hit_tokens,
        output_tokens=output_tokens,
    )["total_cost_usd"]


# ══════════════════════════════════════════════════════════════════════════
# Tokenizer — local only. tiktoken ships its encodings as part of the
# package (bundled/cached on first use, no account, no token, no per-repo
# gating) — unlike HuggingFace's AutoTokenizer.from_pretrained, which looked
# auth-free in an initial unauthenticated test but turned out to need
# HF_TOKEN under real usage (rate limits or gating depending on the repo).
# A tokenizer utility silently depending on an unrelated Hub credential is
# the wrong trade for what's fundamentally a cost-estimate helper.
# ══════════════════════════════════════════════════════════════════════════

_tiktoken_cache: Dict[str, Any] = {}  # encoding name -> loaded encoding, or False = unavailable
_tiktoken_lock = threading.Lock()

# o200k_base first (GPT-4o family — the closer BPE-vocabulary match to a
# modern large model), cl100k_base second (older but even more ubiquitously
# bundled) — the same two-tier fallback deepseek_client.py used before this
# module existed, restored verbatim rather than reinvented.
_TIKTOKEN_ENCODINGS: tuple = ("o200k_base", "cl100k_base")


def _get_tiktoken_encoding(name: str):
    if name in _tiktoken_cache:
        cached = _tiktoken_cache[name]
        return cached if cached is not False else None
    with _tiktoken_lock:
        if name in _tiktoken_cache:
            cached = _tiktoken_cache[name]
            return cached if cached is not False else None
        try:
            import tiktoken
            enc = tiktoken.get_encoding(name)
        except Exception as exc:
            logger.debug("[token_telemetry] tiktoken encoding %s unavailable (%s)", name, exc)
            _tiktoken_cache[name] = False
            return None
        _tiktoken_cache[name] = enc
        return enc


def count_tokens(text: str, model: str) -> int:
    """Token count for `text` — local tiktoken encoding, not a real
    per-model vocabulary (DeepSeek doesn't publish one usable without Hub
    auth; see module docstring). Close enough for cost estimates and
    context-budget tracking, not billing-exact.

    Priority: tiktoken o200k_base -> tiktoken cl100k_base -> len(text)//4
    heuristic. Never raises, never touches the network after tiktoken's own
    encoding files are cached locally.
    """
    _ = model  # kept in the signature — call sites don't need to change if a
               # real per-model tokenizer ever becomes usable again
    if not text:
        return 0
    for encoding_name in _TIKTOKEN_ENCODINGS:
        enc = _get_tiktoken_encoding(encoding_name)
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception as exc:
                logger.debug("[token_telemetry] tiktoken encode failed with %s (%s) — trying next", encoding_name, exc)
    return max(1, len(text) // 4)


# ══════════════════════════════════════════════════════════════════════════
# Volume metadata — identifies which project a log belongs to, and sizes it,
# without needing a live API call. Read from what Librarian already wrote,
# never re-parsed from the raw .opf (which lives under a transient
# _epub_extracted/ directory a cleanup pass could remove later).
# ══════════════════════════════════════════════════════════════════════════

def _read_opf_metadata(work_dir: Path) -> Dict[str, Any]:
    """context.xml's <opf_metadata> block — Librarian's raw OPF extraction
    (title/author/publisher, JP-language fields). Populated at extract time,
    before prep or translation ever runs (librarian/agent.py's
    _write_context_placeholder writes this block immediately, unlike every
    other block which starts <pending>) — so it's available for every log
    this module ever writes. Returns {} (never raises) if context.xml is
    missing or malformed."""
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        return {}
    try:
        root = ET.fromstring(context_path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return {}
    opf_el = root.find("opf_metadata")
    if opf_el is None or not (opf_el.text or "").strip():
        return {}
    try:
        return json.loads(opf_el.text)
    except json.JSONDecodeError:
        return {}


def _measure_jp_source(work_dir: Path, model: str) -> Tuple[int, int]:
    """(character_count, estimated_input_tokens) across every WORK/<vol>/JP/*.md
    file — the raw JP source's size before any API call ever touches it.
    Character count is literal len(text) per file, summed; token estimate is
    count_tokens() over the concatenated text (local tiktoken approximation,
    same caveats as every other count in this module). Returns (0, 0) if
    JP/ doesn't exist yet."""
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        return 0, 0
    texts = [path.read_text(encoding="utf-8") for path in sorted(jp_dir.glob("*.md"))]
    char_count = sum(len(t) for t in texts)
    token_estimate = count_tokens("\n".join(texts), model) if texts else 0
    return char_count, token_estimate


# ══════════════════════════════════════════════════════════════════════════
# Logging — one file per RUN: WORK/<volume_id>/LOG/token_log_<run-stamp>.md.
# ══════════════════════════════════════════════════════════════════════════

_log_lock = threading.Lock()

# volume_id -> "YYYYMMDD_HHMMSS_xxxx" established the first time this
# process logs a call for that volume, reused for every later call in the
# same run. Separate lock from _log_lock: establishing the stamp and
# appending a row are two different critical sections (a call that already
# knows the stamp shouldn't block on the file-append lock just to read it).
_session_stamp_cache: Dict[str, str] = {}
_session_stamp_lock = threading.Lock()


def _session_stamp_for_volume(volume_id: str) -> str:
    """The run-identifying stamp for THIS process's work on this volume —
    date, time, and a short random tie-breaker (same-second reruns, e.g. a
    fast automated retry loop, still get distinct files). Set once per
    process per volume_id; a later rerun is a new process, starts with an
    empty _session_stamp_cache, and therefore gets a fresh stamp — that's
    the entire mechanism that keeps one run's log from landing in the same
    file as another run's."""
    if volume_id in _session_stamp_cache:
        return _session_stamp_cache[volume_id]
    with _session_stamp_lock:
        if volume_id in _session_stamp_cache:  # re-check after acquiring the lock
            return _session_stamp_cache[volume_id]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
        _session_stamp_cache[volume_id] = stamp
        return stamp


def _log_path_for_volume(volume_id: str) -> Path:
    stamp = _session_stamp_for_volume(volume_id)
    return WORK_DIR / volume_id / "LOG" / f"token_log_{stamp}.md"


def _build_header(volume_id: str, work_dir: Path, model: str) -> str:
    opf = _read_opf_metadata(work_dir)
    title = str(opf.get("dc_title_jp") or "").strip() or volume_id
    author = str(opf.get("author_jp") or "").strip() or "—"
    publisher = str(opf.get("publisher_short") or opf.get("publisher_jp") or "").strip() or "—"
    jp_chars, jp_tokens_est = _measure_jp_source(work_dir, model)
    run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_stamp = _session_stamp_for_volume(volume_id)

    return (
        f"# DeepSeek Token & Cost Log — {volume_id}\n\n"
        f"- **Run started:** {run_started} (stamp `{run_stamp}`)\n"
        f"- **Title (JP):** {title}\n"
        f"- **Author (JP):** {author}\n"
        f"- **Publisher:** {publisher}\n"
        f"- **JP source size:** {jp_chars:,} characters across JP/*.md\n"
        f"- **Estimated input tokens (whole volume, local tiktoken approximation):** {jp_tokens_est:,}\n\n"
        "One row per API call made THIS RUN across prep and/or translator — each "
        "invocation of this process gets its own dated log file rather than "
        "appending into (or blending with) an earlier run's; see "
        "WORK/<volume>/LOG/ for the full run history. Input/output counts come "
        "from a local tiktoken encoding (o200k_base, approximation only — "
        "DeepSeek doesn't publish a real tokenizer usable without HuggingFace "
        "Hub auth), falling back to a char-based heuristic if even that isn't "
        "available; cached-token counts come from the API's own usage object "
        "(`cache_read_input_tokens`) — that's server-side knowledge no local "
        "tokenizer can reconstruct. Pricing: "
        "https://api-docs.deepseek.com/quick_start/pricing.\n\n"
        "| Timestamp (UTC) | Phase | Volume | Call | Model | Cache Hit | Fresh (Miss) | Output | Cost (USD) |\n"
        "|---|---|---|---|---|---:|---:|---:|---:|\n"
    )


def log_call(
    *,
    phase: str,
    volume_id: str,
    call_label: str,
    model: str,
    cache_hit_tokens: int,
    fresh_tokens: int,
    output_tokens: int,
    cost_usd: Optional[float] = None,
) -> float:
    """Append one row to this run's WORK/<volume_id>/LOG/token_log_<stamp>.md
    (see _session_stamp_for_volume). Thread-safe — prep's parallel Flash
    calls and the translator can both write without interleaving or
    corrupting rows.

    Pass cost_usd when the caller already computed it correctly (the
    translator's estimate_usage_cost_usd() has done this for a while) —
    don't make this function silently recompute and risk a rounding-drift
    mismatch against a number the caller is also logging elsewhere. Leave it
    None (prep's case) to have this function compute it from the shared
    pricing table.

    Returns the cost in USD (whichever value ended up in the row).
    """
    if cost_usd is None:
        cost_usd = compute_cost_usd(
            model, cache_hit_tokens=cache_hit_tokens, fresh_tokens=fresh_tokens, output_tokens=output_tokens,
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = (
        f"| {timestamp} | {phase} | {volume_id} | {call_label} | {model} | "
        f"{int(cache_hit_tokens)} | {int(fresh_tokens)} | {int(output_tokens)} | ${cost_usd:.6f} |\n"
    )
    work_dir = WORK_DIR / volume_id
    log_path = _log_path_for_volume(volume_id)
    with _log_lock:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not log_path.exists()
        with open(log_path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(_build_header(volume_id, work_dir, model))
            f.write(row)
    return cost_usd
