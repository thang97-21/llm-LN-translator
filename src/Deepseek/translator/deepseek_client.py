"""
DeepSeek V4 Pro client adapter.

Uses the Anthropic-compatible API format via the Anthropic SDK pointed at
DeepSeek's base URL.  Implements the same public interface as AnthropicClient
so that agent.py and chapter_processor.py require zero structural changes.

Key differences from AnthropicClient:
  - No cache_control markers (auto-prefix cache)
  - No beta headers, fast mode, or advisor tool
  - Anthropic-compatible thinking and output_config.effort
  - Preserve thinking blocks on assistant turns that performed tool_use (API requirement)
  - Strip thinking on non-tool assistant turns only
  - No batch API — raises NotImplementedError
  - DeepSeek V4 Pro pricing table
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.Deepseek.common.llm_types import LLMApiFamily, LLMContentBlock, LLMResponse, LLMUsage
from src.Deepseek.translator.deepseek_conversation import DeepSeekConversationManager
from src.Deepseek.translator.thinking_output import split_analysis_prelude_from_output

# ── Inlined provider-capabilities declaration (main-repo common/ module) ────
# Only the one capability instance this client actually declares — the full
# multi-provider registry (Anthropic/MiMo/Kimi/OpenAI/OpenRouter/Gemini) has
# no reader in a DeepSeek-only client.


@dataclass(frozen=True)
class ProviderCapabilities:
    """Frozen declaration of what a provider supports (see the main repo's
    provider-capabilities module for the full multi-provider doc)."""

    provider_name: str
    supports_tool_loop: bool
    supports_advisor_tool: bool
    supports_batch: bool
    supports_explicit_caching: bool
    supports_fast_mode: bool
    supports_thinking: bool
    supports_effort_routing: bool
    supports_beta_headers: bool
    supports_streaming: bool = True
    strip_thinking_between_turns: bool = False
    cost_estimator: Optional[Any] = field(default=None, compare=False)
    max_output_tokens: int = 128000
    context_window: int = 1000000


DEEPSEEK_CAPABILITIES = ProviderCapabilities(
    provider_name="deepseek",
    supports_tool_loop=False,
    supports_advisor_tool=False,
    supports_batch=False,
    supports_explicit_caching=False,
    supports_fast_mode=False,
    supports_thinking=True,
    supports_effort_routing=True,
    supports_beta_headers=False,
    supports_streaming=True,
    strip_thinking_between_turns=True,
    cost_estimator=None,
    max_output_tokens=384000,
    context_window=1000000,
)

# A top-level Markdown H1 (the chapter heading) — the credibility signal that a
# salvaged reasoning_content tail is really a chapter and not a reasoning fragment.
# split_analysis_prelude_from_output (imported above, from thinking_output.py)
# uses its own private copy of this same H1 pattern; kept as two constants
# rather than one shared import because this one gates the empty-content
# salvage credibility check specifically, not general prelude-splitting.
_TOP_LEVEL_H1_RE = re.compile(r"(?m)^#\s+.+$")

# One-time guard so the actual usage-object schema returned by DeepSeek's
# Anthropic-format endpoint is logged exactly once per process (see
# _extract_usage_tokens) — confirms field names empirically instead of guessing.
_USAGE_SHAPE_LOGGED = False

logger = logging.getLogger(__name__)

# ── Tool name constants (must match translator/tools/) ──────────────────────
TOOL_NAME_DECLARE_TRANSLATION_PARAMETERS = "declare_translation_parameters"
TOOL_NAME_REPORT_TRANSLATION_QC = "report_translation_qc"
TOOL_NAME_REPORT_CONSISTENCY_ANCHOR = "report_consistency_anchor"
TOOL_NAME_FLAG_STRUCTURAL_CONSTRAINT = "flag_structural_constraint"


# DeepSeek V4 Pro/Flash pricing lives in src.Deepseek.common.token_telemetry.PRICING_PER_MTOK
# now — see estimate_usage_cost_usd() below, which delegates there.


@dataclass
class DeepSeekTurnResult:
    """Single API turn result before aggregation."""

    final: Any = None
    text_parts: List[str] = field(default_factory=list)
    thinking_parts: List[str] = field(default_factory=list)
    finish_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    audit_phase: str = "translation_turn"


class DeepSeekClient:
    """
    DeepSeek V4 Pro client matching the AnthropicClient interface contract.

    Consumer code (agent.py, chapter_processor.py) reads capabilities via
    ``getattr(client, "CAPABILITIES", None)`` and gates provider-specific
    features accordingly.
    """

    CAPABILITIES: ProviderCapabilities = DEEPSEEK_CAPABILITIES
    _DEFAULT_HTTP_TIMEOUT_SECONDS = 600.0

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        enable_caching: bool = True,
        timeout_seconds: Optional[float] = None,
        base_url: Optional[str] = None,
        api_key_env: Optional[str] = None,
        thinking: bool = True,
    ):
        try:
            import anthropic as _anthropic_mod
        except ImportError:
            raise ImportError(
                "DeepSeekClient requires the Anthropic SDK (pip install anthropic). "
                "DeepSeek uses an Anthropic-compatible API format."
            )
        self._anthropic_mod = _anthropic_mod

        # ── Config resolution ────────────────────────────────────────────────
        from src.Deepseek.translator.config import get_deepseek_config
        ds_cfg = get_deepseek_config() or {}
        endpoint_type = str(
            ds_cfg.get("endpoint_type", "anthropic")
        ).strip().lower()
        if endpoint_type != "anthropic":
            raise ValueError(
                "DeepSeekClient requires endpoint_type: anthropic for Phase 2."
            )

        self.model = model or str(ds_cfg.get("model", "deepseek-v4-pro")).strip()
        self.fallback_model = str(ds_cfg.get("fallback_model") or "").strip() or None

        self._api_key_env = str(api_key_env or ds_cfg.get("api_key_env", "DEEPSEEK_API_KEY")).strip()
        self._api_key = api_key or os.getenv(self._api_key_env)
        if not self._api_key:
            raise ValueError(
                f"DeepSeek API key not found. Set {self._api_key_env} environment "
                f"variable or pass api_key= explicitly."
            )

        # Resolve base_url — supports both string and dict (endpoint_type-keyed)
        raw_base_url = base_url or ds_cfg.get("base_url", "https://api.deepseek.com/anthropic")
        if isinstance(raw_base_url, dict):
            raw_base_url = raw_base_url.get(
                endpoint_type,
                list(raw_base_url.values())[0],
            )
        self._base_url = str(raw_base_url).strip().rstrip("/")

        gen_cfg = ds_cfg.get("generation", {}) or {}
        self._max_output_tokens = int(gen_cfg.get("max_output_tokens", 128000))
        self._top_p = float(gen_cfg.get("top_p", 0.95))

        thinking_cfg = ds_cfg.get("thinking_mode", {}) or {}
        self._thinking_enabled = bool(thinking and thinking_cfg.get("enabled", True))
        self._reasoning_effort = str(
            thinking_cfg.get("reasoning_effort")
            or ((thinking_cfg.get("output_config") or {}).get("effort"))
            or "max"
        ).strip().lower()
        self._thinking_budget = int(
            thinking_cfg.get("thinking_budget", 32000)
        )
        reasoning_content_cfg = thinking_cfg.get("reasoning_content", {}) or {}
        self._reasoning_content_enabled = bool(reasoning_content_cfg.get("enabled", True))

        caching_cfg = ds_cfg.get("caching", {}) or {}
        self.enable_caching = bool(enable_caching and caching_cfg.get("enabled", True))
        self._cache_ttl_minutes = int(caching_cfg.get("ttl_minutes", 120))

        self._http_timeout = float(
            timeout_seconds or ds_cfg.get("http_timeout_seconds", self._DEFAULT_HTTP_TIMEOUT_SECONDS)
        )

        # ── Rate limiting ────────────────────────────────────────────────────
        self._rate_limit_delay = 6.0
        self._last_request_time = 0.0

        # ── Build Anthropic SDK client pointed at DeepSeek ──────────────────
        # DeepSeek's Anthropic-compatible endpoint expects ONLY x-api-key header.
        # The Anthropic SDK auto-discovers ANTHROPIC_AUTH_TOKEN + ANTHROPIC_API_KEY
        # from the environment, which may contain Claude Code / platform keys that
        # cause 401 Authentication Fails when both are sent to DeepSeek.
        # Workaround: temporarily clear conflicting env vars, then restore.
        _saved_auth_token = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        _saved_api_key = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ["ANTHROPIC_API_KEY"] = self._api_key
            self._client = _anthropic_mod.Anthropic(
                base_url=self._base_url,
                timeout=self._http_timeout,
                max_retries=0,
            )
        finally:
            if _saved_auth_token is not None:
                os.environ["ANTHROPIC_AUTH_TOKEN"] = _saved_auth_token
            if _saved_api_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = _saved_api_key
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

        # ── Optimization: DeepSeek cache monitor ────────────────────────────
        self._cache_monitor_enabled = bool(
            ds_cfg.get("cache_monitor", {}).get("enabled", True)
        )
        self._cache_warn_threshold = float(
            ds_cfg.get("cache_monitor", {}).get("warn_threshold_cache_hit_ratio", 0.50)
        )

        # ── Optimization: Thinking analytics ────────────────────────────────
        self._thinking_analytics_enabled = bool(
            ds_cfg.get("thinking_analytics", {}).get("enabled", True)
        )

        # ── Accumulated state (reset per chapter) ────────────────────────────
        self._cached_system_blocks: Optional[Any] = None
        self._conversation_manager: Optional[DeepSeekConversationManager] = None
        self.conversation_enabled = False

        logger.info(
            "DeepSeek V4 Pro client initialized (model=%s, endpoint=%s, thinking=%s, "
            "caching=%s, cache_monitor=%s, thinking_analytics=%s)",
            self.model,
            self._base_url,
            "enabled" if self._thinking_enabled else "disabled",
            "enabled" if self.enable_caching else "disabled",
            "enabled" if self._cache_monitor_enabled else "disabled",
            "enabled" if self._thinking_analytics_enabled else "disabled",
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Public cache surface (matching AnthropicClient + GeminiClient)
    # ══════════════════════════════════════════════════════════════════════════

    def create_cache(self, system_instruction: str, *args, **kwargs) -> Optional[str]:
        """No-op: DeepSeek auto-prefix cache requires no explicit creation."""
        self._cached_system_blocks = [
            {"type": "text", "text": system_instruction},
        ]
        return "__deepseek_auto_cache__"

    def delete_cache(self, *args, **kwargs) -> None:
        """Clear inline cache blocks."""
        self._cached_system_blocks = None

    def clear_cache(self) -> None:
        """Alias for delete_cache."""
        self._cached_system_blocks = None

    def warm_cache(
        self,
        system_instruction: Optional[str] = None,
        model: Optional[str] = None,
        *args,
        **kwargs,
    ) -> bool:
        """System-instruction-only warm (same as create_cache for DeepSeek)."""
        _ = model, args, kwargs
        if system_instruction:
            self.create_cache(system_instruction)
            return True
        return False

    def prewarm_cache(self, *args, **kwargs) -> None:
        """No-op: DeepSeek auto-prefix cache requires no prewarm."""
        pass

    def set_cache_ttl(self, minutes: int) -> None:
        """Store TTL for display only (DeepSeek manages cache lifecycle)."""
        self._cache_ttl_minutes = minutes

    # ── Application-owned multi-turn conversation ─────────────────────────

    def attach_conversation(
        self,
        *,
        work_dir: Path,
        volume_id: str,
        conversation_config: Optional[Dict[str, Any]] = None,
    ) -> DeepSeekConversationManager:
        """Attach the persisted current-volume conversation to this client."""
        cfg = dict(conversation_config or {})
        if "context_window" not in cfg:
            from src.Deepseek.translator.config import get_deepseek_config

            provider_cfg = get_deepseek_config() or {}
            cfg["context_window"] = int(
                provider_cfg.get("context_window", 1_000_000) or 1_000_000
            )
        self._conversation_manager = DeepSeekConversationManager(
            work_dir=Path(work_dir),
            volume_id=str(volume_id),
            model=self.model,
            endpoint=self._base_url,
            config=cfg,
            token_counter=self.get_token_count,
        )
        self.conversation_enabled = bool(self._conversation_manager.enabled)
        return self._conversation_manager

    @property
    def conversation_manager(self) -> Optional[DeepSeekConversationManager]:
        return self._conversation_manager

    def seed_conversation_turn(
        self,
        *,
        chapter_id: str,
        jp_text: str,
        en_text: str,
        output_path: Path,
    ) -> bool:
        manager = self._conversation_manager
        if manager is None:
            return False
        return manager.seed_turn_from_files(
            chapter_id=chapter_id,
            jp_text=jp_text,
            en_text=en_text,
            output_path=Path(output_path),
        )

    def truncate_conversation_from(self, chapter_id: str) -> bool:
        manager = self._conversation_manager
        return bool(manager and manager.truncate_from(chapter_id))

    def commit_conversation_turn(
        self,
        *,
        response: LLMResponse,
        chapter_id: str,
        output_path: Path,
        canonical_output: str,
    ) -> Dict[str, Any]:
        """Commit only the accepted response staged by ``generate``."""
        manager = self._conversation_manager
        if manager is None or not manager.enabled:
            return {"enabled": False}
        pending = getattr(response, "conversation_pending_turn", None)
        if not isinstance(pending, dict):
            raise RuntimeError(
                "DeepSeek response has no staged conversation turn; refusing to "
                "commit an ambiguous translation."
            )
        telemetry = dict(pending.get("telemetry") or {})
        return manager.commit_turn(
            chapter_id=chapter_id,
            user_prompt=str(pending.get("user_prompt") or ""),
            assistant_response=str(pending.get("assistant_response") or ""),
            canonical_output=canonical_output,
            output_path=Path(output_path),
            cache_telemetry=telemetry,
            tool_history=pending.get("tool_history"),
        )

    def commit_conversation_content(
        self,
        *,
        chapter_id: str,
        user_prompt: str,
        assistant_response: str,
        canonical_output: str,
        output_path: Path,
        cache_telemetry: Optional[Dict[str, Any]] = None,
        subturns: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Commit an atomically accepted aggregate such as a merged chapter."""
        manager = self._conversation_manager
        if manager is None or not manager.enabled:
            return {"enabled": False}
        return manager.commit_turn(
            chapter_id=chapter_id,
            user_prompt=user_prompt,
            assistant_response=assistant_response,
            canonical_output=canonical_output,
            output_path=Path(output_path),
            cache_telemetry=cache_telemetry,
            subturns=subturns,
        )

    # ── Local Tokenizer (approximation) ──────────────────────────────────────
    # Delegates to src.Deepseek.common.token_telemetry.count_tokens(), shared with
    # prep — local tiktoken (o200k_base, then cl100k_base), then a
    # char-based heuristic. DeepSeek's own HuggingFace tokenizer repos were
    # tried here briefly and reverted: they need HF_TOKEN in practice
    # (rate-limited or gated depending on the repo), and a token-counting
    # utility silently depending on an unrelated Hub credential isn't a
    # trade worth making for a closer-but-still-approximate count. Never
    # raises, never touches the network. The API response `usage` fields
    # remain the ground truth for actual billing — this is for context-
    # budget estimation (conversation manager compaction-ladder decisions),
    # not what gets logged as "actual cost" in LOG/token_log.md.

    def get_token_count(self, text: str) -> int:
        """Count tokens using the local tiktoken approximation for self.model."""
        from src.Deepseek.common.token_telemetry import count_tokens
        return count_tokens(text, self.model)

    def get_token_count_batch(self, texts: List[str]) -> int:
        """Count total tokens across multiple texts."""
        from src.Deepseek.common.token_telemetry import count_tokens
        return sum(count_tokens(t, self.model) for t in texts)

    # ══════════════════════════════════════════════════════════════════════════
    # Cost estimation
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def estimate_usage_cost_usd(
        *,
        model_name: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Estimate USD cost for DeepSeek V4 (Pro or Flash).

        Delegates to src.Deepseek.common.token_telemetry.cost_breakdown_usd() — the
        pricing table now lives there as the single source of truth shared
        with prep (src/utility/prep/parallel_agent.py) and LOG/token_log.md, instead
        of a second copy here that could silently drift out of sync with it
        after the next DeepSeek price change. Return shape is unchanged —
        existing callers of this method see no difference.
        """
        from src.Deepseek.common.token_telemetry import cost_breakdown_usd
        _ = kwargs
        return cost_breakdown_usd(
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # stream_generate — wraps generate() with streaming flag
    # ══════════════════════════════════════════════════════════════════════════

    def stream_generate(self, *args, **kwargs) -> LLMResponse:
        """Alias for generate() — DeepSeek supports streaming natively."""
        return self.generate(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # Core: generate()
    # ══════════════════════════════════════════════════════════════════════════

    def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: int = 64000,
        safety_settings: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        cached_content: Optional[str] = None,
        force_new_session: bool = False,
        generation_config: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Any]] = None,
        use_tool_mode: bool = False,
        tool_handlers: Optional[Dict[str, Any]] = None,
        retrospective_anchor: Optional[str] = None,
        dry_run: bool = False,
    ) -> LLMResponse:
        """
        Generate content via DeepSeek API (Anthropic-compatible endpoint).

        Returns an LLMResponse for the shared ChapterProcessor contract.

        dry_run=True guarantees zero network calls, full stop — not just
        skipping the translation turn itself. That's why the conversation
        manager's prepare_turn() is also skipped below rather than run and
        then discarded: prepare_turn() can trigger a REAL, billed checkpoint-
        summarization call of its own when the compaction ladder happens to
        fire on this turn (see the `checkpoint_usage` handling a few lines
        down) — running it "just to preview the payload" would silently
        defeat the one guarantee dry-run exists to make. The cost: a dry-run
        preview shows this turn's raw system+user payload, not the
        conversation-accumulated one multi-turn mode would actually send.
        Callers that need the accumulated shape have no dry-run-safe way to
        see it short of tracing prepare_turn() by hand.
        """
        _ = safety_settings, force_new_session, retrospective_anchor
        target_model = model or self.model

        # Apply generation_config overrides
        if generation_config:
            temperature = generation_config.get("temperature", temperature)
            max_output_tokens = generation_config.get("max_output_tokens", max_output_tokens)

        max_output_tokens = min(max_output_tokens, self._max_output_tokens)

        # Rate limit
        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)

        # Build system value
        system_value = system_instruction or None
        if cached_content and self._cached_system_blocks:
            system_value = self._cached_system_blocks

        # Resolve tools BEFORE the conversation turn is prepared.  Tool schemas sit
        # in the cached prefix alongside the system prompt, so the ledger needs the
        # final, filtered list to hash — hashing the raw `tools` argument would
        # attribute drift to a payload that is not the one actually sent.  This block
        # reads only `tools` / `use_tool_mode` and is independent of `messages`,
        # `system_value`, and `manager`, so its position here is free.
        enabled_tools = [
            tool for tool in (tools or [])
            if isinstance(tool, dict) and str(tool.get("name", "")).strip()
        ]
        post_turn_tools = [
            tool for tool in enabled_tools
            if tool.get("name") != TOOL_NAME_DECLARE_TRANSLATION_PARAMETERS
        ]
        declare_tool = next(
            (tool for tool in enabled_tools
             if tool.get("name") == TOOL_NAME_DECLARE_TRANSLATION_PARAMETERS),
            None,
        )
        tool_mode_active = bool(use_tool_mode and declare_tool)

        # Filter out advisor_20260301 — not supported by DeepSeek
        enabled_tools = [
            t for t in enabled_tools if t.get("type") != "advisor_20260301"
        ]
        post_turn_tools = [
            t for t in post_turn_tools if t.get("type") != "advisor_20260301"
        ]
        # Re-resolve declare_tool after filter
        if tool_mode_active:
            declare_tool = next(
                (t for t in enabled_tools
                 if t.get("name") == TOOL_NAME_DECLARE_TRANSLATION_PARAMETERS),
                None,
            )
            tool_mode_active = bool(declare_tool)

        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        conversation_telemetry: Dict[str, Any] = {"enabled": False}
        checkpoint_usage: Optional[Dict[str, Any]] = None
        manager = self._conversation_manager
        if manager is not None and manager.enabled and not dry_run:
            prepared = manager.prepare_turn(
                prompt=prompt,
                system=system_value,
                max_output_tokens=max_output_tokens,
                checkpoint_callback=self._generate_conversation_checkpoint,
                tools=enabled_tools,
            )
            system_value = prepared["system"]
            messages = list(prepared["messages"])
            conversation_telemetry = dict(prepared.get("telemetry") or {})
            checkpoint_usage = prepared.get("checkpoint_usage")
            if checkpoint_usage:
                elapsed = time.time() - self._last_request_time
                if elapsed < self._rate_limit_delay:
                    time.sleep(self._rate_limit_delay - elapsed)

        # Build base kwargs
        kwargs: Dict[str, Any] = {
            "model": target_model,
            "max_tokens": max_output_tokens,
            "messages": messages,
        }
        if system_value is not None:
            kwargs["system"] = system_value
        if manager is not None and manager.metadata_user_id:
            kwargs["metadata"] = {"user_id": manager.metadata_user_id}

        # DeepSeek thinking: always "enabled" when configured, budget_tokens locked to
        # providers.yaml value (default 32000), output_config.effort=max.
        # Do NOT include temperature when thinking is enabled (errors in DeepSeek).
        if self._thinking_enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget,
            }
            kwargs["output_config"] = {"effort": self._reasoning_effort or "max"}
            kwargs.pop("temperature", None)
        else:
            kwargs["temperature"] = temperature

        if dry_run:
            # kwargs is the exact payload messages.stream(**kwargs) would have
            # received — model, max_tokens, messages, system, thinking,
            # output_config, all present exactly as a real call would build
            # them. Stashed in provider_metadata rather than written to disk
            # here: this class doesn't otherwise do file I/O, and it has no
            # idea what chapter/volume this is for — the caller (agent.py's
            # translate_chapter, which does know both) owns turning this into
            # a markdown file, same division of labor as thinking-log and
            # cost-log already use.
            return LLMResponse(
                content="",
                finish_reason="dry_run",
                model=target_model,
                provider="deepseek",
                api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
                raw_finish_reason="dry_run",
                provider_metadata={"dry_run": True, "payload": kwargs},
            )

        start_time = time.time()

        try:
            text_parts: List[str] = []
            thinking_parts: List[str] = []
            finish_reason = "end_turn"
            turn_records: List[DeepSeekTurnResult] = []
            if isinstance(checkpoint_usage, dict):
                checkpoint_turn = DeepSeekTurnResult(
                    text_parts=[str(checkpoint_usage.get("content") or "")],
                    finish_reason=str(checkpoint_usage.get("finish_reason") or "end_turn"),
                    input_tokens=int(checkpoint_usage.get("input_tokens") or 0),
                    output_tokens=int(checkpoint_usage.get("output_tokens") or 0),
                    cache_read_tokens=int(checkpoint_usage.get("cache_read_tokens") or 0),
                    cache_creation_tokens=int(checkpoint_usage.get("cache_miss_tokens") or 0),
                    audit_phase="conversation_checkpoint",
                )
                turn_records.append(checkpoint_turn)
            tool_calls_made: List[str] = []
            declared_params = None
            qc_self_report = None
            consistency_anchor_report = None
            structural_constraints: List[Any] = []
            current_messages: List[Dict[str, Any]] = list(messages)

            # ── Single-turn generation (no tools — DeepSeek does not support tool_choice with thinking) ──
            turn_kwargs = dict(kwargs)
            if enabled_tools:
                turn_kwargs["tools"] = enabled_tools
            turn_kwargs["tool_choice"] = {"type": "none"}

            turn_result = self._run_turn(turn_kwargs=turn_kwargs)
            text_parts.extend(turn_result.text_parts)
            thinking_parts.extend(turn_result.thinking_parts)
            finish_reason = turn_result.finish_reason
            turn_result.audit_phase = "translation_turn"
            turn_records.append(turn_result)

        except Exception as e:
            logger.error("DeepSeek API error (%s): %s", e.__class__.__name__, e)
            raise

        # ── Reasoning-channel salvage (fail-safe) ───────────────────────────
        # If the content channel came back empty but the finished chapter is
        # sitting in reasoning_content (observed in multi-turn), recover it rather
        # than discarding a paid, fully generated translation. The silent-reasoning
        # prompt contract is the real fix; this is insurance against a recurrence.
        _salvaged = self._salvage_reasoning_leaked_answer(text_parts, thinking_parts)
        if _salvaged is not None:
            text_parts, thinking_parts, _salvage_method = _salvaged
            logger.warning(
                "[DEEPSEEK-SALVAGE] Content channel empty — recovered %d-char "
                "chapter from reasoning_content via %s. The model emitted its answer "
                "on the reasoning channel; confirm the silent-reasoning prompt "
                "contract is in force for this route.",
                len("".join(text_parts)), _salvage_method,
            )

        # ── Aggregate across turns ──────────────────────────────────────────
        input_tokens = sum(int(t.input_tokens) for t in turn_records)
        output_tokens = sum(int(t.output_tokens) for t in turn_records)
        cache_read_tokens = sum(int(t.cache_read_tokens) for t in turn_records)
        cache_miss_tokens = sum(int(t.cache_creation_tokens) for t in turn_records)
        cache_creation_tokens = 0  # DeepSeek automatic cache misses are not cache writes.

        # Compute cost per turn
        for t in turn_records:
            t_cost = self.estimate_usage_cost_usd(
                model_name=target_model,
                input_tokens=int(t.input_tokens),
                output_tokens=int(t.output_tokens),
                cache_read_tokens=int(t.cache_read_tokens),
                cache_creation_tokens=0,
            )
            t._cost_breakdown = t_cost

        total_input_cost = sum(float(getattr(t, "_cost_breakdown", {}).get("input_cost_usd", 0)) for t in turn_records)
        total_output_cost = sum(float(getattr(t, "_cost_breakdown", {}).get("output_cost_usd", 0)) for t in turn_records)
        total_cache_read_cost = sum(float(getattr(t, "_cost_breakdown", {}).get("cache_read_cost_usd", 0)) for t in turn_records)
        total_cache_creation_cost = sum(float(getattr(t, "_cost_breakdown", {}).get("cache_creation_cost_usd", 0)) for t in turn_records)
        total_cost = sum(float(getattr(t, "_cost_breakdown", {}).get("total_cost_usd", 0)) for t in turn_records)

        # ── Optimization 4: Cache efficiency analytics ──────────────────────
        cache_denominator = cache_read_tokens + cache_miss_tokens
        cache_hit_ratio = (
            cache_read_tokens / cache_denominator if cache_denominator > 0 else 0.0
        )
        if self._cache_monitor_enabled and cache_denominator > 0:
            if cache_hit_ratio < self._cache_warn_threshold:
                # "Increase prefix stability" names the lever but not the thing that
                # moved.  The ledger tracked the system text, the tool schemas, and
                # the checkpoint generation as separate axes precisely so this
                # warning can point at a component instead of at the whole prefix.
                attribution: Dict[str, Any] = {}
                if manager is not None and manager.enabled:
                    try:
                        attribution = manager.cache_miss_attribution(
                            cache_read_tokens=cache_read_tokens,
                            cache_miss_tokens=cache_miss_tokens,
                        )
                    except Exception as exc:  # diagnostics must never break a turn
                        logger.debug(
                            "[DEEPSEEK-CACHE] Miss attribution unavailable: %s", exc
                        )
                reasons = list(attribution.get("prefix_change_reasons") or [])
                if reasons:
                    cause = "prefix moved this turn: " + ", ".join(reasons)
                elif attribution.get("consecutive_checkpoints"):
                    # Prefix held, but checkpointing is rewriting it on a run of
                    # turns — the compaction meant to save tokens is spending them.
                    cause = (
                        "prefix stable, but "
                        f"{attribution['consecutive_checkpoints']} consecutive "
                        "checkpoint(s) have been rewriting it"
                    )
                elif attribution:
                    cause = (
                        "prefix stable; miss is tail growth, not prefix drift "
                        f"(generation={attribution.get('checkpoint_generation', 0)})"
                    )
                else:
                    cause = "consider increasing prompt prefix stability"
                logger.warning(
                    "[DEEPSEEK-CACHE] Low cache hit ratio: %.1f%% "
                    "(hit=%s, miss=%s) — %s.",
                    cache_hit_ratio * 100,
                    f"{cache_read_tokens:,}",
                    f"{cache_miss_tokens:,}",
                    cause,
                )
            else:
                logger.info(
                    "[DEEPSEEK-CACHE] Cache hit ratio: %.1f%% (hit=%s, miss=%s)",
                    cache_hit_ratio * 100,
                    f"{cache_read_tokens:,}",
                    f"{cache_miss_tokens:,}",
                )

        # ── Optimization 5: Thinking budget analytics ───────────────────────
        thinking_content = "".join(thinking_parts).strip() if thinking_parts else None
        if not self._reasoning_content_enabled:
            thinking_content = None
        if self._thinking_analytics_enabled and thinking_content:
            thinking_len = len(thinking_content)
            thinking_tokens_est = len(thinking_content) // 3  # rough estimate
            logger.info(
                "[DEEPSEEK-THINKING] Thinking output: ~%d chars / ~%d est tokens "
                "(%d text parts, %d output tokens)",
                thinking_len,
                thinking_tokens_est,
                len(text_parts),
                output_tokens,
            )

        # ── Build cost audit dict ───────────────────────────────────────────
        turn_audit_records = []
        for idx, t in enumerate(turn_records):
            turn_audit_records.append({
                "index": idx + 1,
                "phase": str(t.audit_phase or "translation_turn"),
                "finish_reason": str(t.finish_reason or ""),
                "input_tokens": int(t.input_tokens),
                "output_tokens": int(t.output_tokens),
                "cache_read_tokens": int(t.cache_read_tokens),
                "cache_miss_tokens": int(t.cache_creation_tokens),
                "cache_creation_tokens": 0,
                "cost_breakdown": dict(getattr(t, "_cost_breakdown", {}) or {}),
            })

        response_cost_audit = {
            "schema_version": "1.0",
            "provider": "deepseek",
            "request_type": "stream",
            "turn_count": len(turn_audit_records),
            "turns": turn_audit_records,
            "tool_calls_made": list(tool_calls_made),
            "tool_call_count": len(tool_calls_made),
            "totals": {
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
                "cached_tokens": int(cache_read_tokens or 0),
                "cache_miss_tokens": int(cache_miss_tokens or 0),
                "cache_creation_tokens": int(cache_creation_tokens or 0),
                "input_cost_usd": round(total_input_cost, 8),
                "output_cost_usd": round(total_output_cost, 8),
                "cache_read_cost_usd": round(total_cache_read_cost, 8),
                "cache_creation_cost_usd": round(total_cache_creation_cost, 8),
                "total_cost_usd": round(total_cost, 8),
            },
            "batch_pricing": False,
            "fast_mode_pricing": False,
            "conversation": {
                **conversation_telemetry,
                "cache_hit_tokens": int(cache_read_tokens or 0),
                "cache_miss_tokens": int(cache_miss_tokens or 0),
                "cache_hit_ratio": round(cache_hit_ratio, 6),
                "expected_reusable_prefix_tokens": int(cache_read_tokens or 0),
            },
        }

        duration = time.time() - start_time
        logger.info(
            "DeepSeek response in %.2fs (stop_reason=%s, turns=%d)",
            duration, finish_reason, len(turn_records),
        )
        logger.info(
            "[COST] DeepSeek: in=%s ($%.6f) | out=%s ($%.6f) | "
            "cache_read=%s ($%.6f) | total=$%.6f",
            f"{input_tokens:,}", total_input_cost,
            f"{output_tokens:,}", total_output_cost,
            f"{cache_read_tokens:,}", total_cache_read_cost,
            total_cost,
        )

        self._last_request_time = time.time()

        response = LLMResponse(
            content="".join(text_parts),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            model=target_model,
            provider="deepseek",
            api_family=LLMApiFamily.ANTHROPIC_MESSAGES,
            raw_finish_reason=finish_reason,
            content_blocks=(
                [LLMContentBlock(type="text", text="".join(text_parts))]
                + ([LLMContentBlock(type="reasoning", text=thinking_content)] if thinking_content else [])
            ),
            usage=LLMUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
            ),
            cached_tokens=cache_read_tokens,
            thinking_content=thinking_content,
            cache_creation_tokens=cache_creation_tokens,
            input_cost_usd=round(total_input_cost, 8),
            output_cost_usd=round(total_output_cost, 8),
            cache_read_cost_usd=round(total_cache_read_cost, 8),
            cache_creation_cost_usd=round(total_cache_creation_cost, 8),
            total_cost_usd=round(total_cost, 8),
            batch_pricing=False,
            fast_mode_pricing=False,
            declared_params=declared_params,
            tool_calls_made=list(tool_calls_made),
            tool_call_count=len(tool_calls_made),
            qc_self_report=qc_self_report,
            consistency_anchor_report=consistency_anchor_report,
            structural_constraints=list(structural_constraints),
            cost_audit=response_cost_audit,
        )
        tool_history = self._build_committable_tool_history(
            turn_records[-1].final if turn_records else None
        )
        response.conversation_telemetry = dict(response_cost_audit["conversation"])
        response.conversation_pending_turn = {
            "user_prompt": prompt,
            "assistant_response": response.content,
            "telemetry": dict(response.conversation_telemetry),
            "tool_history": tool_history,
            "force_new_session": bool(force_new_session),
        }
        return response

    def _generate_conversation_checkpoint(
        self,
        *,
        previous_checkpoint: Optional[str],
        evicted_turns: List[Dict[str, Any]],
        required_chapter_ids: List[str],
        max_output_tokens: int,
        validation_errors: List[str],
    ) -> Dict[str, Any]:
        """Generate a validated continuity checkpoint without mutating history."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._rate_limit_delay:
            time.sleep(self._rate_limit_delay - elapsed)
        transcript = []
        for turn in evicted_turns:
            transcript.append(
                {
                    "chapter_id": turn.get("chapter_id"),
                    "user_prompt": turn.get("user_prompt"),
                    "assistant_response": turn.get("assistant_response"),
                }
            )
        correction = ""
        if validation_errors:
            correction = (
                "\nThe prior attempt failed validation for: "
                + "; ".join(validation_errors)
                + ". Correct every defect."
            )
        checkpoint_prompt = (
            "Compress the accepted translation transcript into one dense, "
            "loss-minimizing current-volume continuity checkpoint. Return XML "
            "only — no preamble, no commentary. Use root "
            "<deepseek_volume_checkpoint> and exactly these named sections: "
            "chapter_coverage, plot_state, relationship_state, unresolved_threads, "
            "names_and_terms, voice_and_pov, callbacks, translation_decisions, "
            "anchor_excerpts. Every section: "
            "<section name=\"...\"><![CDATA[...]]></section>. "
            "Mention every required chapter ID verbatim: "
            f"{', '.join(required_chapter_ids)}. "
            "translation_decisions: preserve only reusable locked choices "
            "(wording, register, device strategy, formatting) — label each "
            "MODE=DECISION unless backed by an exact accepted span. Omit "
            "one-off decisions that will never recur. "
            "anchor_excerpts: retain ONLY English prose likely to recur as a "
            "quotation, flashback, catchphrase, promise, prophecy, or title "
            "echo. For each, give CHAPTER_ID and SOURCE_TRIGGER, then the "
            "exact prose between EXACT_EN_BEGIN and EXACT_EN_END copied "
            "byte-for-byte from assistant_response — never normalize, never "
            "reconstruct from memory. If no such anchor exists for the "
            "evicted chapters, write NONE and move on — do not invent. "
            "Prioritize density: every word must earn its place. Do not "
            "paraphrase plot when a one-line summary suffices. Do not "
            "invent facts."
            f"{correction}\n\n"
            f"<previous_checkpoint>{previous_checkpoint or ''}</previous_checkpoint>\n"
            "<accepted_turns_json>\n"
            f"{json.dumps(transcript, ensure_ascii=False, sort_keys=True)}\n"
            "</accepted_turns_json>"
        )
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": min(int(max_output_tokens), self._max_output_tokens),
            "system": (
                "You maintain deterministic literary-translation continuity. "
                "Output only the requested XML checkpoint."
            ),
            "messages": [{"role": "user", "content": checkpoint_prompt}],
        }
        manager = self._conversation_manager
        if manager is not None and manager.metadata_user_id:
            kwargs["metadata"] = {"user_id": manager.metadata_user_id}
        if self._thinking_enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": min(
                    self._thinking_budget,
                    max(1024, int(max_output_tokens) // 2),
                ),
            }
            kwargs["output_config"] = {"effort": self._reasoning_effort or "max"}
        result = self._run_turn(turn_kwargs=kwargs)
        self._last_request_time = time.time()
        return {
            "content": "".join(result.text_parts),
            "finish_reason": result.finish_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_miss_tokens": result.cache_creation_tokens,
        }

    @staticmethod
    def _build_committable_tool_history(
        final: Any,
        tool_result_messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Retain complete thinking/tool-use/tool-result blocks for tool turns."""
        blocks = getattr(final, "content", None)
        if not isinstance(blocks, list):
            return None
        serialized: List[Dict[str, Any]] = []
        has_tool_use = False
        for block in blocks:
            if isinstance(block, dict):
                value = dict(block)
            elif callable(getattr(block, "model_dump", None)):
                value = block.model_dump(exclude_none=True)
            else:
                value = {
                    key: getattr(block, key)
                    for key in ("type", "text", "thinking", "signature", "id", "name", "input")
                    if getattr(block, key, None) is not None
                }
            if value.get("type") == "tool_use":
                has_tool_use = True
            serialized.append(value)
        if not has_tool_use:
            return None
        history: List[Dict[str, Any]] = [
            {"role": "assistant", "content": serialized}
        ]
        for message in tool_result_messages or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                history.append({"role": "user", "content": content})
        return history

    # ══════════════════════════════════════════════════════════════════════════
    # Internal: Fail-safe salvage of a chapter routed into reasoning_content
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _salvage_reasoning_leaked_answer(
        text_parts: List[str],
        thinking_parts: List[str],
    ) -> Optional[Tuple[List[str], List[str], str]]:
        """Recover a chapter the model routed into reasoning_content instead of content.

        DeepSeek V4 Pro is a native-reasoning model: its chain-of-thought streams on
        the reasoning_content channel and the finished chapter is expected on the
        content channel. In multi-turn conversations it has been observed to emit its
        ENTIRE turn — reasoning AND the finished chapter — into reasoning_content,
        leaving content empty (WORK/…_1b2b chapters 02–04, 2026-07-24). The real fix
        is the prompt no longer asking for a visible <thinking> block (silent native
        reasoning); this is fail-safe insurance so a recurrence salvages the paid,
        fully generated chapter instead of discarding it as an "empty response".

        Returns ``(new_text_parts, new_thinking_parts, method)`` when a credible
        chapter is recovered, else ``None`` (leaving the caller's empty-response
        handling intact — fail closed rather than ship a reasoning fragment).
        """
        if "".join(text_parts).strip():
            return None  # content channel populated — normal path, never touch it.
        reasoning = "".join(thinking_parts)
        if not reasoning.strip():
            return None  # genuinely empty turn — nothing to salvage.

        answer = ""
        reasoning_head = ""
        method = ""
        # Case 1: the model textually closed its reasoning with </thinking> and then
        # continued with the chapter, all still on the reasoning_content channel.
        close_idx = reasoning.rfind("</thinking>")
        if close_idx != -1:
            answer = reasoning[close_idx + len("</thinking>"):].strip()
            reasoning_head = reasoning[:close_idx].strip()
            method = "closing_thinking_tag"
        else:
            # Case 2: no tag — split at the first top-level chapter heading, treating
            # the analysis prelude before it as reasoning.
            body, prelude = split_analysis_prelude_from_output(reasoning)
            if prelude:
                answer = body.strip()
                reasoning_head = prelude[0].strip()
                method = "h1_prelude_split"

        # Credibility gate: the salvaged answer must look like a chapter (contain a
        # top-level H1). Otherwise fail closed and let the empty-response error fire.
        if not answer or not _TOP_LEVEL_H1_RE.search(answer):
            return None

        return ([answer], [reasoning_head] if reasoning_head else [], method)

    # ══════════════════════════════════════════════════════════════════════════
    # Internal: Normalize the usage object across endpoint schemas
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_usage_tokens(usage: Any) -> Dict[str, int]:
        """Normalize token usage across DeepSeek's Anthropic- and OpenAI-format schemas.

        DeepSeek V4 Pro runs on the Anthropic-format endpoint, whose usage object
        reports cache tokens in ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
        and — crucially — reports ``input_tokens`` as the UNCACHED portion only (cache
        tokens are excluded). The prior code read the OpenAI-format names
        (``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``), which do not exist
        on this endpoint, so cache telemetry always read 0 even while the server-side
        prefix cache was plainly active (input_tokens collapse after turn 1).

        This normalizes BOTH schemas to the convention ``estimate_usage_cost_usd`` and
        the aggregation code already expect:
          * ``input_tokens``          — TOTAL input, INCLUDING cache reads AND writes
                                        (so the cost model's ``input_tokens - cache_read``
                                        still yields the true uncached-billed count —
                                        DeepSeek bills writes at the normal input rate,
                                        same as a genuine miss, so cost is unaffected by
                                        anything below).
          * ``cache_read_tokens``     — tokens served from cache (cheap rate, a hit).
          * ``cache_write_tokens``    — tokens newly written to a cache entry THIS turn
                                        (Anthropic-format only; 0 on the OpenAI-format
                                        fallback, which has no separate write signal).
                                        A write is prefix content becoming reusable on
                                        the NEXT turn, not a failure on this one.
          * ``cache_creation_tokens`` — the genuine miss bucket (aggregated downstream
                                        as ``cache_miss_tokens`` for the hit-ratio health
                                        metric). Deliberately EXCLUDES cache writes: a
                                        turn that pays to establish a new cache entry
                                        (turn 1 of a volume, or the turn right after a
                                        checkpoint/ladder rung resets the prefix) is not
                                        "the cache failing" and must not report a false
                                        near-zero hit ratio just because nothing existed
                                        yet to read from. Cost accounting is untouched —
                                        ``estimate_usage_cost_usd`` derives the uncached
                                        bill from ``input_tokens - cache_read_tokens``
                                        directly and never reads this key.
        """
        global _USAGE_SHAPE_LOGGED
        if usage is None:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_creation_tokens": 0,
            }

        if not _USAGE_SHAPE_LOGGED:
            _USAGE_SHAPE_LOGGED = True
            shape: Any
            try:
                shape = usage.model_dump()  # pydantic v2 (Anthropic SDK)
            except Exception:
                try:
                    shape = {
                        k: getattr(usage, k)
                        for k in (
                            "input_tokens", "output_tokens",
                            "cache_read_input_tokens", "cache_creation_input_tokens",
                            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                        )
                        if hasattr(usage, k)
                    }
                except Exception:
                    shape = {"repr": repr(usage)[:400]}
            logger.info(
                "[DEEPSEEK-USAGE-SHAPE] usage fields as returned by the endpoint: %s",
                shape,
            )

        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        raw_input = int(getattr(usage, "input_tokens", 0) or 0)

        # Anthropic-format cache fields take priority; input_tokens excludes them here.
        a_read = getattr(usage, "cache_read_input_tokens", None)
        a_create = getattr(usage, "cache_creation_input_tokens", None)
        if a_read is not None or a_create is not None:
            cache_read = int(a_read or 0)
            cache_write = int(a_create or 0)
            # DeepSeek bills cache writes at the normal input rate → fold into total
            # for COST purposes. Do NOT fold cache_write into cache_miss below — that
            # is a separate, health-metric bucket and writes are not misses there.
            total_input = raw_input + cache_read + cache_write
            cache_miss = raw_input
        else:
            # OpenAI-format fallback: prompt_cache_hit is a SUBSET of input_tokens, and
            # this schema has no separate write signal — miss is just total minus read.
            cache_read = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
            cache_write = 0
            total_input = raw_input  # already includes hits
            cache_miss = max(0, total_input - cache_read)

        return {
            "input_tokens": total_input,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_creation_tokens": cache_miss,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Internal: Run a single streaming turn
    # ══════════════════════════════════════════════════════════════════════════

    def _run_turn(
        self,
        *,
        turn_kwargs: Dict[str, Any],
    ) -> DeepSeekTurnResult:
        """Execute one DeepSeek API call with streaming."""
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        finish_reason = "end_turn"
        final = None

        try:
            with self._client.messages.stream(**turn_kwargs) as stream:
                for event in stream:
                    event_type = getattr(event, "type", None)
                    if event_type != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    delta_type = getattr(delta, "type", None)
                    if delta_type == "text_delta":
                        text_parts.append(getattr(delta, "text", ""))
                    elif delta_type == "thinking_delta":
                        thinking_parts.append(getattr(delta, "thinking", ""))
                    elif delta_type == "redacted_thinking_delta":
                        thinking_parts.append("\n\n[REDACTED_THINKING_BLOCK]\n\n")

                final = stream.get_final_message()
                finish_reason = getattr(final, "stop_reason", None) or "end_turn"

            usage = getattr(final, "usage", None)
            _u = self._extract_usage_tokens(usage)

            return DeepSeekTurnResult(
                final=final,
                text_parts=text_parts,
                thinking_parts=thinking_parts,
                finish_reason=finish_reason,
                input_tokens=_u["input_tokens"],
                output_tokens=_u["output_tokens"],
                cache_read_tokens=_u["cache_read_tokens"],
                cache_creation_tokens=_u["cache_creation_tokens"],
            )
        except Exception as e:
            logger.error("[DEEPSEEK-TURN] Stream failure: %s", e)
            # Try non-streaming fallback
            logger.warning("[DEEPSEEK-TURN] Attempting non-streaming fallback...")
            fallback_kwargs = {
                k: v for k, v in turn_kwargs.items()
                if k not in ("stream",)
            }
            try:
                response = self._client.messages.create(**fallback_kwargs)
                for block in getattr(response, "content", []):
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        text_parts.append(getattr(block, "text", "") or "")
                    elif btype == "thinking":
                        thinking_parts.append(getattr(block, "thinking", "") or "")

                final = response
                finish_reason = getattr(response, "stop_reason", None) or "end_turn"
                usage = getattr(response, "usage", None)
                _u = self._extract_usage_tokens(usage)
                input_tokens = _u["input_tokens"]
                output_tokens = _u["output_tokens"]
                cached_tokens = _u["cache_read_tokens"]
                cache_creation_tokens_int = _u["cache_creation_tokens"]

                return DeepSeekTurnResult(
                    final=final,
                    text_parts=text_parts,
                    thinking_parts=thinking_parts,
                    finish_reason=finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cached_tokens,
                    cache_creation_tokens=cache_creation_tokens_int,
                )
            except Exception as fe:
                logger.error("[DEEPSEEK-TURN] Non-streaming fallback also failed: %s", fe)
                raise

    # ══════════════════════════════════════════════════════════════════════════
    # Internal: Strip thinking blocks between tool turns
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _assistant_message_had_tool_use(msg: Dict[str, Any]) -> bool:
        content = msg.get("content")
        if not isinstance(content, list):
            return False
        return any(b.get("type") == "tool_use" for b in content)

    def _strip_thinking_from_turn(self, messages: list) -> list:
        """
        Strip thinking blocks from assistant turns that did NOT use tools.

        DeepSeek thinking+tool-call guidance: assistant turns that performed
        tool_use must retain thinking blocks in the message history for the
        next API request. Non-tool turns omit prior CoT (multi-turn efficiency).
        """
        cleaned = []
        for msg in messages:
            if (
                msg.get("role") == "assistant"
                and isinstance(msg.get("content"), list)
                and not self._assistant_message_had_tool_use(msg)
            ):
                msg = {
                    **msg,
                    "content": [
                        b for b in msg["content"]
                        if b.get("type") != "thinking"
                    ],
                }
            cleaned.append(msg)
        return cleaned

    # ══════════════════════════════════════════════════════════════════════════
    # Internal: Serialize assistant content blocks for message history
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _serialize_assistant_blocks(blocks: List[Any]) -> List[Dict[str, Any]]:
        """Convert Anthropic content blocks to serializable dicts."""
        serialized = []
        for block in blocks:
            btype = getattr(block, "type", None)
            if btype == "text":
                serialized.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                serialized.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                })
            elif btype == "thinking":
                thinking_text = getattr(block, "thinking", "") or ""
                if thinking_text:
                    serialized.append({"type": "thinking", "thinking": thinking_text})
            # Note: redacted_thinking, search_results, etc. are also omitted
        return serialized
