"""Qwen Anthropic-Messages response client.

Implements the official QwenCloud Anthropic-compatible route:
thinking budgets, explicit cache markers, streaming, retries, and
moderation-aware error classification.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMResponse,
    LLMUsage,
    normalize_content_blocks,
    normalize_termination,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.Qwen.config import get_qwen_config
from src.Qwen.errors import RetryPolicy, call_with_retry, classify_exception

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QwenCapabilities:
    provider_name: str = "qwen"
    supports_thinking: bool = True
    supports_explicit_caching: bool = True
    supports_streaming: bool = True
    supports_partial_prefix: bool = True
    supports_retry: bool = True
    supports_moderation_errors: bool = True
    context_window: int = 1_000_000
    max_output_tokens: int = 64_000


QWEN_CAPABILITIES = QwenCapabilities()


@dataclass
class _StreamAccumulator:
    text_parts: List[str] = field(default_factory=list)
    thinking_parts: List[str] = field(default_factory=list)
    raw_blocks: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "end_turn"
    response_id: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    current_block_type: str = ""
    current_text: List[str] = field(default_factory=list)
    current_thinking: List[str] = field(default_factory=list)

    def start_block(self, block_type: str) -> None:
        self.flush_block()
        self.current_block_type = block_type
        self.current_text = []
        self.current_thinking = []

    def flush_block(self) -> None:
        if self.current_block_type == "thinking":
            text = "".join(self.current_thinking)
            self.thinking_parts.append(text)
            self.raw_blocks.append({"type": "thinking", "thinking": text})
        elif self.current_block_type == "text":
            text = "".join(self.current_text)
            self.text_parts.append(text)
            self.raw_blocks.append({"type": "text", "text": text})
        self.current_block_type = ""
        self.current_text = []
        self.current_thinking = []


class QwenClient:
    CAPABILITIES = QWEN_CAPABILITIES

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        dry_run: bool = False,
    ):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError("QwenClient requires the Anthropic SDK (pip install anthropic).") from exc
        cfg = get_qwen_config()
        self.model = model or str(cfg.get("model", "qwen3.8-max"))
        self.api_key_env = str(cfg.get("api_key_env", "DASHSCOPE_API_KEY"))
        self.api_key = api_key or os.getenv(self.api_key_env)
        if not self.api_key and not dry_run:
            raise ValueError(f"Qwen API key not found. Set {self.api_key_env} or pass api_key explicitly.")
        self.base_url = (
            base_url or str(cfg.get("endpoint", "https://dashscope-intl.aliyuncs.com/apps/anthropic"))
        ).rstrip("/")
        self._cfg = cfg
        self._client = None if dry_run else anthropic.Anthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=float(cfg.get("http_timeout_seconds", 600)),
            max_retries=0,
        )
        self.conversation_manager = None
        self._cache_telemetry: Dict[str, Any] = {
            "calls": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def attach_conversation(self, *, work_dir, volume_id: str, conversation_config: Optional[Dict[str, Any]] = None):
        from src.Qwen.conversation import QwenConversationManager

        cfg = conversation_config or {}
        self.conversation_manager = QwenConversationManager(
            work_dir, volume_id, self.model, self.base_url, cfg
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
        partial: bool = False,
        thinking_enabled: Optional[bool] = None,
    ) -> LLMResponse:
        cfg = self._cfg
        thinking_cfg = cfg.get("thinking", {}) or {}
        generation_cfg = cfg.get("generation", {}) or {}
        caching_cfg = cfg.get("caching", {}) or {}
        streaming_cfg = cfg.get("streaming", {}) or {}
        retry_cfg = cfg.get("retry", {}) or {}

        use_thinking = bool(thinking_cfg.get("enabled", True) if thinking_enabled is None else thinking_enabled)
        if partial and use_thinking:
            # Official Partial Mode docs: thinking mode does not support prefix continuation.
            use_thinking = False

        budget = int(thinking_cfg.get("budget_tokens", 32000))
        max_tokens = int(generation_cfg.get("max_output_tokens", 64000))
        if use_thinking and max_tokens <= budget:
            max_tokens = budget + max(1024, max_tokens)
            logger.warning(
                "[QWEN] max_tokens raised to %d so it exceeds thinking.budget_tokens=%d",
                max_tokens,
                budget,
            )

        request_messages = self._prepare_messages(
            messages or [{"role": "user", "content": prompt}],
            explicit_cache=bool(caching_cfg.get("enabled", True) and caching_cfg.get("explicit", True)),
            partial=partial,
        )
        request: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": request_messages,
        }
        if generation_cfg.get("top_p") is not None and not use_thinking:
            request["top_p"] = float(generation_cfg["top_p"])
        if use_thinking:
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
        else:
            request["thinking"] = {"type": "disabled"}

        if caching_cfg.get("enabled", True) and caching_cfg.get("explicit", True):
            request["system"] = [
                {
                    "type": "text",
                    "text": system_instruction,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            request["system"] = system_instruction

        use_stream = bool(streaming_cfg.get("enabled", True) if stream is None else stream)
        request_for_meta = dict(request)
        request_for_meta["stream"] = use_stream

        if dry_run:
            return LLMResponse(
                content="[DRY RUN]",
                model=self.model,
                provider="qwen",
                api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
                provider_metadata={"dry_run": True, "payload": request_for_meta},
            )

        policy = RetryPolicy(
            max_retries=int(retry_cfg.get("max_retries", 5) or 5),
            base_delay_ms=int(retry_cfg.get("base_delay_ms", 500) or 500),
            max_delay_ms=int(retry_cfg.get("max_delay_ms", 32000) or 32000),
            jitter_factor=float(retry_cfg.get("jitter_factor", 0.25) or 0.25),
            max_429_retries=int(retry_cfg.get("max_429_retries", 3) or 3),
        )

        def _invoke():
            try:
                if use_stream:
                    return self._generate_streaming(request)
                return self._generate_blocking(request)
            except Exception as exc:  # noqa: BLE001
                raise classify_exception(exc) from exc

        return call_with_retry(
            _invoke,
            policy,
            on_retry=lambda attempt, err, delay: logger.warning(
                "[QWEN-RETRY] attempt=%d delay=%.2fs error=%s", attempt, delay, err
            ),
        )

    def _prepare_messages(
        self,
        messages: List[Dict[str, Any]],
        *,
        explicit_cache: bool,
        partial: bool,
    ) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        for index, message in enumerate(messages):
            item = dict(message)
            is_last = index == len(messages) - 1
            if partial and is_last and item.get("role") == "assistant":
                item["partial"] = True
            if explicit_cache and is_last:
                item = self._with_cache_control(item)
            prepared.append(item)
        return prepared

    @staticmethod
    def _with_cache_control(message: Dict[str, Any]) -> Dict[str, Any]:
        content = message.get("content")
        if isinstance(content, str):
            return {
                **message,
                "content": [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        if isinstance(content, list) and content:
            blocks = [dict(block) if isinstance(block, dict) else {"type": "text", "text": str(block)} for block in content]
            last = dict(blocks[-1])
            if last.get("type", "text") == "text":
                last["cache_control"] = {"type": "ephemeral"}
                blocks[-1] = last
            return {**message, "content": blocks}
        return message

    def _generate_blocking(self, request: Dict[str, Any]) -> LLMResponse:
        response = self._client.messages.create(**request, stream=False)
        raw_blocks = [_as_dict(block) for block in (_response_field(response, "content", []) or [])]
        return self._to_llm_response(
            raw_blocks=raw_blocks,
            stop_reason=str(_response_field(response, "stop_reason", "end_turn") or "end_turn"),
            response_id=str(_response_field(response, "id", "") or ""),
            usage_obj=_response_field(response, "usage", {}) or {},
            streamed=False,
        )

    def _generate_streaming(self, request: Dict[str, Any]) -> LLMResponse:
        acc = _StreamAccumulator()
        with self._client.messages.stream(**request) as stream:
            for event in stream:
                event_type = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
                if event_type == "message_start":
                    message = _response_field(event, "message", {}) or {}
                    acc.response_id = str(_response_field(message, "id", "") or "")
                    usage = _response_field(message, "usage", {}) or {}
                    if usage:
                        acc.usage = _as_dict(usage)
                elif event_type == "content_block_start":
                    block = _response_field(event, "content_block", {}) or {}
                    acc.start_block(str(_response_field(block, "type", "text") or "text"))
                elif event_type == "content_block_delta":
                    delta = _response_field(event, "delta", {}) or {}
                    delta_type = str(_response_field(delta, "type", "") or "")
                    if delta_type == "thinking_delta" or hasattr(delta, "thinking") or (
                        isinstance(delta, dict) and "thinking" in delta
                    ):
                        acc.current_thinking.append(str(_response_field(delta, "thinking", "") or ""))
                    elif delta_type == "text_delta" or hasattr(delta, "text") or (
                        isinstance(delta, dict) and "text" in delta
                    ):
                        acc.current_text.append(str(_response_field(delta, "text", "") or ""))
                elif event_type == "content_block_stop":
                    acc.flush_block()
                elif event_type == "message_delta":
                    delta = _response_field(event, "delta", {}) or {}
                    stop = _response_field(delta, "stop_reason", None)
                    if stop:
                        acc.stop_reason = str(stop)
                    usage = _response_field(event, "usage", None)
                    if usage:
                        acc.usage = {**acc.usage, **_as_dict(usage)}
                elif event_type == "message_stop":
                    acc.flush_block()
            final = stream.get_final_message()
            if final is not None:
                acc.response_id = acc.response_id or str(_response_field(final, "id", "") or "")
                acc.stop_reason = str(_response_field(final, "stop_reason", acc.stop_reason) or acc.stop_reason)
                usage = _response_field(final, "usage", None)
                if usage:
                    acc.usage = _as_dict(usage)
                if not acc.raw_blocks:
                    acc.raw_blocks = [_as_dict(block) for block in (_response_field(final, "content", []) or [])]
        if not acc.raw_blocks and (acc.text_parts or acc.thinking_parts):
            for thinking in acc.thinking_parts:
                acc.raw_blocks.append({"type": "thinking", "thinking": thinking})
            for text in acc.text_parts:
                acc.raw_blocks.append({"type": "text", "text": text})
        return self._to_llm_response(
            raw_blocks=acc.raw_blocks,
            stop_reason=acc.stop_reason,
            response_id=acc.response_id,
            usage_obj=acc.usage,
            streamed=True,
        )

    def _to_llm_response(
        self,
        *,
        raw_blocks: List[Dict[str, Any]],
        stop_reason: str,
        response_id: str,
        usage_obj: Any,
        streamed: bool,
    ) -> LLMResponse:
        blocks = normalize_content_blocks(raw_blocks, api_family=LLMApiFamily.ANTHROPIC_MESSAGES)
        content = "".join(block.text for block in blocks if block.type == "text")
        thinking = "\n".join(block.text for block in blocks if block.type == "reasoning") or None
        usage = LLMUsage(
            input_tokens=int(_response_field(usage_obj, "input_tokens", 0) or 0),
            output_tokens=int(_response_field(usage_obj, "output_tokens", 0) or 0),
            cache_read_tokens=int(_response_field(usage_obj, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(_response_field(usage_obj, "cache_creation_input_tokens", 0) or 0),
            reasoning_tokens=sum(len(block.text.split()) for block in blocks if block.type == "reasoning"),
        )
        costs = cost_breakdown_usd(
            model_name=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_write_tokens,
        )
        self._record_cache_telemetry(usage)
        return LLMResponse(
            content=content,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_write_tokens,
            finish_reason=stop_reason,
            raw_finish_reason=stop_reason,
            termination=normalize_termination(stop_reason, provider="qwen"),
            model=self.model,
            provider="qwen",
            api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
            response_id=response_id,
            content_blocks=blocks,
            usage=usage,
            thinking_content=thinking,
            input_cost_usd=float(costs["input_cost_usd"]),
            output_cost_usd=float(costs["output_cost_usd"]),
            cache_read_cost_usd=float(costs["cache_read_cost_usd"]),
            cache_creation_cost_usd=float(costs["cache_creation_cost_usd"]),
            total_cost_usd=float(costs["total_cost_usd"]),
            provider_metadata={
                "request_id": response_id,
                "raw_content_blocks": raw_blocks,
                "streamed": streamed,
                "cache_telemetry": self.cache_telemetry,
            },
        )

    def _record_cache_telemetry(self, usage: LLMUsage) -> None:
        self._cache_telemetry["calls"] = int(self._cache_telemetry.get("calls", 0)) + 1
        self._cache_telemetry["cache_read_tokens"] = int(self._cache_telemetry.get("cache_read_tokens", 0)) + usage.cache_read_tokens
        self._cache_telemetry["cache_creation_tokens"] = int(self._cache_telemetry.get("cache_creation_tokens", 0)) + usage.cache_write_tokens
        self._cache_telemetry["input_tokens"] = int(self._cache_telemetry.get("input_tokens", 0)) + usage.input_tokens
        self._cache_telemetry["output_tokens"] = int(self._cache_telemetry.get("output_tokens", 0)) + usage.output_tokens
        input_tokens = max(1, int(self._cache_telemetry["input_tokens"]))
        hit_ratio = float(self._cache_telemetry["cache_read_tokens"]) / float(input_tokens)
        self._cache_telemetry["cache_hit_ratio"] = round(hit_ratio, 4)
        warn_threshold = float(((self._cfg.get("caching") or {}).get("cache_monitor") or {}).get("warn_threshold_cache_hit_ratio", 0.5))
        if (
            bool(((self._cfg.get("caching") or {}).get("cache_monitor") or {}).get("enabled", True))
            and self._cache_telemetry["calls"] >= 2
            and hit_ratio < warn_threshold
        ):
            logger.warning(
                "[QWEN-CACHE] hit ratio %.2f below threshold %.2f (read=%d input=%d)",
                hit_ratio,
                warn_threshold,
                self._cache_telemetry["cache_read_tokens"],
                self._cache_telemetry["input_tokens"],
            )


def _response_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"type": "provider_block", "value": value}
