"""OpenAI-native Responses client for the Phase 2 literary translator."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from src.Deepseek.common.llm_types import LLMApiFamily, LLMResponse
from src.Deepseek.common.chapter_signals import normalize_reasoning_effort
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.OpenAI.config import get_openai_config
from src.OpenAI.errors import RetryPolicy, call_with_retry, classify_exception
from src.OpenAI.prompt_loader import prompt_profile_version
from src.OpenAI.response import as_dict, response_to_llm_response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenAICapabilities:
    provider_name: str = "openai"
    api_family: LLMApiFamily = LLMApiFamily.OPENAI_RESPONSES
    supports_reasoning: bool = True
    supports_explicit_caching: bool = True
    supports_streaming: bool = True
    supports_local_conversation_replay: bool = True
    context_window: int = 1_050_000
    max_input_tokens: int = 922_000
    max_output_tokens: int = 128_000


OPENAI_CAPABILITIES = OpenAICapabilities()


class OpenAIClient:
    """Provider-isolated client for the native Responses protocol.

    The client returns MTLS's shared ``LLMResponse`` boundary object, while
    retaining raw ``response.output`` in metadata for local stateless replay.
    """

    CAPABILITIES = OPENAI_CAPABILITIES

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        dry_run: bool = False,
        config: Optional[Dict[str, Any]] = None,
    ):
        cfg = copy.deepcopy(config if config is not None else get_openai_config())
        self._cfg = cfg
        self.model = model or str(cfg.get("model", "gpt-6-sol"))
        self.prompt_profile = prompt_profile_version(self.model)
        self.api_key_env = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
        self.api_key = api_key or os.getenv(self.api_key_env)
        if not self.api_key and not dry_run:
            raise ValueError(f"OpenAI API key not found. Set {self.api_key_env} or pass api_key explicitly.")
        self.base_url = (base_url or str(cfg.get("endpoint", "https://api.openai.com/v1"))).rstrip("/")
        self.organization = _configured_env(cfg, "organization_env")
        self.project = _configured_env(cfg, "project_env")
        self._client = None
        if not dry_run:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ImportError("OpenAIClient requires the OpenAI SDK (pip install openai).") from exc
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                organization=self.organization,
                project=self.project,
                timeout=float(cfg.get("http_timeout_seconds", 600)),
                max_retries=0,
            )
        self.conversation_manager = None
        self._cache_telemetry: Dict[str, Any] = {
            "calls": 0,
            "repeat_calls": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "ordinary_input_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "expected_prefix_tokens": 0,
            "breakpoint_opportunities": 0,
            "breakpoint_successes": 0,
            "expected_prefix_opportunity_tokens": 0,
            "recovered_prefix_tokens": 0,
            "breakpoint_success_rate": 0.0,
            "prefix_recovery": 0.0,
            "total_cache_coverage": 0.0,
            # Kept as a compatibility alias for existing telemetry consumers.
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

    def attach_conversation(self, *, work_dir, volume_id: str, conversation_config: Optional[Dict[str, Any]] = None):
        from src.OpenAI.conversation import OpenAIConversationManager

        self.conversation_manager = OpenAIConversationManager(
            work_dir, volume_id, self.model, self.base_url, conversation_config or {}
        )
        return self.conversation_manager

    @property
    def cache_telemetry(self) -> Dict[str, Any]:
        return copy.deepcopy(self._cache_telemetry)

    def build_request(
        self,
        *,
        prompt: str,
        system_instruction: str,
        input_items: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
        stream: Optional[bool] = None,
        configuration_update: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build the wire-level Responses body without executing it.

        The synchronous and Batch routes must use the same request shape. The
        SDK accepts ``extra_body`` as an escape hatch, but OpenAI Batch receives
        a raw JSONL body, so its prompt-cache options are emitted as the
        documented top-level ``prompt_cache_options`` field here.
        """
        cfg = self._cfg
        generation_cfg = cfg.get("generation", {}) or {}
        reasoning_cfg = cfg.get("reasoning", {}) or {}
        caching_cfg = cfg.get("caching", {}) or {}
        streaming_cfg = cfg.get("streaming", {}) or {}
        conversation_cfg = cfg.get("conversation", {}) or {}

        base_input = _base_input(system_instruction, prompt)
        request_input = base_input if dry_run else (input_items or base_input)
        mode = str(reasoning_cfg.get("mode", "standard") or "standard").lower()
        if configuration_update and _is_gpt6_model(self.model) and mode == "standard":
            request_input = _insert_configuration_update(request_input, configuration_update)
        elif configuration_update:
            if not _is_gpt6_model(self.model):
                logger.warning(
                    "[OPENAI-CONFIGURATION] ignoring configuration_update for non-GPT-6 model %s",
                    self.model,
                )
            else:
                logger.warning(
                    "[OPENAI-CONFIGURATION] ignoring configuration_update for GPT-6 mode=%s; "
                    "standard mode is required",
                    mode,
                )
        cache_mode = str(caching_cfg.get("mode", "explicit") or "explicit").lower()
        if cache_mode == "explicit":
            request_input = _with_explicit_cache_breakpoint(request_input)
        effort = _reasoning_effort(self.model, reasoning_cfg.get("effort", "max"))
        request: Dict[str, Any] = {
            "model": self.model,
            "input": request_input,
            "store": bool(conversation_cfg.get("store", False)),
            "max_output_tokens": int(generation_cfg.get("max_output_tokens", 128_000) or 128_000),
            "service_tier": str(generation_cfg.get("service_tier", "default") or "default"),
            "reasoning": {
                "mode": str(reasoning_cfg.get("mode", "standard") or "standard"),
                "effort": effort,
                "context": str(reasoning_cfg.get("context", "all_turns") or "all_turns"),
            },
            "text": {
                "verbosity": str(((generation_cfg.get("text") or {}).get("verbosity", "high") or "high")),
            },
        }
        if cache_mode in {"explicit", "implicit"}:
            request["prompt_cache_key"] = _prompt_cache_key(self.model, system_instruction)
            request["prompt_cache_options"] = {
                "mode": cache_mode,
                "ttl": str(caching_cfg.get("ttl", "30m") or "30m"),
            }
        summary = reasoning_cfg.get("summary", "auto")
        if summary:
            request["reasoning"]["summary"] = str(summary)
        if bool(reasoning_cfg.get("include_encrypted_content", True)):
            request["include"] = ["reasoning.encrypted_content"]
        return request

    def generate(
        self,
        *,
        prompt: str,
        system_instruction: str,
        input_items: Optional[List[Dict[str, Any]]] = None,
        dry_run: bool = False,
        stream: Optional[bool] = None,
        configuration_update: Optional[str] = None,
    ) -> LLMResponse:
        """Build and execute a Responses request, or render it without a key."""
        cfg = self._cfg
        request = self.build_request(
            prompt=prompt,
            system_instruction=system_instruction,
            input_items=input_items,
            dry_run=dry_run,
            stream=stream,
            configuration_update=configuration_update,
        )
        streaming_cfg = cfg.get("streaming", {}) or {}
        use_stream = bool(streaming_cfg.get("enabled", True) if stream is None else stream)
        request_for_meta = {**request, "stream": use_stream}
        if dry_run:
            return LLMResponse(
                content="[DRY RUN]",
                model=self.model,
                provider="openai",
                api_family=LLMApiFamily.OPENAI_RESPONSES,
                provider_metadata={
                    "dry_run": True,
                    "payload": request_for_meta,
                    "prompt_profile": self.prompt_profile,
                },
            )

        retry_cfg = cfg.get("retry", {}) or {}
        policy = RetryPolicy(
            max_retries=int(retry_cfg.get("max_retries", 5) or 5),
            base_delay_ms=int(retry_cfg.get("base_delay_ms", 500) or 500),
            max_delay_ms=int(retry_cfg.get("max_delay_ms", 32_000) or 32_000),
            jitter_factor=float(retry_cfg.get("jitter_factor", 0.25) or 0.25),
            max_429_retries=int(retry_cfg.get("max_429_retries", 3) or 3),
        )

        def invoke() -> LLMResponse:
            try:
                response = self._generate_streaming(request) if use_stream else self._generate_blocking(request)
                self._record_cache_telemetry(response)
                response.provider_metadata["cache_telemetry"] = self.cache_telemetry
                response.provider_metadata["prompt_profile"] = self.prompt_profile
                return response
            except Exception as exc:  # SDK failures need one provider-neutral boundary
                raise classify_exception(exc) from exc

        return call_with_retry(
            invoke,
            policy,
            on_retry=lambda attempt, err, delay: logger.warning(
                "[OPENAI-RETRY] attempt=%d delay=%.2fs error=%s", attempt, delay, err
            ),
        )

    def _generate_blocking(self, request: Dict[str, Any]) -> LLMResponse:
        return response_to_llm_response(self._client.responses.create(**_sdk_request(request)), model=self.model, streamed=False)

    def _generate_streaming(self, request: Dict[str, Any]) -> LLMResponse:
        stream = self._client.responses.create(**_sdk_request(request), stream=True)
        final_response = None
        text_parts: List[str] = []
        for event in stream:
            event_raw = as_dict(event)
            event_type = str(event_raw.get("type") or "")
            if event_type == "response.output_text.delta":
                text_parts.append(str(event_raw.get("delta") or ""))
            if event_type in {"response.completed", "response.incomplete", "response.failed"}:
                final_response = event_raw.get("response") or getattr(event, "response", None)
        if final_response is None and hasattr(stream, "get_final_response"):
            final_response = stream.get_final_response()
        if final_response is None:
            final_response = {
                "model": self.model,
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]}],
            }
        return response_to_llm_response(final_response, model=self.model, streamed=True)

    def _record_cache_telemetry(self, response: LLMResponse) -> None:
        """Separate fixed-breakpoint health from total input cost coverage."""
        usage = response.usage
        telemetry = self._cache_telemetry
        prior_calls = int(telemetry["calls"])
        cache_read = max(0, int(usage.cache_read_tokens))
        cache_write = max(0, int(usage.cache_write_tokens))
        input_tokens_this_call = max(0, int(usage.input_tokens))
        ordinary_input = max(0, input_tokens_this_call - cache_read - cache_write)

        telemetry["calls"] = prior_calls + 1
        telemetry["repeat_calls"] = max(0, int(telemetry["calls"]) - 1)
        telemetry["cache_read_tokens"] = int(telemetry["cache_read_tokens"]) + cache_read
        telemetry["cache_write_tokens"] = int(telemetry["cache_write_tokens"]) + cache_write
        telemetry["ordinary_input_tokens"] = int(telemetry["ordinary_input_tokens"]) + ordinary_input
        telemetry["input_tokens"] = int(telemetry["input_tokens"]) + input_tokens_this_call
        telemetry["output_tokens"] = int(telemetry["output_tokens"]) + int(usage.output_tokens)

        expected_prefix = int(telemetry["expected_prefix_tokens"])
        if expected_prefix <= 0:
            # The cold explicit write is authoritative. Cache read is the
            # fallback for SDK/API variants that do not report write tokens.
            expected_prefix = cache_write or cache_read
            telemetry["expected_prefix_tokens"] = expected_prefix

        cache_cfg = self._cfg.get("caching", {}) or {}
        monitor = cache_cfg.get("cache_monitor", {}) or {}
        recovery_threshold = float(monitor.get("warn_threshold_prefix_recovery", 0.90) or 0.90)
        if prior_calls >= 1 and expected_prefix > 0:
            recovered = min(cache_read, expected_prefix)
            recovery = recovered / float(expected_prefix)
            telemetry["breakpoint_opportunities"] = int(telemetry["breakpoint_opportunities"]) + 1
            telemetry["expected_prefix_opportunity_tokens"] = (
                int(telemetry["expected_prefix_opportunity_tokens"]) + expected_prefix
            )
            telemetry["recovered_prefix_tokens"] = int(telemetry["recovered_prefix_tokens"]) + recovered
            if recovery >= recovery_threshold:
                telemetry["breakpoint_successes"] = int(telemetry["breakpoint_successes"]) + 1

        opportunities = int(telemetry["breakpoint_opportunities"])
        expected_total = int(telemetry["expected_prefix_opportunity_tokens"])
        breakpoint_success_rate = (
            int(telemetry["breakpoint_successes"]) / float(opportunities) if opportunities else 0.0
        )
        prefix_recovery = (
            int(telemetry["recovered_prefix_tokens"]) / float(expected_total) if expected_total else 0.0
        )
        total_input = max(1, int(telemetry["input_tokens"]))
        total_cache_coverage = int(telemetry["cache_read_tokens"]) / float(total_input)
        telemetry["breakpoint_success_rate"] = round(breakpoint_success_rate, 4)
        telemetry["prefix_recovery"] = round(prefix_recovery, 4)
        telemetry["total_cache_coverage"] = round(total_cache_coverage, 4)
        telemetry["cache_hit_ratio"] = telemetry["total_cache_coverage"]

        baseline = cost_breakdown_usd(
            model_name=response.model or self.model,
            input_tokens=input_tokens_this_call,
        )
        baseline_input_cost = float(baseline["input_cost_usd"])
        actual_input_cost = (
            float(response.input_cost_usd)
            + float(response.cache_read_cost_usd)
            + float(response.cache_creation_cost_usd)
        )
        economics = telemetry["cache_economics"]
        economics["cache_read_tokens"] = int(telemetry["cache_read_tokens"])
        economics["cache_write_tokens"] = int(telemetry["cache_write_tokens"])
        economics["ordinary_input_tokens"] = int(telemetry["ordinary_input_tokens"])
        total_writes = int(telemetry["cache_write_tokens"])
        economics["cache_read_to_write_ratio"] = (
            round(int(telemetry["cache_read_tokens"]) / float(total_writes), 4)
            if total_writes else 0.0
        )
        economics["baseline_input_cost_usd"] = round(
            float(economics["baseline_input_cost_usd"]) + baseline_input_cost, 8
        )
        economics["actual_input_cost_usd"] = round(
            float(economics["actual_input_cost_usd"]) + actual_input_cost, 8
        )
        economics["net_input_savings_usd"] = round(
            float(economics["baseline_input_cost_usd"])
            - float(economics["actual_input_cost_usd"]),
            8,
        )

        min_repeat_calls = max(1, int(monitor.get("min_repeat_calls_before_warning", 2) or 2))
        if bool(monitor.get("enabled", True)) and opportunities >= min_repeat_calls:
            success_threshold = float(
                monitor.get("warn_threshold_breakpoint_success_rate", 0.90) or 0.90
            )
            if breakpoint_success_rate < success_threshold:
                logger.warning(
                    "[OPENAI-CACHE] breakpoint success rate %.2f below threshold %.2f "
                    "(successes=%d opportunities=%d)",
                    breakpoint_success_rate,
                    success_threshold,
                    telemetry["breakpoint_successes"],
                    opportunities,
                )
            if prefix_recovery < recovery_threshold:
                logger.warning(
                    "[OPENAI-CACHE] prefix recovery %.2f below threshold %.2f "
                    "(recovered=%d expected=%d coverage=%.2f)",
                    prefix_recovery,
                    recovery_threshold,
                    telemetry["recovered_prefix_tokens"],
                    expected_total,
                    total_cache_coverage,
                )


def _configured_env(cfg: Dict[str, Any], key: str) -> Optional[str]:
    variable = str(cfg.get(key, "") or "").strip()
    return os.getenv(variable) if variable else None


def _sdk_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Keep Batch JSONL wire fields intact while supporting older SDK signatures.

    The installed SDK has ``extra_body`` but not a typed
    ``prompt_cache_options`` argument. The server receives the same top-level
    field either way; only the Python call signature differs.
    """
    prepared = dict(request)
    cache_options = prepared.pop("prompt_cache_options", None)
    if cache_options is not None:
        prepared["extra_body"] = {"prompt_cache_options": cache_options}
    return prepared


def _prompt_cache_key(model: str, system_instruction: str) -> str:
    digest = hashlib.sha256(
        (str(model) + "\0" + str(system_instruction)).encode("utf-8")
    ).hexdigest()[:24]
    return f"mtls:{model}:{prompt_profile_version(model)}:{digest}"


def _reasoning_effort(model: str, configured: Any) -> str:
    """Map unsupported effort values to a documented GPT-6 effort."""
    return normalize_reasoning_effort(model, configured or "max")


def _is_gpt6_model(model: str) -> bool:
    return str(model).strip().lower() in {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna"}


def configuration_update_item(effort: str) -> Dict[str, Any]:
    """Build the documented Responses input item for a reasoning update."""
    return {
        "type": "configuration_update",
        "reasoning": {"effort": str(effort).strip().lower()},
    }


def _insert_configuration_update(
    items: List[Dict[str, Any]],
    effort: str,
) -> List[Dict[str, Any]]:
    """Insert a GPT-6 update immediately before the next user message."""
    prepared = list(items)
    update = configuration_update_item(effort)
    user_index = next(
        (index for index in range(len(prepared) - 1, -1, -1) if prepared[index].get("role") == "user"),
        None,
    )
    if user_index is None:
        return [*prepared, update]

    if user_index > 0 and prepared[user_index - 1].get("type") == "configuration_update":
        previous_effort = (
            prepared[user_index - 1].get("reasoning", {}) or {}
        ).get("effort")
        if str(previous_effort).strip().lower() == update["reasoning"]["effort"]:
            return prepared
        raise ValueError(
            "configuration_update items cannot be adjacent before the same user message"
        )

    prepared.insert(user_index, update)
    return prepared


def _base_input(system_instruction: str, prompt: str) -> List[Dict[str, Any]]:
    return [
        {"role": "developer", "content": [{"type": "input_text", "text": system_instruction}]},
        {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    ]


def _with_explicit_cache_breakpoint(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mark the stable developer prefix without mutating the replay ledger."""
    prepared = list(items)
    for item_index, item in enumerate(prepared):
        if not isinstance(item, dict) or item.get("role") != "developer":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "input_text":
                continue
            prepared_block = dict(block)
            prepared_block["prompt_cache_breakpoint"] = {"mode": "explicit"}
            prepared_content = list(content)
            prepared_content[block_index] = prepared_block
            prepared_item = dict(item)
            prepared_item["content"] = prepared_content
            prepared[item_index] = prepared_item
            return prepared
    return prepared
