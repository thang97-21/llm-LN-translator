"""Lossless normalization for native Anthropic Messages API payloads."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.Deepseek.common.llm_types import (
    LLMApiFamily,
    LLMContentBlock,
    LLMResponse,
    LLMUsage,
    normalize_termination,
)
from src.Deepseek.common.token_telemetry import cost_breakdown_usd

logger = logging.getLogger(__name__)


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
    # A `text` block immediately preceding a `server_tool_use` call is the
    # executor's own pre-consult scoping sentence ("I'll consult the advisor
    # on..."), not chapter prose -- confirmed reproducing at effort="high" with
    # the ratified soft escalation wording (Run 11), the exact configuration
    # previously believed safe after one clean sample (Run 8). This is a
    # required filter, not optional hardening -- see
    # docs/anthropic-advisor-mode-spec.md §7.3.
    leaked_indices = {
        i for i, block in enumerate(raw_content)
        if isinstance(block, dict) and block.get("type") == "text"
        and i + 1 < len(raw_content)
        and isinstance(raw_content[i + 1], dict)
        and raw_content[i + 1].get("type") == "server_tool_use"
    }
    if leaked_indices:
        logger.warning(
            "[ANTHROPIC] %d text block(s) immediately precede a server_tool_use call; "
            "excluding from visible_text as a likely pre-consult scoping fragment, not chapter prose",
            len(leaked_indices),
        )
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
    visible_text = "".join(
        block.text for i, block in enumerate(blocks)
        if block.type == "text" and i not in leaked_indices
    )
    thinking_text = "\n".join(block.text for block in blocks if block.type == "reasoning" and block.text) or None

    provider_metadata: Dict[str, Any] = {
        "raw_response": raw,
        "raw_content": raw_content,
        "streamed": streamed,
        "stop_reason": stop_reason,
        "cache_creation": as_dict(usage_raw.get("cache_creation") or {}),
    }
    # The advisor sub-inference is billed at its OWN model's rate, tracked
    # separately from the executor's usage in usage.iterations (type:
    # "advisor_message"). Left unread, _log_usage would attribute the
    # advisor's entire spend to response.model (the executor) at the
    # executor's rate -- silently wrong billing the moment advisor mode ships.
    iterations = usage_raw.get("iterations") or []
    advisor_iterations = [it for it in iterations if str(as_dict(it).get("type")) == "advisor_message"]
    if advisor_iterations:
        provider_metadata["advisor_usage"] = [
            {
                "model": as_dict(it).get("model"),
                "input_tokens": _as_int(as_dict(it).get("input_tokens")),
                "output_tokens": _as_int(as_dict(it).get("output_tokens")),
                # Currently always 0 -- client.py's advisor tool definition
                # never sends the `caching` field Anthropic's advisor tool
                # accepts, so advisor.caching.enabled is a no-op today.
                # Extracted anyway so a future fix doesn't silently drop cost
                # once that wiring exists.
                "cache_read_tokens": _as_int(as_dict(it).get("cache_read_input_tokens")),
                "cache_creation_tokens": _as_int(as_dict(it).get("cache_creation_input_tokens")),
            }
            for it in advisor_iterations
        ]

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
        provider_metadata=provider_metadata,
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
        elif item_type == "server_tool_use":
            # The executor's call into a server-side tool (advisor, web_search).
            # input is always {} -- the server builds the tool's own view from
            # the transcript; nothing the executor puts here is used.
            blocks.append(
                LLMContentBlock(
                    type="tool_call",
                    block_id=str(payload.get("id") or ""),
                    name=str(payload.get("name") or ""),
                    arguments=payload.get("input"),
                    payload=payload,
                )
            )
        elif item_type == "advisor_tool_result":
            # A distinct type, not reused "tool_call"/"tool_result" -- an
            # advisor consult isn't interchangeable with an ordinary tool
            # result for any caller branching on type (see _log_usage below,
            # which needs to find advisor calls without re-parsing payload).
            # `text` is empty for the encrypted advisor_redacted_result variant
            # (Fable 5.1 / Mythos 5.1 / Opus 5 / Fable 5 / Mythos 5 advisors);
            # populated for the plaintext advisor_result variant (e.g. Opus 4.8).
            result = as_dict(payload.get("content") or {})
            blocks.append(
                LLMContentBlock(
                    type="advisor_result",
                    block_id=str(payload.get("tool_use_id") or ""),
                    text=str(result.get("text") or ""),
                    payload=payload,
                )
            )
        elif item_type == "web_search_tool_result":
            # content is a list of results on success, or a single
            # web_search_tool_result_error object on failure -- never assume
            # the list shape. `text` here is a thin summary (result titles)
            # for quick inspection only; a chapter that needs to carry
            # citations forward would read them from `payload`, not `text`.
            raw_results = payload.get("content")
            if isinstance(raw_results, list):
                titles = [str(as_dict(r).get("title") or "") for r in raw_results]
                text = "\n".join(t for t in titles if t)
            else:
                text = ""
            blocks.append(
                LLMContentBlock(
                    type="web_search_result",
                    block_id=str(payload.get("tool_use_id") or ""),
                    text=text,
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
    # advisor / web_search server-tool blocks (advisor-tool-2026-03-01).
    # server_tool_use also carries a response-only `caller` field (observed
    # None in every archived dry-run response) -- excluded here per this
    # file's existing policy: only fields the API accepts on an INBOUND
    # block survive replay.
    "server_tool_use": {"type", "id", "name", "input", "cache_control"},
    "advisor_tool_result": {"type", "tool_use_id", "content"},
    # web_search_tool_result.content carries each result's `encrypted_content`,
    # which must round-trip byte-exact on a later turn (the server decrypts it
    # server-side) -- the field-level allowlist already covers this since
    # `content` is kept whole, not trimmed further.
    "web_search_tool_result": {"type", "tool_use_id", "content"},
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
