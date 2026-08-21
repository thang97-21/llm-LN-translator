"""Lossless normalization for native OpenAI Responses API payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMContentBlock,
    LLMResponse,
    LLMTermination,
    LLMUsage,
    normalize_termination,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd


def response_to_llm_response(response: Any, *, model: str, streamed: bool) -> LLMResponse:
    """Convert a Responses object without discarding replay-critical output items.

    A native response may contain output messages, encrypted reasoning items,
    reasoning summaries, tool calls, and refusal items. The local conversation
    ledger must retain the original ``output`` array so a stateless next request
    can replay valid reasoning state; only visible text becomes chapter prose.
    """
    raw = as_dict(response)
    raw_output = raw.get("output") or []
    blocks, refused = normalize_openai_output(raw_output)
    status = str(raw.get("status") or "completed")
    incomplete = as_dict(raw.get("incomplete_details") or {})
    raw_reason = str(incomplete.get("reason") or status)
    if refused:
        termination = LLMTermination.REFUSED
        raw_reason = "refused"
    elif status == "incomplete":
        termination = normalize_termination(raw_reason, provider="openai")
    elif status in {"failed", "cancelled", "expired"}:
        termination = LLMTermination.ERROR
    else:
        termination = normalize_termination(raw_reason, provider="openai")
        if termination is LLMTermination.UNKNOWN and status == "completed":
            termination = LLMTermination.COMPLETED

    usage_raw = as_dict(raw.get("usage") or {})
    input_details = as_dict(usage_raw.get("input_tokens_details") or {})
    output_details = as_dict(usage_raw.get("output_tokens_details") or {})
    usage = LLMUsage(
        input_tokens=_as_int(usage_raw.get("input_tokens")),
        output_tokens=_as_int(usage_raw.get("output_tokens")),
        cache_read_tokens=_as_int(input_details.get("cached_tokens")),
        cache_write_tokens=_as_int(input_details.get("cache_write_tokens")),
        reasoning_tokens=_as_int(output_details.get("reasoning_tokens")),
    )
    costs = cost_breakdown_usd(
        model_name=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_write_tokens,
        cache_creation_included_in_input=True,
    )
    visible_text = "".join(block.text for block in blocks if block.type == "text")
    reasoning_text = "\n".join(block.text for block in blocks if block.type == "reasoning") or None
    return LLMResponse(
        content=visible_text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_write_tokens,
        finish_reason=raw_reason,
        raw_finish_reason=raw_reason,
        termination=termination,
        model=str(raw.get("model") or model),
        provider="openai",
        api_family=LLMApiFamily.OPENAI_RESPONSES,
        response_id=str(raw.get("id") or ""),
        content_blocks=blocks,
        usage=usage,
        thinking_content=reasoning_text,
        input_cost_usd=float(costs["input_cost_usd"]),
        output_cost_usd=float(costs["output_cost_usd"]),
        cache_read_cost_usd=float(costs["cache_read_cost_usd"]),
        cache_creation_cost_usd=float(costs["cache_creation_cost_usd"]),
        total_cost_usd=float(costs["total_cost_usd"]),
        provider_metadata={
            "raw_response": raw,
            "raw_output": raw_output,
            "streamed": streamed,
            "status": status,
            "incomplete_details": incomplete,
        },
    )


def normalize_openai_output(raw_output: Any) -> tuple[List[LLMContentBlock], bool]:
    """Normalize visible and diagnostic content while retaining full payloads."""
    items = raw_output if isinstance(raw_output, list) else [raw_output]
    blocks: List[LLMContentBlock] = []
    refused = False
    for item in items:
        payload = as_dict(item)
        item_type = str(payload.get("type") or "").lower()
        if item_type == "message":
            for content_item in _as_list(payload.get("content")):
                content = as_dict(content_item)
                content_type = str(content.get("type") or "").lower()
                if content_type in {"output_text", "text"}:
                    blocks.append(LLMContentBlock(type="text", text=str(content.get("text") or ""), payload=content))
                elif content_type == "refusal":
                    refused = True
                    blocks.append(
                        LLMContentBlock(
                            type="provider_block",
                            text=str(content.get("refusal") or content.get("text") or ""),
                            payload=content,
                        )
                    )
                else:
                    blocks.append(LLMContentBlock(type="provider_block", payload=content))
        elif item_type == "reasoning":
            summaries = _as_list(payload.get("summary"))
            if summaries:
                for summary in summaries:
                    summary_payload = as_dict(summary)
                    blocks.append(
                        LLMContentBlock(
                            type="reasoning",
                            text=str(summary_payload.get("text") or summary_payload.get("summary") or ""),
                            payload={**payload, "summary_item": summary_payload},
                        )
                    )
            else:
                blocks.append(LLMContentBlock(type="reasoning", payload=payload))
        elif item_type in {"function_call", "tool_call"}:
            blocks.append(
                LLMContentBlock(
                    type="tool_call",
                    block_id=str(payload.get("call_id") or payload.get("id") or ""),
                    name=str(payload.get("name") or ""),
                    arguments=payload.get("arguments"),
                    payload=payload,
                )
            )
        else:
            blocks.append(LLMContentBlock(type="provider_block", payload=payload))
    return blocks, refused


def as_dict(value: Any) -> Dict[str, Any]:
    """Turn SDK data models into JSON-compatible dictionaries."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json")
        except TypeError:
            dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


def _as_list(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
