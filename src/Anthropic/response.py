"""Lossless normalization for native Anthropic Messages API payloads."""

from __future__ import annotations

from typing import Any, Dict, List

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMContentBlock,
    LLMResponse,
    LLMUsage,
    normalize_termination,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd


def response_to_llm_response(
    response: Any, *, model: str, streamed: bool, batch: bool = False, cache_ttl: str = "5m"
) -> LLMResponse:
    """Convert a Messages response without discarding replay-critical content.

    ``thinking``/``redacted_thinking`` blocks must round-trip byte-exact in a
    multi-turn conversation (see Anthropic's "Preserving thinking blocks"),
    so the full raw ``content`` array is retained in ``provider_metadata``
    for the caller to echo back unmodified — never re-synthesized here.
    """
    raw = as_dict(response)
    raw_content = raw.get("content") or []
    blocks = normalize_anthropic_content(raw_content)
    stop_reason = str(raw.get("stop_reason") or "")
    termination = normalize_termination(stop_reason, provider="anthropic")

    usage_raw = as_dict(raw.get("usage") or {})
    usage = LLMUsage(
        input_tokens=_as_int(usage_raw.get("input_tokens")),
        output_tokens=_as_int(usage_raw.get("output_tokens")),
        cache_read_tokens=_as_int(usage_raw.get("cache_read_input_tokens")),
        cache_write_tokens=_as_int(usage_raw.get("cache_creation_input_tokens")),
    )
    # batch=True halves every rate: a Message Batches result is billed at
    # 50% of the synchronous price, and a route that exists to capture that
    # discount must not report the undiscounted figure.
    costs = cost_breakdown_usd(
        model_name=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_write_tokens,
        # The Messages API's three token counts are disjoint: input_tokens
        # already excludes cache reads and cache writes. Without this the
        # fresh-token figure collapses to zero whenever the cached prefix is
        # larger than the chapter envelope - which is always.
        cache_read_included_in_input=False,
        cache_ttl=cache_ttl,
        batch=batch,
    )
    visible_text = "".join(block.text for block in blocks if block.type == "text")
    thinking_text = "\n".join(block.text for block in blocks if block.type == "reasoning" and block.text) or None
    return LLMResponse(
        content=visible_text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_write_tokens,
        finish_reason=stop_reason,
        raw_finish_reason=stop_reason,
        termination=termination,
        model=str(raw.get("model") or model),
        provider="anthropic",
        api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
        response_id=str(raw.get("id") or ""),
        content_blocks=blocks,
        usage=usage,
        thinking_content=thinking_text,
        input_cost_usd=float(costs["input_cost_usd"]),
        output_cost_usd=float(costs["output_cost_usd"]),
        cache_read_cost_usd=float(costs["cache_read_cost_usd"]),
        cache_creation_cost_usd=float(costs["cache_creation_cost_usd"]),
        total_cost_usd=float(costs["total_cost_usd"]),
        batch_pricing=batch,
        provider_metadata={
            "raw_response": raw,
            "raw_content": raw_content,
            "streamed": streamed,
            "stop_reason": stop_reason,
            "cache_creation": as_dict(usage_raw.get("cache_creation") or {}),
        },
    )


def normalize_anthropic_content(raw_content: Any) -> List[LLMContentBlock]:
    """Normalize a Messages ``content`` array, preserving unknown block types."""
    items = raw_content if isinstance(raw_content, list) else ([raw_content] if raw_content else [])
    blocks: List[LLMContentBlock] = []
    for item in items:
        payload = as_dict(item)
        item_type = str(payload.get("type") or "").lower()
        if item_type == "text":
            blocks.append(LLMContentBlock(type="text", text=str(payload.get("text") or ""), payload=payload))
        elif item_type == "thinking":
            # `thinking` is empty when display="omitted" — same block shape,
            # billed the same, just no readable text. signature stays in payload.
            blocks.append(LLMContentBlock(type="reasoning", text=str(payload.get("thinking") or ""), payload=payload))
        elif item_type == "redacted_thinking":
            # No readable text at all — opaque `data` field only. Preserved
            # as a reasoning block with empty text so callers that filter on
            # type=="reasoning" don't silently drop it (the docs' own warning
            # about filtering block.type == "thinking" alone).
            blocks.append(LLMContentBlock(type="reasoning", text="", payload=payload))
        elif item_type == "tool_use":
            blocks.append(
                LLMContentBlock(
                    type="tool_call",
                    block_id=str(payload.get("id") or ""),
                    name=str(payload.get("name") or ""),
                    arguments=payload.get("input"),
                    payload=payload,
                )
            )
        else:
            blocks.append(LLMContentBlock(type="provider_block", payload=payload))
    return blocks


# The SDK's model_dump() serializes every field a response content block can
# carry, including response-only ones (e.g. a text block's `citations`,
# `parsed_output`). Anthropic's Messages API validates inbound `messages`
# strictly — replaying those response-only fields on a later turn is a hard
# 400 ("Extra inputs are not permitted"), not a soft ignore. Only fields the
# API actually accepts on an inbound block survive replay. Applied both when
# a turn is first stored (agent.py) and every time it's replayed
# (conversation.py) — the latter self-heals conversation ledgers already on
# disk from before this fix existed, with no manual data migration needed.
REPLAYABLE_BLOCK_FIELDS: Dict[str, set] = {
    "text": {"type", "text", "cache_control"},
    "thinking": {"type", "thinking", "signature"},
    "redacted_thinking": {"type", "data"},
    "tool_use": {"type", "id", "name", "input", "cache_control"},
}


def sanitize_replayable_block(block: Dict[str, Any]) -> Dict[str, Any]:
    allowed = REPLAYABLE_BLOCK_FIELDS.get(str(block.get("type") or ""))
    if allowed is None:
        return block
    return {key: value for key, value in block.items() if key in allowed}


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


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
