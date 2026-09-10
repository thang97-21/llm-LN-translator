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
   DeepSeek's own rates are peak/off-peak clock-sensitive (see
   deepseek_pricing.json, deepseek_pricing_status()) rather than a single
   flat number — the JSON file is shared with the operator console so the
   visible tariff and the estimator can't wander into separate realities
   after the next price revision.

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

   Cache-read and cache-write token counts can NEVER come from local
   tokenization — they are provider-side knowledge no local tokenizer can
   reconstruct. Every caller sources them from its API usage object.

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
   volume, both via count_tokens(). Prep's optional parallel Flash calls (see src/utility/prep/parallel_agent.py) and the translator can both
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
# Pricing — USD per 1M tokens. DeepSeek's own tariff is peak/off-peak
# clock-sensitive and lives in the JSON file below (shared with the operator
# console) rather than as a flat module constant.
# ══════════════════════════════════════════════════════════════════════════

_DEEPSEEK_PRICING_PATH = Path(__file__).with_name("deepseek_pricing.json")
_DEEPSEEK_PRICING_DOCUMENT = json.loads(_DEEPSEEK_PRICING_PATH.read_text(encoding="utf-8"))
DEEPSEEK_PRICING_SOURCE = str(_DEEPSEEK_PRICING_DOCUMENT["source"])
DEEPSEEK_PEAK_UTC_WINDOWS = tuple(
    (int(window[0]), int(window[1])) for window in _DEEPSEEK_PRICING_DOCUMENT["peak_utc_windows"]
)
DEEPSEEK_PRICING_PER_MTOK: Dict[str, Dict[str, Dict[str, float]]] = {
    str(model): {
        str(period): {str(metric): float(rate) for metric, rate in rates.items()}
        for period, rates in periods.items()
    }
    for model, periods in _DEEPSEEK_PRICING_DOCUMENT["models"].items()
}

PRICING_PER_MTOK: Dict[str, Dict[str, float]] = {
    # QwenCloud real-time API pricing, verified 2026-08-27. cache_hit holds
    # implicit-cache rates; _rates_for_model swaps in the explicit-cache rate
    # when the caller identifies the Qwen cache mode.
    "qwen3.8-max":  {"cache_hit": 0.25, "cache_miss": 2.0, "cache_write": 2.5,  "output": 6.0},
    "qwen3.7-max":  {"cache_hit": 0.25, "cache_miss": 1.25, "cache_write": 1.5625, "output": 3.75},
    "qwen3.7-plus": {"cache_hit": 0.064, "cache_miss": 0.32, "cache_write": 0.4, "output": 1.28},
    "qwen3.7-flash":{"cache_hit": 0.006,"cache_miss": 0.03,"cache_write": 0.038,"output": 0.13},
    # OpenAI GPT-6 Astra pricing (USD / 1M tokens), verified against the
    # official model page. GPT-5.6 family rows follow below.
    "gpt-6-astra":   {"cache_hit": 1.00, "cache_miss": 10.00, "cache_write": 12.50, "output": 50.00},
    # OpenAI GPT-5.6 family text pricing (USD / 1M tokens), verified 2026-08-17
    # against https://developers.openai.com/api/docs/pricing. Sol/Terra/Luna
    # are the three snapshot tiers; the long-context surcharge above 272K
    # input tokens is applied in _rates_for_model from the actual request
    # size rather than exposed as an operator-controlled knob.
    "gpt-5.6-sol":  {"cache_hit": 0.50, "cache_miss": 5.00, "cache_write": 6.25, "output": 30.00},
    "gpt-5.6-terra":{"cache_hit": 0.20, "cache_miss": 2.00, "cache_write": 2.50, "output": 12.00},
    "gpt-5.6-luna": {"cache_hit": 0.02, "cache_miss": 0.20, "cache_write": 0.25, "output": 1.20},
    # Anthropic Claude-5 family pricing (USD / 1M tokens). sonnet-5/opus-5
    # verified 2026-08-20; claude-fable-5-1 verified 2026-09-03 against
    # https://platform.claude.com/docs/en/models/fable-5-1/overview.
    #
    # cache_hit is 0.1x the base input rate on sonnet-5/opus-5, but Fable 5.1
    # prices cache reads at 0.025x — $0.25/MTok, a per-model rate the vendor
    # states outright, NOT the family multiplier. It must be read from this
    # table and never derived from cache_miss, or every cached token on the
    # Fable route is overstated fourfold. cache_write approximates the 5m-TTL
    # write multiplier (1.25x input); the 1h-TTL write rate (2x — $20/MTok on
    # fable-5-1) isn't tracked as a separate column, the same "not
    # billing-exact" disclaimer this module already carries for DeepSeek and
    # Qwen's own approximated rates.
    # cache_write is the 5m-TTL rate (1.25x input); cache_write_1h is the
    # 1h-TTL rate (2x input). $20/MTok on fable-5-1 is stated outright on the
    # model page; the other two follow the documented 2x rule. Selected by
    # translation.anthropic.caching.ttl, so a 1h run is not costed at the 5m
    # rate and understated by 40%.
    "claude-sonnet-5":  {"cache_hit": 0.20, "cache_miss": 2.00, "cache_write": 2.50,  "cache_write_1h": 4.00,  "output": 10.00},
    "claude-opus-5":    {"cache_hit": 0.50, "cache_miss": 5.00, "cache_write": 6.25,  "cache_write_1h": 10.00, "output": 25.00},
    "claude-fable-5-1": {"cache_hit": 0.25, "cache_miss": 10.00, "cache_write": 12.50, "cache_write_1h": 20.00, "output": 50.00},
    # Z.AI GLM-5.3 pricing (USD / 1M tokens), verified 2026-08-27. Cached
    # input storage is limited-time free, so it is not a token-billed write.
    "glm-5.3":       {"cache_hit": 0.26,  "cache_miss": 1.40, "cache_write": 0.0, "output": 4.40},
    "glm-5.3-flash": {"cache_hit": 0.03,  "cache_miss": 0.15, "cache_write": 0.0, "output": 0.50},
}

# Message Batches API discount. "50% cost reduction on all token usage"
# (Anthropic's batch-processing docs; the Fable 5.1 model page states it as
# "50% discount on input and output"). Applied to all four rate columns.
#
# The residual uncertainty is the cache columns: if Anthropic bills cache
# reads and writes undiscounted, this understates a batch run by that portion
# alone. That is a deliberate choice against this module's usual
# guess-high rule, because a ledger for a route whose entire justification is
# the discount must be able to show it. Verify a real batch run against the
# console before treating these rows as billing-exact - this module has never
# claimed to be.
_BATCH_DISCOUNT = 0.5

# The exact model set src/Anthropic is built and verified against. Kept here
# so the conservative fallback below is derived from the table rather than
# from a hand-picked row that can silently stop being the priciest one.
_ANTHROPIC_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")

_QWEN_EXPLICIT_CACHE_READ_PER_MTOK = {
    "qwen3.8-max": 0.17,
    "qwen3.7-max": 0.125,
    "qwen3.7-plus": 0.032,
    "qwen3.7-flash": 0.003,
}
_GLM_FLASH_PROMOTION_END = datetime(2026, 9, 9, 16, tzinfo=timezone.utc)
_GLM_FLASH_PROMOTION_RATES = {"cache_hit": 0.015, "cache_miss": 0.075, "cache_write": 0.0, "output": 0.25}


def deepseek_pricing_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return the current DeepSeek tariff using the host computer's local clock.

    DeepSeek publishes its schedule in UTC. ``datetime.now().astimezone()``
    obtains the machine's local timezone (including daylight-saving rules), then
    this function converts that instant to UTC before choosing the tariff. An
    aware ``now`` is accepted for deterministic tests and audit replay; a naïve
    value is interpreted as local machine time, matching the normal path.
    """
    local_now = now if now is not None else datetime.now().astimezone()
    if local_now.tzinfo is None:
        local_now = local_now.astimezone()
    utc_now = local_now.astimezone(timezone.utc)
    has_peak_windows = bool(DEEPSEEK_PEAK_UTC_WINDOWS)
    is_peak = has_peak_windows and any(start <= utc_now.hour < end for start, end in DEEPSEEK_PEAK_UTC_WINDOWS)
    period = "peak" if is_peak else "off_peak"
    return {
        "period": period,
        "label": "Peak" if is_peak else ("Off-peak" if has_peak_windows else "Standard"),
        "local_time": local_now.isoformat(timespec="minutes"),
        "local_timezone": local_now.tzname() or "local time",
        "utc_time": utc_now.isoformat(timespec="minutes"),
        "peak_utc_windows": DEEPSEEK_PEAK_UTC_WINDOWS,
        "rates_per_mtok": DEEPSEEK_PRICING_PER_MTOK["deepseek-v4-pro"][period],
        "rates_by_model_per_mtok": {
            model: periods[period] for model, periods in DEEPSEEK_PRICING_PER_MTOK.items()
        },
        "source": DEEPSEEK_PRICING_SOURCE,
    }


def _rates_for_model(
    model_name: str,
    *,
    input_tokens: int = 0,
    pricing_at: Optional[datetime] = None,
    cache_pricing_mode: str = "implicit",
) -> Dict[str, float]:
    name = str(model_name or "").strip().lower()
    if name.startswith("gpt-6-astra"):
        rates = PRICING_PER_MTOK["gpt-6-astra"]
        if int(input_tokens) > 272_000:
            return {
                "cache_hit": rates["cache_hit"] * 2,
                "cache_miss": rates["cache_miss"] * 2,
                "cache_write": rates["cache_write"] * 2,
                "output": rates["output"] * 1.5,
            }
        return rates
    if name.startswith("gpt-5.6"):
        if name == "gpt-5.6" or name.startswith("gpt-5.6-sol"):
            model = "gpt-5.6-sol"
        elif name.startswith("gpt-5.6-terra"):
            model = "gpt-5.6-terra"
        elif name.startswith("gpt-5.6-luna"):
            model = "gpt-5.6-luna"
        else:
            # An unrecognized GPT-5.6 snapshot must not inherit Luna's
            # economy rate. The documented alias is Sol, so that is the
            # conservative estimate until a newer snapshot has a table entry.
            model = "gpt-5.6-sol"
        rates = PRICING_PER_MTOK[model]
        if int(input_tokens) > 272_000:
            return {
                "cache_hit": rates["cache_hit"] * 2,
                "cache_miss": rates["cache_miss"] * 2,
                "cache_write": rates["cache_write"] * 2,
                "output": rates["output"] * 1.5,
            }
        return rates
    if name == "glm-5.3-flash":
        at = pricing_at or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return _GLM_FLASH_PROMOTION_RATES if at.astimezone(timezone.utc) < _GLM_FLASH_PROMOTION_END else PRICING_PER_MTOK[name]
    if name.startswith("qwen"):
        if "flash" in name:
            model = "qwen3.7-flash"
        elif "plus" in name:
            model = "qwen3.7-plus"
        elif "3.7-max" in name:
            model = "qwen3.7-max"
        else:
            model = "qwen3.8-max"
        # Frontier-class fallback: any other qwen model (qwen3.7-max and
        # whatever succeeds it) bills at max-tier rates. Deliberately the
        # priciest Qwen tier — a cost ledger that guesses low is worse than
        # useless, because nobody audits a number that looks cheap.
        rates = dict(PRICING_PER_MTOK[model])
        if str(cache_pricing_mode).lower() == "explicit":
            rates["cache_hit"] = _QWEN_EXPLICIT_CACHE_READ_PER_MTOK[model]
        return rates
    if name in PRICING_PER_MTOK:
        return PRICING_PER_MTOK[name]
    if name.startswith("claude-"):
        # Accept the dotted display spelling ("claude-fable-5.1") as an alias
        # for the wire id ("claude-fable-5-1"). config.yaml carried the dotted
        # form for a while, and a cost ledger should not drop to a guess over
        # a punctuation difference when it knows the real rates.
        aliased = name.replace(".", "-")
        if aliased in PRICING_PER_MTOK:
            return PRICING_PER_MTOK[aliased]
        # Any other claude-* name (a genuine typo, or a model outside the
        # supported set) must not silently undercount. No single row is the
        # conservative choice any more: fable-5-1 is priciest on input and
        # output but has the CHEAPEST cache read of the three ($0.25 against
        # Opus 5's $0.50), so take the per-column maximum instead.
        return {
            column: max(PRICING_PER_MTOK[model][column] for model in _ANTHROPIC_MODELS)
            for column in ("cache_hit", "cache_miss", "cache_write", "output")
        }
    if name.startswith("glm-5.3-flash"):
        return PRICING_PER_MTOK["glm-5.3-flash"]
    if name.startswith("glm-5.3"):
        return PRICING_PER_MTOK["glm-5.3"]
    model = "deepseek-v4-flash" if "flash" in name else "deepseek-v4-pro"
    return DEEPSEEK_PRICING_PER_MTOK[model][deepseek_pricing_status(pricing_at)["period"]]


def cost_breakdown_usd(
    *,
    model_name: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_included_in_input: bool = False,
    cache_read_included_in_input: bool = True,
    cache_ttl: str = "5m",
    pricing_at: Optional[datetime] = None,
    cache_pricing_mode: str = "implicit",
    batch: bool = False,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Full cost breakdown dict — same shape src/translator/deepseek_client.py's
    estimate_usage_cost_usd() has always returned, so that method can delegate
    here without changing its contract for existing callers.

    ``cache_creation_included_in_input`` is true for native OpenAI Responses,
    where ``usage.input_tokens`` already contains cache-write tokens. Other
    providers retain the established accounting semantics by default.

    ``cache_read_included_in_input`` is the mirror image, and it matters more.
    The Anthropic Messages API reports ``input_tokens``,
    ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` as three
    DISJOINT counts: input_tokens already excludes anything served from cache.
    Subtracting the reads again drives the fresh figure to zero on exactly the
    workload caching is for - a 44K cached prefix against a 5K chapter
    envelope yields max(0, 5000 - 44000) = 0, and every genuinely fresh token
    bills at nothing. Pass False for that family. Left True by default so no
    existing provider's accounting shifts underneath it.

    ``cache_ttl`` selects the cache-write rate: "5m" (1.25x input) or "1h"
    (2x). Ignored by models with no 1h column.

    ``pricing_at`` selects DeepSeek's clock-sensitive tariff and the
    date-bounded GLM-5.3-Flash promotion. Other providers ignore it.

    ``batch`` marks a call served by a provider's Batch API (Anthropic Message
    Batches or OpenAI Batch), which bills at half rate. It scales the rates rather than the total, so the
    returned ``*_rate_per_mtok`` fields agree with the returned cost instead
    of quietly disagreeing with it.
    """
    model_name_normalized = str(model_name or "").strip().lower()
    is_deepseek = model_name_normalized.startswith("deepseek")
    rate_status = deepseek_pricing_status(pricing_at) if is_deepseek else None
    rates = _rates_for_model(model_name, input_tokens=input_tokens, pricing_at=pricing_at, cache_pricing_mode=cache_pricing_mode)
    if batch:
        rates = {column: float(rate) * _BATCH_DISCOUNT for column, rate in rates.items()}
    uncached_input = max(
        0,
        int(input_tokens)
        - (int(cache_read_tokens) if cache_read_included_in_input else 0)
        - (int(cache_creation_tokens) if cache_creation_included_in_input else 0),
    )

    input_cost = uncached_input * rates["cache_miss"] / 1_000_000
    cache_read_cost = int(cache_read_tokens) * rates["cache_hit"] / 1_000_000
    cache_write_rate = float(rates.get("cache_write", 0.0) or 0.0)
    if str(cache_ttl).strip().lower() == "1h" and rates.get("cache_write_1h"):
        cache_write_rate = float(rates["cache_write_1h"])
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
        "cache_write_rate_per_mtok": cache_write_rate,
        "pricing_period": rate_status["period"] if rate_status else None,
        "pricing_local_time": rate_status["local_time"] if rate_status else None,
        "pricing_utc_time": rate_status["utc_time"] if rate_status else None,
    }


def compute_cost_usd(
    model: str,
    *,
    cache_hit_tokens: int,
    fresh_tokens: int,
    output_tokens: int,
    batch: bool = False,
) -> float:
    """Total-only convenience wrapper around cost_breakdown_usd() for callers
    (prep) that don't need the per-component split."""
    return cost_breakdown_usd(
        model_name=model,
        input_tokens=int(fresh_tokens) + int(cache_hit_tokens),
        cache_read_tokens=cache_hit_tokens,
        output_tokens=output_tokens,
        batch=batch,
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


def _build_header(volume_id: str, work_dir: Path, model: str, provider: str = "deepseek") -> str:
    opf = _read_opf_metadata(work_dir)
    title = str(opf.get("dc_title_jp") or "").strip() or volume_id
    author = str(opf.get("author_jp") or "").strip() or "—"
    publisher = str(opf.get("publisher_short") or opf.get("publisher_jp") or "").strip() or "—"
    jp_chars, jp_tokens_est = _measure_jp_source(work_dir, model)
    run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_stamp = _session_stamp_for_volume(volume_id)
    normalized_provider = str(provider or "unknown").strip().lower()
    pricing_line = ""
    if normalized_provider == "deepseek":
        rate_status = deepseek_pricing_status()
        pricing_line = (
            f"- **DeepSeek tariff at run start:** {rate_status['label']} "
            f"(computer local {rate_status['local_time']}; UTC {rate_status['utc_time']})\n"
        )

    return (
        f"# Token & Cost Log - {volume_id}\n\n"
        f"- **Provider:** {normalized_provider}\n"
        f"- **Run started:** {run_started} (stamp `{run_stamp}`)\n"
        f"- **Title (JP):** {title}\n"
        f"- **Author (JP):** {author}\n"
        f"- **Publisher:** {publisher}\n"
        f"- **JP source size:** {jp_chars:,} characters across JP/*.md\n"
        f"- **Estimated input tokens (whole volume, local tiktoken approximation):** {jp_tokens_est:,}\n\n"
        f"{pricing_line}"
        "One row per API call made THIS RUN across prep and/or translator — each "
        "invocation of this process gets its own dated log file rather than "
        "appending into (or blending with) an earlier run's; see "
        "WORK/<volume>/LOG/ for the full run history. Per-call input, output, "
        "cache-read, and cache-write counts come from the provider usage object; "
        "the whole-volume JP estimate above remains a local tokenizer estimate.\n\n"
        "| Timestamp (UTC) | Phase | Volume | Call | Model | Cache Hit | Cache Write | Fresh | Output | Breakpoint Success Rate | Prefix Recovery | Total Cache Coverage | Net Cache Savings (USD) | Cost (USD) |\n"
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
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
    provider: str = "deepseek",
    batch: bool = False,
    cache_write_tokens: int = 0,
    breakpoint_success_rate: Optional[float] = None,
    prefix_recovery: Optional[float] = None,
    total_cache_coverage: Optional[float] = None,
    cache_net_savings_usd: Optional[float] = None,
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
            model, cache_hit_tokens=cache_hit_tokens, fresh_tokens=fresh_tokens,
            output_tokens=output_tokens, batch=batch,
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ratio = lambda value: "-" if value is None else f"{float(value) * 100:.2f}%"
    savings = "-" if cache_net_savings_usd is None else f"${float(cache_net_savings_usd):.6f}"
    row = (
        f"| {timestamp} | {phase} | {volume_id} | {call_label} | {model} | "
        f"{int(cache_hit_tokens)} | {int(cache_write_tokens)} | {int(fresh_tokens)} | "
        f"{int(output_tokens)} | {ratio(breakpoint_success_rate)} | {ratio(prefix_recovery)} | "
        f"{ratio(total_cache_coverage)} | {savings} | ${cost_usd:.6f} |\n"
    )
    work_dir = WORK_DIR / volume_id
    log_path = _log_path_for_volume(volume_id)
    with _log_lock:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not log_path.exists()
        with open(log_path, "a", encoding="utf-8") as f:
            if is_new:
                f.write(_build_header(volume_id, work_dir, model, provider))
            f.write(row)
    return cost_usd
