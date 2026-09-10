"""Z.AI GLM-5.3 client over the OpenAI Chat Completions protocol."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMContentBlock,
    LLMResponse,
    LLMTermination,
    LLMUsage,
    normalize_termination,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.GLM.config import get_glm_config
from src.GLM.errors import RetryPolicy, call_with_retry, classify_exception, classify_finish_reason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GLMCapabilities:
    provider_name: str = "glm"
    api_family: LLMApiFamily = LLMApiFamily.OPENAI_CHAT_COMPLETIONS
    supports_thinking: bool = True
    supports_thinking_disable: bool = False
    supports_implicit_caching: bool = True
    supports_streaming: bool = True
    context_window: int = 1_000_000
    max_output_tokens: int = 131_072


GLM_CAPABILITIES = GLMCapabilities()


class GLMClient:
    CAPABILITIES = GLM_CAPABILITIES

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None, base_url: Optional[str] = None, dry_run: bool = False):
        cfg = get_glm_config()
        self._cfg = cfg
        self.model = str(model or cfg.get("model", "glm-5.3-flash"))
        if self.model not in {"glm-5.3", "glm-5.3-flash"}:
            logger.warning("[GLM] model %r is outside the verified GLM-5.3 profile", self.model)
        self.api_key_env = str(cfg.get("api_key_env", "ZAI_API_KEY"))
        self.api_key = api_key or os.getenv(self.api_key_env)
        if not self.api_key and not dry_run:
            raise ValueError(f"GLM API key not found. Set {self.api_key_env} or pass api_key explicitly.")
        self.base_url = str(base_url or cfg.get("endpoint", "https://api.z.ai/api/paas/v4")).rstrip("/")
        self._client = None
        if not dry_run:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ImportError("GLMClient requires the OpenAI SDK (pip install openai).") from exc
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(cfg.get("http_timeout_seconds", 600)),
                max_retries=0,
            )
        self.conversation_manager = None
        self._cache_telemetry: Dict[str, Any] = {"calls": 0, "cache_read_tokens": 0, "input_tokens": 0, "output_tokens": 0}

    def attach_conversation(self, *, work_dir, volume_id: str, conversation_config: Optional[Dict[str, Any]] = None):
        from src.GLM.conversation import GLMConversationManager

        self.conversation_manager = GLMConversationManager(work_dir, volume_id, self.model, self.base_url, conversation_config or {})
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
        reasoning_effort: Optional[str] = None,
    ) -> LLMResponse:
        cfg = self._cfg
        generation = cfg.get("generation", {}) or {}
        reasoning = cfg.get("reasoning", {}) or {}
        thinking = cfg.get("thinking", {}) or {}
        streaming = cfg.get("streaming", {}) or {}
        # Dry-run must be a truthful single-turn preview, never a stale ledger replay.
        request_messages = (
            [{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}]
            if dry_run
            else (messages or [{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}])
        )
        effort = str(reasoning_effort or reasoning.get("effort", thinking.get("effort", "high")) or "high").lower()
        if effort not in {"low", "high", "max"}:
            raise ValueError(f"GLM reasoning_effort must be one of low, high, max; got {effort!r}")
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "max_tokens": int(generation.get("max_output_tokens", 131072) or 131072),
            "temperature": float(generation.get("temperature", 1.0) or 1.0),
            "reasoning_effort": effort,
        }
        if generation.get("top_p") is not None:
            request["top_p"] = float(generation["top_p"])
        clear_thinking = thinking.get("clear_thinking", True)
        # These are Z.AI extensions to the OpenAI SDK schema. extra_body lets
        # supported and older openai package versions serialize them without
        # client-side validation rejecting the request before it reaches Z.AI.
        request["extra_body"] = {
            "thinking": {"type": "enabled", "clear_thinking": bool(clear_thinking)},
        }
        use_stream = bool(streaming.get("enabled", True) if stream is None else stream)
        request_for_meta = {**request, "stream": use_stream}
        extra_body = request_for_meta.pop("extra_body")
        request_for_meta.update(extra_body)
        if dry_run:
            return LLMResponse(
                content="[DRY RUN]",
                model=self.model,
                provider="glm",
                api_family=LLMApiFamily.OPENAI_CHAT_COMPLETIONS,
                provider_metadata={"dry_run": True, "payload": request_for_meta},
            )

        retry_cfg = cfg.get("retry", {}) or {}
        policy = RetryPolicy(
            max_retries=int(retry_cfg.get("max_retries", 5) or 5),
            base_delay_ms=int(retry_cfg.get("base_delay_ms", 500) or 500),
            max_delay_ms=int(retry_cfg.get("max_delay_ms", 32000) or 32000),
            jitter_factor=float(retry_cfg.get("jitter_factor", 0.25) or 0.25),
            max_429_retries=int(retry_cfg.get("max_429_retries", 3) or 3),
        )

        def invoke() -> LLMResponse:
            try:
                response = self._generate_streaming(request) if use_stream else self._generate_blocking(request)
                self._record_cache_telemetry(response.usage)
                response.provider_metadata["cache_telemetry"] = self.cache_telemetry
                return response
            except Exception as exc:
                raise classify_exception(exc) from exc

        return call_with_retry(invoke, policy, on_retry=lambda attempt, err, delay: logger.warning(
            "[GLM-RETRY] attempt=%d delay=%.2fs error=%s", attempt, delay, err
        ))

    def _generate_blocking(self, request: Dict[str, Any]) -> LLMResponse:
        return self._to_llm_response(self._client.chat.completions.create(**request), streamed=False)

    def _generate_streaming(self, request: Dict[str, Any]) -> LLMResponse:
        stream = self._client.chat.completions.create(**request, stream=True)
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        usage: Dict[str, Any] = {}
        response_id = ""
        model = self.model
        finish_reason = "stop"
        for chunk in stream:
            raw = _as_dict(chunk)
            response_id = response_id or str(raw.get("id") or "")
            model = str(raw.get("model") or model)
            choices = raw.get("choices") or []
            if choices:
                choice = _as_dict(choices[0])
                finish_reason = str(choice.get("finish_reason") or finish_reason)
                delta = _as_dict(choice.get("delta") or {})
                text_parts.append(str(delta.get("content") or ""))
                reasoning_parts.append(str(delta.get("reasoning_content") or ""))
            if raw.get("usage"):
                usage = _as_dict(raw["usage"])
        return self._to_llm_response({
            "id": response_id,
            "model": model,
            "choices": [{"message": {"content": "".join(text_parts), "reasoning_content": "".join(reasoning_parts)}, "finish_reason": finish_reason}],
            "usage": usage,
        }, streamed=True)

    def _to_llm_response(self, response: Any, *, streamed: bool) -> LLMResponse:
        raw = _as_dict(response)
        choices = raw.get("choices") or []
        choice = _as_dict(choices[0]) if choices else {}
        message = _as_dict(choice.get("message") or {})
        finish_reason = str(choice.get("finish_reason") or "stop")
        refusal = classify_finish_reason(finish_reason)
        if refusal is not None:
            raise refusal
        content = _text_value(message.get("content"))
        reasoning_content = _text_value(message.get("reasoning_content"))
        blocks: List[LLMContentBlock] = []
        if reasoning_content:
            blocks.append(LLMContentBlock(type="reasoning", text=reasoning_content, payload={"reasoning_content": reasoning_content}))
        if content:
            blocks.append(LLMContentBlock(type="text", text=content, payload={"content": content}))
        for tool in message.get("tool_calls") or []:
            payload = _as_dict(tool)
            function = _as_dict(payload.get("function") or {})
            blocks.append(LLMContentBlock(type="tool_call", block_id=str(payload.get("id") or ""), name=str(function.get("name") or ""), arguments=function.get("arguments"), payload=payload))
        usage_raw = _as_dict(raw.get("usage") or {})
        details = _as_dict(usage_raw.get("prompt_tokens_details") or {})
        output_details = _as_dict(usage_raw.get("completion_tokens_details") or {})
        usage = LLMUsage(
            input_tokens=_as_int(usage_raw.get("prompt_tokens")),
            output_tokens=_as_int(usage_raw.get("completion_tokens")),
            cache_read_tokens=_as_int(details.get("cached_tokens")),
            reasoning_tokens=_as_int(output_details.get("reasoning_tokens")),
        )
        costs = cost_breakdown_usd(model_name=str(raw.get("model") or self.model), input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cache_read_tokens=usage.cache_read_tokens)
        termination = normalize_termination(finish_reason, provider="glm")
        if termination is LLMTermination.UNKNOWN and finish_reason == "stop":
            termination = LLMTermination.COMPLETED
        return LLMResponse(
            content=content,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens,
            finish_reason=finish_reason,
            raw_finish_reason=finish_reason,
            termination=termination,
            model=str(raw.get("model") or self.model),
            provider="glm",
            api_family=LLMApiFamily.OPENAI_CHAT_COMPLETIONS,
            response_id=str(raw.get("id") or ""),
            content_blocks=blocks,
            usage=usage,
            thinking_content=reasoning_content or None,
            input_cost_usd=float(costs["input_cost_usd"]),
            output_cost_usd=float(costs["output_cost_usd"]),
            cache_read_cost_usd=float(costs["cache_read_cost_usd"]),
            cache_creation_cost_usd=float(costs["cache_creation_cost_usd"]),
            total_cost_usd=float(costs["total_cost_usd"]),
            provider_metadata={"raw_response": raw, "streamed": streamed, "raw_message": message},
        )

    def _record_cache_telemetry(self, usage: LLMUsage) -> None:
        self._cache_telemetry["calls"] += 1
        self._cache_telemetry["cache_read_tokens"] += usage.cache_read_tokens
        self._cache_telemetry["input_tokens"] += usage.input_tokens
        self._cache_telemetry["output_tokens"] += usage.output_tokens
        self._cache_telemetry["cache_hit_ratio"] = round(self._cache_telemetry["cache_read_tokens"] / max(1, self._cache_telemetry["input_tokens"]), 4)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json")
        except TypeError:
            dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_value(item.get("text") if isinstance(item, dict) else item) for item in value)
    return str(value)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
