"""Anthropic Messages API client for the Phase 2 literary translator.

Scoped, per the operator's explicit choice, to the current Claude-5 family
that shares one capability profile end to end: claude-sonnet-5, claude-opus-5,
claude-fable-5-1. All three use adaptive thinking only (no budget_tokens
manual mode), share a 1M-token context window and 128K max output, and
reject non-default temperature/top_p/top_k with a hard 400 regardless of
thinking state. Claude Haiku 4.5 — the one current-family model still on
manual extended thinking — is deliberately out of scope for this route.

claude-fable-5 was retired from this route on 2026-09-03 in favour of
claude-fable-5-1 (released 2026-09-01), which carries the same input/output
price, the same tokenizer, and a cache-read rate a quarter of its
predecessor's. Fable 5.1 also adds "preserved thinking": a thinking block's
signature binds the conversation prefix that produced it, so replaying a
block after the history in front of it changed is a 400. That constraint is
enforced in conversation.py, not here — see
AnthropicConversationManager._strip_thinking_blocks.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

from src.Anthropic.config import get_anthropic_config
from src.Anthropic.errors import RetryPolicy, call_with_retry, classify_exception
from src.Anthropic.response import response_to_llm_response
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.Deepseek.common.llm_types import LLMApiFamily, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

# The only models this route is built and verified against.
SUPPORTED_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")

# claude-fable-5-1 rejects `thinking: {type: "disabled"}` outright — thinking
# is always on and cannot be turned off at all (Anthropic's thinking docs;
# `budget_tokens` is likewise a 400). The client never attempts to disable
# thinking for this model regardless of translation.anthropic.thinking.enabled.
THINKING_ALWAYS_ON_MODELS = ("claude-fable-5-1",)

# Display spelling -> wire id. Anthropic model ids never contain a dot: the
# marketing name is "Claude Fable 5.1", the request body takes
# "claude-fable-5-1". config.yaml shipped the dotted form, which is not a
# warning but a hard 404 at request time, so normalise rather than trust the
# operator to remember the punctuation. Retired ids map forward to their
# successor so an old config keeps running instead of failing at the wire.
MODEL_ID_ALIASES = {
    "claude-fable-5.1": "claude-fable-5-1",
    "claude-fable-5": "claude-fable-5-1",
    "claude-sonnet-5.0": "claude-sonnet-5",
    "claude-opus-5.0": "claude-opus-5",
}


def normalize_model_id(model: str) -> str:
    """Resolve a configured model string to the id the API actually accepts."""
    name = str(model or "").strip()
    return MODEL_ID_ALIASES.get(name, name)


# Executor -> valid advisor model ids, restricted to the executors this route
# actually supports (SUPPORTED_MODELS above). Sourced directly from Anthropic's
# advisor-tool documentation's Model compatibility table (fetched 2026-09-13),
# not inferred: "the advisor must be Claude Sonnet 4.6 or a more capable model,
# and it must be at least as capable as the executor." claude-fable-5-1 is the
# most restrictive row -- only Fable 5.1 / Mythos 5.1 may advise it -- which is
# why claude-opus-4-8 (the plaintext-readable default validated in Runs 8-11)
# requires a claude-sonnet-5 executor, not claude-opus-5.
ADVISOR_COMPATIBILITY: Dict[str, tuple] = {
    "claude-sonnet-5": (
        "claude-mythos-5-1", "claude-fable-5-1", "claude-mythos-5", "claude-fable-5",
        "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-5",
    ),
    "claude-opus-5": (
        "claude-mythos-5-1", "claude-fable-5-1", "claude-mythos-5", "claude-fable-5",
        "claude-opus-5",
    ),
    "claude-fable-5-1": (
        "claude-mythos-5-1", "claude-fable-5-1",
    ),
}


@dataclass(frozen=True)
class AnthropicCapabilities:
    provider_name: str = "anthropic"
    api_family: LLMApiFamily = LLMApiFamily.ANTHROPIC_MESSAGES
    supports_adaptive_thinking: bool = True
    supports_explicit_caching: bool = True
    supports_streaming: bool = True
    supports_batch: bool = True
    context_window: int = 1_000_000
    max_output_tokens: int = 128_000


ANTHROPIC_CAPABILITIES = AnthropicCapabilities()


class AnthropicClient:
    """Provider-isolated client for the real Anthropic Messages API.

    The client returns MTLS's shared ``LLMResponse`` boundary object, while
    retaining the raw ``content`` array in metadata so thinking blocks can be
    replayed byte-exact by the conversation manager.
    """

    CAPABILITIES = ANTHROPIC_CAPABILITIES

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        dry_run: bool = False,
    ):
        cfg = get_anthropic_config()
        self._cfg = cfg
        configured_model = str(model or cfg.get("model", "claude-sonnet-5"))
        self.model = normalize_model_id(configured_model)
        if self.model != configured_model:
            logger.info(
                "[ANTHROPIC] model %r resolved to wire id %r",
                configured_model,
                self.model,
            )
        if self.model not in SUPPORTED_MODELS:
            logger.warning(
                "[ANTHROPIC] model %r is outside the supported Claude-5 set %s; "
                "this route is built and verified only for those models",
                self.model,
                SUPPORTED_MODELS,
            )
        self._validate_advisor_config(cfg)
        self.api_key_env = str(cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
        self.api_key = api_key or os.getenv(self.api_key_env)
        if not self.api_key and not dry_run:
            raise ValueError(f"Anthropic API key not found. Set {self.api_key_env} or pass api_key explicitly.")
        self.base_url = (base_url or str(cfg.get("endpoint", "https://api.anthropic.com"))).rstrip("/")
        self.anthropic_version = str(cfg.get("anthropic_version", "2023-06-01"))
        self._client = None
        if not dry_run:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError("AnthropicClient requires the Anthropic SDK (pip install anthropic).") from exc
            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(cfg.get("http_timeout_seconds", 600)),
                max_retries=0,
            )
        self.conversation_manager = None
        # Mirrors the OpenAI route's telemetry shape (src/OpenAI/client.py) on
        # purpose: both routes land rows in the same token log, so the columns
        # have to mean the same thing in both.
        self._cache_telemetry: Dict[str, Any] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "ordinary_input_tokens": 0,
            "expected_prefix_tokens": 0,
            "breakpoint_opportunities": 0,
            "breakpoint_successes": 0,
            "recovered_prefix_tokens": 0,
            "expected_prefix_opportunity_tokens": 0,
            "breakpoint_success_rate": 0.0,
            "prefix_recovery": 0.0,
            "total_cache_coverage": 0.0,
            "cache_hit_ratio": 0.0,
            "cache_economics": {
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "ordinary_input_tokens": 0,
                "cache_read_to_write_ratio": 0.0,
                "baseline_input_cost_usd": 0.0,
                "actual_input_cost_usd": 0.0,
                "net_input_savings_usd": 0.0,
            },
        }

    def _validate_advisor_config(self, cfg: Dict[str, Any]) -> None:
        """Fail fast on a broken advisor/web_search config, matching the
        existing SUPPORTED_MODELS fail-fast pattern above -- discovering an
        invalid pairing or a silently-defeated escalation design as a live 400
        or a dead feature mid-volume is exactly what this guards against."""
        advisor_cfg = cfg.get("advisor", {}) or {}
        advisor_enabled = bool(advisor_cfg.get("enabled", False))
        websearch_cfg = cfg.get("web_search", {}) or {}
        websearch_enabled = bool(websearch_cfg.get("enabled", False))

        if advisor_enabled:
            advisor_model = normalize_model_id(str(advisor_cfg.get("model", "claude-opus-4-8")))
            valid_advisors = ADVISOR_COMPATIBILITY.get(self.model, ())
            if advisor_model not in valid_advisors:
                raise ValueError(
                    f"translation.anthropic.advisor.model={advisor_model!r} is not a valid advisor "
                    f"for executor {self.model!r}. Valid advisors for this executor: {valid_advisors}. "
                    "See Anthropic's advisor-tool Model compatibility table."
                )
            thinking_cfg = cfg.get("thinking", {}) or {}
            effort = str(thinking_cfg.get("effort", "high") or "high")
            if effort != "high":
                # Runs 5-7: any effort below "high" either kills escalation
                # entirely or produces a consult whose scoping sentence has
                # nowhere safe to land (the exact leak §7.3's filter guards
                # against, at a configuration this session never validated).
                raise ValueError(
                    "translation.anthropic.advisor.enabled requires thinking.effort: \"high\" "
                    f"(configured: {effort!r}). Effort below \"high\" is unvalidated for advisor mode "
                    "and known to either silently disable escalation or produce an unsafely-placed "
                    "pre-consult text fragment."
                )

        batch_enabled = bool((cfg.get("batch", {}) or {}).get("enabled", False))
        if batch_enabled and advisor_enabled:
            logger.warning(
                "[ANTHROPIC] advisor.enabled and batch.enabled are both true: a paused batch "
                "chapter (stop_reason=pause_turn) resolves via one synchronous follow-up call per "
                "pause, so batch-path telemetry will show a small number of sync-rate rows mixed "
                "into an otherwise batch-rate run. This is expected, not an error."
            )
        if batch_enabled and websearch_enabled:
            logger.warning(
                "[ANTHROPIC] web_search.enabled and batch.enabled are both true: the same "
                "synchronous pause-resolution applies to a web_search pause as it does to an "
                "advisor pause."
            )

    def attach_conversation(self, *, work_dir, volume_id: str, conversation_config: Optional[Dict[str, Any]] = None):
        from src.Anthropic.conversation import AnthropicConversationManager

        self.conversation_manager = AnthropicConversationManager(
            work_dir, volume_id, self.model, self.base_url, conversation_config or {}
        )
        return self.conversation_manager

    @property
    def cache_telemetry(self) -> Dict[str, Any]:
        return dict(self._cache_telemetry)

    def generate(
        self,
        *,
        prompt: str,
        system_instruction: Union[str, Sequence[str]],
        messages: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
        stream: Optional[bool] = None,
        advisor_enable: Optional[bool] = None,
        websearch_enable: Optional[bool] = None,
    ) -> LLMResponse:
        """Build and execute a Messages request, or render it without a key."""
        cfg = self._cfg
        generation_cfg = cfg.get("generation", {}) or {}
        thinking_cfg = cfg.get("thinking", {}) or {}
        caching_cfg = cfg.get("caching", {}) or {}
        streaming_cfg = cfg.get("streaming", {}) or {}

        base_messages = messages if messages is not None else [_user_message(prompt)]
        system_blocks = build_system_blocks(
            system_instruction,
            enabled=bool(caching_cfg.get("enabled", True)),
            ttl=str(caching_cfg.get("ttl", "5m") or "5m"),
        )

        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(generation_cfg.get("max_output_tokens", 128_000) or 128_000),
            "system": system_blocks,
            "messages": base_messages,
            # temperature / top_p / top_k are deliberately never sent: a
            # non-default value on any of them is a hard 400 on every model
            # this route supports, regardless of thinking state.
        }
        if bool(thinking_cfg.get("enabled", True)) or self.model in THINKING_ALWAYS_ON_MODELS:
            request["thinking"] = {
                "type": "adaptive",
                "display": str(thinking_cfg.get("display", "summarized") or "summarized"),
            }
            request["output_config"] = {"effort": str(thinking_cfg.get("effort", "high") or "high")}
        else:
            request["thinking"] = {"type": "disabled"}

        # ── Advisor / web_search server tools (advisor-tool-2026-03-01 beta) ──
        # Beta-header policy exception, stated precisely: this route sends no
        # anthropic-beta headers by standing policy (see
        # AnthropicConversationManager's docstring in conversation.py, re:
        # thinking-binding-controls-2026-08-01) because a beta flag tied to a
        # dated feature can go stale/unrecognized server-side and turn into a
        # hard 400 on every request that carries it. The exception here is
        # narrower than that general concern: `betas` is populated only inside
        # the `if advisor_enabled:` branch below, never unconditionally, and
        # only for the one dated feature this route explicitly supports and
        # tests -- not a reversal of the policy, a scoped carve-out from it.
        #
        # advisor_enable / websearch_enable let a caller (agent.py's per-chapter
        # economic gate, spec §2.1-2.2) turn a tool off for THIS call even when
        # config enables it globally -- None means "no override, use config."
        # A caller can never turn a tool ON that config disabled; only narrow.
        advisor_cfg = cfg.get("advisor", {}) or {}
        advisor_enabled = bool(advisor_cfg.get("enabled", False)) and (advisor_enable is not False)
        websearch_cfg = cfg.get("web_search", {}) or {}
        websearch_enabled = bool(websearch_cfg.get("enabled", False)) and (websearch_enable is not False)

        tools: List[Dict[str, Any]] = []
        if advisor_enabled:
            tools.append(build_advisor_tool(advisor_cfg, route_caching_cfg=caching_cfg))
            request["betas"] = ["advisor-tool-2026-03-01"]  # web_search needs no beta header of its own
        if websearch_enabled:
            tools.append({
                "type": str(websearch_cfg.get("type", "web_search_20250305")),
                "name": "web_search",
                "max_uses": int(websearch_cfg.get("max_uses", 5) or 5),
            })
        if tools:
            request["tools"] = tools

        # A long-running server-tool turn (advisor or web_search) can
        # independently return stop_reason: "pause_turn" mid-generation --
        # Anthropic's own docs confirm this for both tools, not just advisor.
        # Streaming is required so the client observes that pause rather than
        # blocking silently until the connection idles out.
        force_stream = advisor_enabled or websearch_enabled
        use_stream = True if force_stream else bool(streaming_cfg.get("enabled", True) if stream is None else stream)
        request_for_meta = {**request, "stream": use_stream}
        if dry_run:
            return LLMResponse(
                content="[DRY RUN]",
                model=self.model,
                provider="anthropic",
                api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
                provider_metadata={"dry_run": True, "payload": request_for_meta},
            )

        retry_cfg = cfg.get("retry", {}) or {}
        policy = RetryPolicy(
            max_retries=int(retry_cfg.get("max_retries", 5) or 5),
            base_delay_ms=int(retry_cfg.get("base_delay_ms", 500) or 500),
            max_delay_ms=int(retry_cfg.get("max_delay_ms", 32_000) or 32_000),
            jitter_factor=float(retry_cfg.get("jitter_factor", 0.25) or 0.25),
            max_429_retries=int(retry_cfg.get("max_429_retries", 3) or 3),
            max_529_retries=int(retry_cfg.get("max_529_retries", 3) or 3),
        )

        def invoke() -> LLMResponse:
            try:
                response = self._generate_streaming(request) if use_stream else self._generate_blocking(request)
                self._record_cache_telemetry(response)
                response.provider_metadata["cache_telemetry"] = self.cache_telemetry
                return response
            except Exception as exc:  # SDK failures need one provider-neutral boundary
                raise classify_exception(exc) from exc

        return call_with_retry(
            invoke,
            policy,
            on_retry=lambda attempt, err, delay: logger.warning(
                "[ANTHROPIC-RETRY] attempt=%d delay=%.2fs error=%s", attempt, delay, err
            ),
        )

    @property
    def cache_ttl(self) -> str:
        """The configured cache TTL, which selects the cache-write rate."""
        return str((self._cfg.get("caching", {}) or {}).get("ttl", "5m") or "5m")

    def _messages_namespace(self, request: Dict[str, Any]):
        """The beta surface is required whenever `betas` is populated (the
        advisor tool's request shape) -- the plain `messages` namespace
        rejects that key. Every other request uses the stable namespace
        unchanged, exactly as before this route added the advisor tool."""
        return self._client.beta.messages if "betas" in request else self._client.messages

    def _generate_blocking(self, request: Dict[str, Any]) -> LLMResponse:
        response = self._messages_namespace(request).create(**request)
        return response_to_llm_response(response, model=self.model, streamed=False, cache_ttl=self.cache_ttl)

    def _generate_streaming(self, request: Dict[str, Any]) -> LLMResponse:
        with self._messages_namespace(request).stream(**request) as stream:
            final = stream.get_final_message()
        return response_to_llm_response(final, model=self.model, streamed=True, cache_ttl=self.cache_ttl)

    def _record_cache_telemetry(self, response: LLMResponse) -> None:
        """Accumulate this session's cache economics.

        Definitions are lifted from src/OpenAI/client.py rather than reinvented:
        prefix_recovery is recovered/expected prefix tokens, breakpoint success
        counts the calls that cleared warn_threshold_prefix_recovery, coverage
        is cache reads over total input, and net savings is what the same input
        would have cost uncached minus what it did cost.

        Note that on the Messages API input_tokens is the FRESH count - reads
        and writes are reported separately - so total input here is the sum of
        all three, not input_tokens alone.
        """
        usage = response.usage
        telemetry = self._cache_telemetry
        cache_read = int(usage.cache_read_tokens)
        cache_write = int(usage.cache_write_tokens)
        fresh = int(usage.input_tokens)
        prior_calls = int(telemetry["calls"])

        telemetry["calls"] = prior_calls + 1
        telemetry["input_tokens"] = int(telemetry["input_tokens"]) + fresh + cache_read + cache_write
        telemetry["output_tokens"] = int(telemetry["output_tokens"]) + int(usage.output_tokens)
        telemetry["cache_read_tokens"] = int(telemetry["cache_read_tokens"]) + cache_read
        telemetry["cache_write_tokens"] = int(telemetry["cache_write_tokens"]) + cache_write
        telemetry["ordinary_input_tokens"] = int(telemetry["ordinary_input_tokens"]) + fresh

        expected_prefix = int(telemetry["expected_prefix_tokens"])
        if expected_prefix <= 0:
            # The cold write is authoritative; the read is the fallback.
            expected_prefix = cache_write or cache_read
            telemetry["expected_prefix_tokens"] = expected_prefix

        cache_cfg = self._cfg.get("caching", {}) or {}
        monitor = cache_cfg.get("cache_monitor", {}) or {}
        recovery_threshold = float(monitor.get("warn_threshold_prefix_recovery", 0.90) or 0.90)
        if prior_calls >= 1 and expected_prefix > 0:
            recovered = min(cache_read, expected_prefix)
            telemetry["breakpoint_opportunities"] = int(telemetry["breakpoint_opportunities"]) + 1
            telemetry["expected_prefix_opportunity_tokens"] = (
                int(telemetry["expected_prefix_opportunity_tokens"]) + expected_prefix
            )
            telemetry["recovered_prefix_tokens"] = int(telemetry["recovered_prefix_tokens"]) + recovered
            if recovered / float(expected_prefix) >= recovery_threshold:
                telemetry["breakpoint_successes"] = int(telemetry["breakpoint_successes"]) + 1

        opportunities = int(telemetry["breakpoint_opportunities"])
        expected_total = int(telemetry["expected_prefix_opportunity_tokens"])
        telemetry["breakpoint_success_rate"] = round(
            int(telemetry["breakpoint_successes"]) / float(opportunities) if opportunities else 0.0, 4
        )
        telemetry["prefix_recovery"] = round(
            int(telemetry["recovered_prefix_tokens"]) / float(expected_total) if expected_total else 0.0, 4
        )
        total_input = max(1, int(telemetry["input_tokens"]))
        hit_ratio = round(int(telemetry["cache_read_tokens"]) / float(total_input), 4)
        telemetry["total_cache_coverage"] = hit_ratio
        telemetry["cache_hit_ratio"] = hit_ratio

        # What this call's input would have cost with no cache at all, against
        # what it actually cost - the cache write premium included, since a
        # write costs 1.25x fresh and a savings figure that hides that is a
        # flattering fiction.
        billed_input = fresh + cache_read + cache_write
        baseline = cost_breakdown_usd(
            model_name=response.model or self.model,
            input_tokens=billed_input,
            cache_read_included_in_input=False,
            batch=bool(response.batch_pricing),
        )
        actual_input_cost = (
            float(response.input_cost_usd)
            + float(response.cache_read_cost_usd)
            + float(response.cache_creation_cost_usd)
        )
        economics = telemetry["cache_economics"]
        economics["cache_read_tokens"] = int(telemetry["cache_read_tokens"])
        economics["cache_write_tokens"] = int(telemetry["cache_write_tokens"])
        economics["ordinary_input_tokens"] = int(telemetry["ordinary_input_tokens"])
        economics["cache_read_to_write_ratio"] = round(
            int(telemetry["cache_read_tokens"]) / float(max(1, int(telemetry["cache_write_tokens"]))), 4
        )
        economics["baseline_input_cost_usd"] = round(
            float(economics["baseline_input_cost_usd"]) + float(baseline["input_cost_usd"]), 8
        )
        economics["actual_input_cost_usd"] = round(
            float(economics["actual_input_cost_usd"]) + actual_input_cost, 8
        )
        economics["net_input_savings_usd"] = round(
            float(economics["baseline_input_cost_usd"]) - float(economics["actual_input_cost_usd"]), 8
        )

        if bool(monitor.get("enabled", True)) and telemetry["calls"] >= 2:
            threshold = float(monitor.get("warn_threshold_cache_hit_ratio", 0.70) or 0.70)
            if hit_ratio < threshold:
                logger.warning(
                    "[ANTHROPIC-CACHE] coverage %.2f below threshold %.2f (read=%d write=%d fresh=%d)",
                    hit_ratio, threshold,
                    telemetry["cache_read_tokens"], telemetry["cache_write_tokens"],
                    telemetry["ordinary_input_tokens"],
                )
            recovery = float(telemetry["prefix_recovery"])
            if opportunities and recovery < recovery_threshold:
                logger.warning(
                    "[ANTHROPIC-CACHE] prefix recovery %.2f below threshold %.2f "
                    "(breakpoint success %.2f over %d opportunities)",
                    recovery, recovery_threshold,
                    float(telemetry["breakpoint_success_rate"]), opportunities,
                )


def _user_message(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def build_cache_control(*, enabled: bool, ttl: str) -> Optional[Dict[str, Any]]:
    """The one place a cache breakpoint is spelled, so the system blocks and
    the replayed conversation history can never drift apart on TTL."""
    if not enabled:
        return None
    cache_control: Dict[str, Any] = {"type": "ephemeral"}
    if ttl == "1h":
        cache_control["ttl"] = "1h"
    return cache_control


def build_advisor_tool(
    advisor_cfg: Dict[str, Any], *, route_caching_cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The one place the advisor tool block is spelled.

    It was previously spelled twice -- once in the streaming path here and once
    in agent.py's batch path -- and both copies silently omitted `caching` and
    `max_uses`, so two documented config keys were inert. Field names and types
    follow `BetaAdvisorTool20260301Param` in the installed SDK:

      caching:   Optional[BetaCacheControlEphemeralParam] -- "Caching for the
                 advisor's own prompt. When set, each advisor call writes a
                 cache entry at the given TTL so subsequent calls in the same
                 conversation read the stable prefix. When omitted, the advisor
                 prompt is not cached." Omitted (not null) when disabled.
      max_uses:  Optional[int] -- per-request cap.

    TTL resolution: `advisor.caching.ttl` when set, else the route's own
    `caching.ttl`. The advisor conversation spans a whole volume (15 calls in
    volume 646941), so it wants the route's long TTL rather than a 5m default.

    Deliberately NOT set: the sibling `cache_control` field, which places a
    breakpoint on this tool block inside the executor's request prefix. The
    executor's prefix caching already works (430k cache-read tokens by the end
    of a 17-chapter volume); adding a breakpoint there would move existing ones
    for no measured gain. `caching` is the field that was actually broken.
    """
    caching_cfg = advisor_cfg.get("caching", {}) or {}
    route_caching_cfg = route_caching_cfg or {}
    ttl = str(
        caching_cfg.get("ttl")
        or route_caching_cfg.get("ttl", "5m")
        or "5m"
    )
    tool: Dict[str, Any] = {
        "type": "advisor_20260301",
        "name": "advisor",
        "model": normalize_model_id(str(advisor_cfg.get("model", "claude-opus-4-8"))),
        "max_tokens": int(advisor_cfg.get("max_tokens", 24000) or 24000),
    }
    max_uses = advisor_cfg.get("max_uses")
    if max_uses is not None:
        tool["max_uses"] = int(max_uses)
    caching = build_cache_control(
        enabled=bool(caching_cfg.get("enabled", False)), ttl=ttl
    )
    if caching is not None:
        tool["caching"] = caching
    return tool


def build_system_blocks(
    system_instruction: Union[str, Sequence[str]], *, enabled: bool, ttl: str
) -> List[Dict[str, Any]]:
    """One text block per cache layer, each closed by its own breakpoint.

    A caller may still pass a single string — it renders as one block exactly
    as before. Passing segments (see prompt_loader.build_system_segments) puts
    a breakpoint after the static craft policy as well as after the volume
    context, so a volume change re-writes only the second entry instead of
    discarding the policy prefix with it.
    """
    segments = [system_instruction] if isinstance(system_instruction, str) else [str(s) for s in system_instruction]
    segments = [segment for segment in segments if segment.strip()]
    if not segments:
        segments = [""]
    cache_control = build_cache_control(enabled=enabled, ttl=ttl)
    blocks: List[Dict[str, Any]] = []
    for segment in segments:
        block: Dict[str, Any] = {"type": "text", "text": segment}
        if cache_control is not None:
            block["cache_control"] = dict(cache_control)
        blocks.append(block)
    return blocks
