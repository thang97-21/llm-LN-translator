"""Anthropic Messages API client for the Phase 2 literary translator.

Scoped, per the operator's explicit choice, to the current Claude-5 family
that shares one capability profile end to end: claude-sonnet-5, claude-opus-5,
claude-fable-5. All three use adaptive thinking only (no budget_tokens
manual mode), share a 1M-token context window and 128K max output, and
reject non-default temperature/top_p/top_k with a hard 400 regardless of
thinking state. Claude Haiku 4.5 — the one current-family model still on
manual extended thinking — is deliberately out of scope for this route.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.Anthropic.config import get_anthropic_config
from src.Anthropic.errors import RetryPolicy, call_with_retry, classify_exception
from src.Anthropic.response import response_to_llm_response
from src.Deepseek.common.llm_types import LLMApiFamily, LLMResponse, LLMUsage

logger = logging.getLogger(__name__)

# The only models this route is built and verified against.
SUPPORTED_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")

# claude-fable-5 rejects `thinking: {type: "disabled"}` outright — thinking
# cannot be turned off on it at all (Anthropic's thinking docs). The client
# never attempts to disable thinking for this model regardless of
# translation.anthropic.thinking.enabled.
THINKING_ALWAYS_ON_MODELS = ("claude-fable-5",)


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
        self.model = str(model or cfg.get("model", "claude-sonnet-5"))
        if self.model not in SUPPORTED_MODELS:
            logger.warning(
                "[ANTHROPIC] model %r is outside the supported Claude-5 set %s; "
                "this route is built and verified only for those models",
                self.model,
                SUPPORTED_MODELS,
            )
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
        self._cache_telemetry: Dict[str, Any] = {
            "calls": 0,
            "cache_read_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

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
        system_instruction: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
        stream: Optional[bool] = None,
    ) -> LLMResponse:
        """Build and execute a Messages request, or render it without a key."""
        cfg = self._cfg
        generation_cfg = cfg.get("generation", {}) or {}
        thinking_cfg = cfg.get("thinking", {}) or {}
        caching_cfg = cfg.get("caching", {}) or {}
        streaming_cfg = cfg.get("streaming", {}) or {}

        base_messages = messages if messages is not None else [_user_message(prompt)]
        system_blocks = _system_blocks(
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

        use_stream = bool(streaming_cfg.get("enabled", True) if stream is None else stream)
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
                self._record_cache_telemetry(response.usage)
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

    def _generate_blocking(self, request: Dict[str, Any]) -> LLMResponse:
        response = self._client.messages.create(**request)
        return response_to_llm_response(response, model=self.model, streamed=False)

    def _generate_streaming(self, request: Dict[str, Any]) -> LLMResponse:
        with self._client.messages.stream(**request) as stream:
            final = stream.get_final_message()
        return response_to_llm_response(final, model=self.model, streamed=True)

    def _record_cache_telemetry(self, usage: LLMUsage) -> None:
        self._cache_telemetry["calls"] = int(self._cache_telemetry["calls"]) + 1
        self._cache_telemetry["cache_read_tokens"] = int(self._cache_telemetry["cache_read_tokens"]) + usage.cache_read_tokens
        self._cache_telemetry["input_tokens"] = int(self._cache_telemetry["input_tokens"]) + usage.input_tokens
        self._cache_telemetry["output_tokens"] = int(self._cache_telemetry["output_tokens"]) + usage.output_tokens
        input_tokens = max(1, int(self._cache_telemetry["input_tokens"]))
        hit_ratio = float(self._cache_telemetry["cache_read_tokens"]) / float(input_tokens)
        self._cache_telemetry["cache_hit_ratio"] = round(hit_ratio, 4)
        cache_cfg = self._cfg.get("caching", {}) or {}
        monitor = cache_cfg.get("cache_monitor", {}) or {}
        if bool(monitor.get("enabled", True)) and self._cache_telemetry["calls"] >= 2:
            threshold = float(monitor.get("warn_threshold_cache_hit_ratio", 0.70) or 0.70)
            if hit_ratio < threshold:
                logger.warning(
                    "[ANTHROPIC-CACHE] hit ratio %.2f below threshold %.2f (read=%d input=%d)",
                    hit_ratio,
                    threshold,
                    self._cache_telemetry["cache_read_tokens"],
                    self._cache_telemetry["input_tokens"],
                )


def _user_message(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _system_blocks(system_instruction: str, *, enabled: bool, ttl: str) -> List[Dict[str, Any]]:
    block: Dict[str, Any] = {"type": "text", "text": system_instruction}
    if enabled:
        cache_control: Dict[str, Any] = {"type": "ephemeral"}
        if ttl == "1h":
            cache_control["ttl"] = "1h"
        block["cache_control"] = cache_control
    return [block]
