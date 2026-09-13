"""Provider-neutral response contract for every MTLS LLM transport.

Clients normalize wire-level responses here at the boundary.  Pipeline code can
therefore reason about one result shape regardless of whether the upstream API
uses Gemini ``generateContent``, Anthropic Messages, OpenAI Responses/chat
completions, or a compatible gateway such as DeepSeek, Kimi, or OpenRouter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LLMApiFamily(str, Enum):
    """Wire protocol used for the request, not a model-brand classification."""

    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    OPENAI_RESPONSES = "openai_responses"
    CUSTOM = "custom"


class LLMTermination(str, Enum):
    """Normalized terminal state for provider-independent control flow."""

    COMPLETED = "completed"
    MAX_OUTPUT = "max_output"
    SAFETY_BLOCKED = "safety_blocked"
    REFUSED = "refused"
    CONTEXT_LIMIT = "context_limit"
    TOOL_USE = "tool_use"
    PAUSED = "paused"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class LLMContentBlock:
    """A lossless, normalized provider output item.

    ``payload`` retains provider-specific fields that do not map cleanly yet,
    so adding a future endpoint never requires silently discarding content.
    """

    type: str
    text: str = ""
    block_id: str = ""
    name: str = ""
    arguments: Any = None
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMUsage:
    """Token accounting with cache traffic kept distinct from normal input."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        """Input tokens not served from a provider prompt cache."""
        return max(0, int(self.input_tokens) - int(self.cache_read_tokens))

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)


def normalize_termination(raw_reason: Any, *, provider: str = "") -> LLMTermination:
    """Map documented provider stop reasons to MTLS control-flow semantics."""
    value = str(raw_reason or "").strip().lower().replace("-", "_")
    if value in {"stop", "end_turn", "complete", "completed", "finished", "eos"}:
        return LLMTermination.COMPLETED
    if value in {"length", "max_tokens", "max_output_tokens", "max_completion_tokens", "incomplete"}:
        return LLMTermination.MAX_OUTPUT
    if value in {"content_filter", "safety", "safety_block", "prohibited_content", "blocked"}:
        return LLMTermination.SAFETY_BLOCKED
    if value in {"refusal", "refused"}:
        return LLMTermination.REFUSED
    if value in {"model_context_window_exceeded", "context_length_exceeded", "context_limit"}:
        return LLMTermination.CONTEXT_LIMIT
    if value in {"tool_use", "tool_calls", "function_call"}:
        return LLMTermination.TOOL_USE
    if value in {"pause_turn"}:
        # A server-side tool (advisor, web_search) is mid-call and the turn is
        # incomplete -- distinct from TOOL_USE, which is a COMPLETED turn that
        # ended on a client-side tool call. Resumed by resending the unchanged
        # assistant message; see src/Anthropic/agent.py::_resolve_pauses.
        return LLMTermination.PAUSED
    if value in {"error", "batch_error", "failed", "cancelled", "expired"}:
        return LLMTermination.ERROR
    return LLMTermination.UNKNOWN


def normalize_content_blocks(
    raw_blocks: Any,
    *,
    api_family: LLMApiFamily | str,
) -> List[LLMContentBlock]:
    """Losslessly normalize common output block shapes from supported APIs.

    Unknown block types are preserved as ``provider_block`` instead of being
    dropped.  This is the forward-compatibility rule for a new endpoint.
    """
    if raw_blocks is None:
        return []
    if isinstance(raw_blocks, str):
        return [LLMContentBlock(type="text", text=raw_blocks)] if raw_blocks else []
    items = raw_blocks if isinstance(raw_blocks, list) else [raw_blocks]
    family = str(getattr(api_family, "value", api_family))
    normalized: List[LLMContentBlock] = []
    for item in items:
        if not isinstance(item, dict):
            normalized.append(LLMContentBlock(type="provider_block", payload={"value": item}))
            continue
        item_type = str(item.get("type") or "").lower()
        if family == LLMApiFamily.OPENAI_RESPONSES.value and item_type == "message":
            normalized.extend(normalize_content_blocks(item.get("content", []), api_family=api_family))
            continue
        if item_type in {"text", "output_text", "input_text"}:
            normalized.append(LLMContentBlock(type="text", text=str(item.get("text") or item.get("content") or ""), payload=dict(item)))
        elif item_type in {"thinking", "reasoning", "reasoning_summary"}:
            normalized.append(LLMContentBlock(type="reasoning", text=str(item.get("thinking") or item.get("text") or item.get("summary") or ""), payload=dict(item)))
        elif item_type in {"tool_use", "tool_call", "function_call"}:
            normalized.append(LLMContentBlock(
                type="tool_call", block_id=str(item.get("id") or item.get("call_id") or ""),
                name=str(item.get("name") or item.get("function", {}).get("name") or ""),
                arguments=item.get("input", item.get("arguments", item.get("function", {}).get("arguments"))),
                payload=dict(item),
            ))
        elif item_type in {"tool_result", "function_call_output"}:
            normalized.append(LLMContentBlock(type="tool_result", block_id=str(item.get("tool_use_id") or item.get("call_id") or ""), text=str(item.get("content") or item.get("output") or ""), payload=dict(item)))
        elif "text" in item:
            block_type = "reasoning" if bool(item.get("thought")) else "text"
            normalized.append(LLMContentBlock(type=block_type, text=str(item.get("text") or ""), payload=dict(item)))
        else:
            normalized.append(LLMContentBlock(type="provider_block", payload=dict(item)))
    return normalized


@dataclass
class LLMResponse:
    """Canonical LLM result shared by all MTLS providers and endpoint families.

    The flat token and cost fields intentionally remain available because the
    translation log is an established serialized contract.  ``usage`` and the
    structured content/diagnostic fields are the canonical extension surface.
    ``__post_init__`` synchronizes both representations while MTLS migrates
    existing reporters without data loss.
    """

    content: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    model: str = ""
    provider: str = "unknown"
    api_family: LLMApiFamily | str = LLMApiFamily.CUSTOM
    response_id: str = ""
    raw_finish_reason: str = ""
    termination: LLMTermination | str = LLMTermination.UNKNOWN
    content_blocks: List[LLMContentBlock] = field(default_factory=list)
    tool_calls: List[LLMContentBlock] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    # Compatibility with persisted cost/audit consumers.  These are transport
    # facts, not Gemini-specific fields, and will be folded into value objects
    # once the serialized audit schema is versioned.
    cached_tokens: int = 0
    thinking_content: Optional[str] = None
    cache_creation_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    cache_read_cost_usd: float = 0.0
    cache_creation_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    batch_pricing: bool = False
    fast_mode_pricing: bool = False
    declared_params: Any = None
    tool_calls_made: List[str] = field(default_factory=list)
    tool_call_count: int = 0
    qc_self_report: Any = None
    consistency_anchor_report: Any = None
    structural_constraints: List[Any] = field(default_factory=list)
    cost_audit: Dict[str, Any] = field(default_factory=dict)
    conversation_telemetry: Dict[str, Any] = field(default_factory=dict)
    conversation_pending_turn: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.provider == "unknown":
            audit_provider = self.cost_audit.get("provider") if isinstance(self.cost_audit, dict) else None
            self.provider = str(audit_provider or self.provider or "unknown")
        self.provider = str(self.provider or "unknown").strip().lower()
        self.api_family = self._coerce_api_family(self.api_family)
        if self.api_family is LLMApiFamily.CUSTOM:
            inferred_families = {
                "anthropic": LLMApiFamily.ANTHROPIC_MESSAGES,
                "gemini": LLMApiFamily.GEMINI_GENERATE_CONTENT,
                "openai": LLMApiFamily.OPENAI_RESPONSES,
                "openrouter": LLMApiFamily.OPENAI_CHAT_COMPLETIONS,
                "kimi": LLMApiFamily.ANTHROPIC_MESSAGES,
                "moonshot": LLMApiFamily.ANTHROPIC_MESSAGES,
            }
            self.api_family = inferred_families.get(self.provider, self.api_family)
        self.raw_finish_reason = str(self.raw_finish_reason or self.finish_reason or "")
        self.finish_reason = str(self.finish_reason or self.raw_finish_reason or "")
        self.termination = self._coerce_termination(self.termination)
        if self.termination is LLMTermination.UNKNOWN:
            self.termination = normalize_termination(self.raw_finish_reason, provider=self.provider)
        self.input_tokens = max(0, int(self.input_tokens or 0))
        self.output_tokens = max(0, int(self.output_tokens or 0))
        self.cached_tokens = max(0, int(self.cached_tokens or 0))
        self.cache_creation_tokens = max(0, int(self.cache_creation_tokens or 0))
        if self.usage.input_tokens == 0:
            self.usage.input_tokens = self.input_tokens
        if self.usage.output_tokens == 0:
            self.usage.output_tokens = self.output_tokens
        if self.usage.cache_read_tokens == 0:
            self.usage.cache_read_tokens = self.cached_tokens
        if self.usage.cache_write_tokens == 0:
            self.usage.cache_write_tokens = self.cache_creation_tokens
        self.cached_tokens = self.usage.cache_read_tokens
        self.cache_creation_tokens = self.usage.cache_write_tokens
        if not self.content_blocks and self.content:
            self.content_blocks = [LLMContentBlock(type="text", text=self.content)]
        if self.content_blocks and not self.content:
            self.content = "".join(block.text for block in self.content_blocks if block.type == "text")
        if not self.tool_calls and self.tool_calls_made:
            self.tool_calls = [LLMContentBlock(type="tool_call", name=name) for name in self.tool_calls_made]
        if self.tool_call_count == 0:
            self.tool_call_count = len(self.tool_calls or self.tool_calls_made)

    @staticmethod
    def _coerce_api_family(value: LLMApiFamily | str) -> LLMApiFamily | str:
        try:
            return value if isinstance(value, LLMApiFamily) else LLMApiFamily(str(value))
        except ValueError:
            return str(value or LLMApiFamily.CUSTOM.value)

    @staticmethod
    def _coerce_termination(value: LLMTermination | str) -> LLMTermination:
        try:
            return value if isinstance(value, LLMTermination) else LLMTermination(str(value))
        except ValueError:
            return LLMTermination.UNKNOWN

    @property
    def cache_read_tokens(self) -> int:
        return self.usage.cache_read_tokens

    @property
    def cache_write_tokens(self) -> int:
        return self.usage.cache_write_tokens
