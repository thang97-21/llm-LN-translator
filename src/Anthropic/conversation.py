"""Persistent messages-array conversation ledger for the Anthropic route."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.Anthropic.response import normalize_anthropic_content, sanitize_replayable_block
from src.Deepseek.common.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)

# Content block types whose replay is governed by the binding check.
_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")

# The models that bind a thinking block's signature to the conversation
# prefix that produced it, and reject a replay after that prefix changed.
# Introduced with Claude Fable 5.1 (2026-09-01) and shared by its Project
# Glasswing counterpart; claude-opus-5-5 (2026-09-22) enforces the same check,
# by default for accounts created on or after 2026-08-31. claude-opus-5 and
# claude-sonnet-5 accept an edited history without complaint, so they are
# deliberately absent: nothing below alters their behaviour.
#
# Cross-model note (Opus 5.5 migration guide): Opus 5.5 reads thinking blocks
# from Opus 5 / Sonnet models but not from Fable or Mythos. A volume switched
# from claude-fable-5-1 to claude-opus-5-5 mid-run has the old blocks dropped
# by the API before the model sees them -- the request succeeds, unbilled.
PREFIX_BOUND_THINKING_MODELS = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5")


class AnthropicConversationManager:
    """Retain a role:user/assistant message ledger across chapters.

    Unlike OpenAI's Responses "input items" ledger, the Messages API is
    stateless per request: every call resends the full conversation as a
    ``messages`` array. ``thinking``/``redacted_thinking`` blocks inside a
    stored assistant turn are passed back byte-exact — never re-synthesized
    — per Anthropic's "Preserving thinking blocks" contract.

    PRESERVED THINKING (claude-fable-5-1 / claude-mythos-5-1 /
    claude-opus-5-5 ONLY — see PREFIX_BOUND_THINKING_MODELS). A Fable 5.1
    (or Opus 5.5) thinking block's
    signature binds the conversation prefix that produced it: the top-level
    ``system`` prompt, the tool set, and every message ahead of the block.
    Replaying a block whose prefix has since changed is a 400 decided before
    any output, and the token-counting endpoint applies the same check.

    This manager performs KEEP-TAIL compaction — fold the older turns into a
    summary, keep the most recent ones verbatim — which Anthropic names
    explicitly as one of the two client-side shapes that break under the
    check: the retained turns are themselves unedited, but they were produced
    with the full history in front of them, and after the fold that history is
    a summary pair instead. The documented header-free remedy is to strip the
    thinking blocks from the retained turns, keeping their text; that is what
    ``_strip_thinking_from_turns`` does at every compaction boundary. The
    alternative — ``thinking.block_binding.prefix_mismatch_behavior:
    "drop_block"`` — is deliberately NOT used, because it requires the
    ``thinking-binding-controls-2026-08-01`` beta header and this route sends
    no beta headers by standing policy (see translation.anthropic.batch in
    config.yaml for the reasoning).

    Losing a retained turn's reasoning trace at a compaction costs nothing the
    translation needs: the chapter's visible English is what carries
    continuity forward, and it is kept intact.

    None of this applies to claude-opus-5 or claude-sonnet-5. They do not
    enforce the conversation-prefix check, so their ledgers keep every
    thinking block across a compaction exactly as they did before this route
    moved to Fable 5.1. The gate is the model id, not the route.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, work_dir: Path, volume_id: str, model: str, endpoint: str, config: Dict[str, Any]):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = str(model)
        # Resolved once, ahead of _load(), because the load-time self-heal
        # routes through the same gate.
        self.prefix_bound_thinking = self.model in PREFIX_BOUND_THINKING_MODELS
        self.endpoint = str(endpoint).rstrip("/")
        self.enabled = bool(config.get("enabled", True))
        self.recent_verbatim_chapters = max(1, int(config.get("recent_verbatim_chapters", 2) or 2))
        # Ceiling for the append-only replay window. The window grows by
        # appending until it passes this, then trims in ONE block back down to
        # recent_verbatim_chapters. A window that slid by one every chapter
        # would reshape the messages prefix every turn and never register a
        # cache hit; trimming in blocks keeps the prefix byte-stable between
        # trims, which is what makes the history breakpoint worth setting.
        self.max_verbatim_chapters = max(
            self.recent_verbatim_chapters, int(config.get("max_verbatim_chapters", 8) or 8)
        )
        self.context_window = max(1, int(config.get("context_window", 1_000_000) or 1_000_000))
        self.soft_notice_ratio = float(config.get("soft_notice_ratio", 0.60) or 0.60)
        self.compact_ratio = float(config.get("compact_ratio", 0.75) or 0.75)
        self.hard_trim_ratio = float(config.get("hard_trim_ratio", 0.90) or 0.90)
        # Context pressure below which max_verbatim_chapters does NOT trim.
        # Read without ``or``: 0.0 is a meaningful setting (restore the old
        # unconditional count ceiling) and ``x or default`` would discard it.
        raw_ceiling_ratio = config.get("verbatim_ceiling_ratio", 0.60)
        self.verbatim_ceiling_ratio = 0.60 if raw_ceiling_ratio is None else float(raw_ceiling_ratio)
        # How much of a folded chapter survives, from its opening and from its
        # ending. The tail is the half that matters to the chapter after it.
        self.summary_head_chars = max(0, int(config.get("summary_head_chars", 240) or 240))
        self.summary_tail_chars = max(0, int(config.get("summary_tail_chars", 480) or 480))
        raw_path = Path(str(config.get("persistence_file", ".context/anthropic_conversation.json")))
        self.state_path = raw_path if raw_path.is_absolute() else self.work_dir / raw_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._new_state()
        self._load()

    def _new_state(self) -> Dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "endpoint": self.endpoint,
            "turns": [],
            "summary": "",
            "compaction_events": [],
            # The system prefix these turns were produced under. context.xml
            # is rewritten by the DeepSeek safety fallback, so a resumed run
            # can legitimately carry a different prefix than the ledger on
            # disk — which invalidates every stored thinking block.
            "system_fingerprint": "",
        }

    def _load(self) -> None:
        if not self.enabled or not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if (
            isinstance(loaded, dict)
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
            and str(loaded.get("endpoint", "")).rstrip("/") == self.endpoint
        ):
            self.state = loaded
            self.state.setdefault("turns", [])
            self.state.setdefault("summary", "")
            self.state.setdefault("compaction_events", [])
            self.state.setdefault("system_fingerprint", "")
            # Self-heal a ledger written before the binding check existed: if
            # a summary is present, a compaction already happened, so any
            # thinking still sitting on the retained turns predates that fold
            # and would be rejected on replay. Strip once, here, rather than
            # discovering it as a 400 mid-volume.
            if str(self.state.get("summary") or "").strip():
                healed = self._strip_thinking_from_turns()
                if healed:
                    logger.info(
                        "[ANTHROPIC-CONVERSATION] stripped thinking from %d "
                        "post-compaction message(s) on load (preserved-thinking check)",
                        healed,
                    )

    @property
    def turns(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("turns", [])

    def bind_system(self, system_segments: Sequence[str], *, tool_fingerprint_extra: str = "") -> None:
        """Record the system prefix these turns were produced under, dropping
        replayable thinking if it has changed since.

        On a prefix-bound model the top-level ``system`` prompt is part of
        every thinking block's signature. context.xml is rewritten in place by
        the DeepSeek safety fallback (decision inheritance), so a resumed run
        can legitimately present a different system prefix than the ledger on
        disk was produced under — and replaying that ledger would then fail
        before any output. Stripping once at this boundary is the recovery
        Anthropic documents for exactly this case; the chapters' English is
        untouched.

        ``tool_fingerprint_extra`` folds the active tool configuration
        (advisor enabled/model, web_search enabled) into the same fingerprint.
        Anthropic's advisor documentation states the tool set is part of what
        binds a thinking block's signature on a prefix-bound model, exactly
        like the system prompt — so toggling ``advisor.enabled`` between runs
        on a claude-fable-5-1-executed volume needs the identical
        "strip thinking, don't fail" recovery a changed system prompt already
        gets here, not a raw 400 the first time the ledger is replayed.

        A no-op on claude-opus-5 / claude-sonnet-5: the fingerprint is still
        recorded (so a later switch to a bound model has something to compare
        against) but nothing is stripped.
        """
        fingerprint = _system_fingerprint(system_segments, tool_fingerprint_extra)
        recorded = str(self.state.get("system_fingerprint") or "")
        self.state["system_fingerprint"] = fingerprint
        if not self.enabled or not recorded or recorded == fingerprint or not self.turns:
            return
        stripped = self._strip_thinking_from_turns()
        logger.warning(
            "[ANTHROPIC-CONVERSATION] system prefix changed since this ledger was "
            "written (context.xml rewritten?); stripped thinking from %d message(s) "
            "across %d retained turn(s)",
            stripped,
            len(self.turns),
        )
        atomic_write_json(self.state_path, self.state)

    def estimated_tokens(self, messages: Any = None) -> int:
        serialized = json.dumps(self._all_replay_messages() if messages is None else messages, ensure_ascii=False)
        return max(1, len(serialized) // 4)

    def build_messages(
        self,
        user_prompt: str,
        recent_count: int | None = None,
        cache_control: Dict[str, Any] | None = None,
        compact: bool = True,
    ) -> List[Dict[str, Any]]:
        """Build the ``messages`` array: folded summary + replayed turns + current prompt.

        Every retained turn is replayed, not a trailing slice of them. The
        retained set only grows by appending until a block trim, so the prefix
        this produces is the previous chapter's prefix plus one turn, which is
        the shape prompt caching rewards. ``recent_count`` stays available as
        an explicit cap for callers that want one.

        ``cache_control`` closes that stable history with a breakpoint, so
        prior chapters are served as cache reads instead of being re-billed in
        full on every chapter.

        ``compact=False`` skips the compaction check for callers that must
        build several requests against ONE ledger state - a batch wave, whose
        members are only mutually consistent because their prefix is frozen.
        Those callers run the check once, via ``prepare_prefix``.
        """
        if compact:
            self._maybe_compact()
        if recent_count is None:
            replayed = list(self.turns)
        else:
            count = max(0, int(recent_count))
            replayed = list(self.turns[-count:]) if count else []
        messages: List[Dict[str, Any]] = []
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            messages.append(_user_text(f"<anthropic_conversation_summary>\n{summary}\n</anthropic_conversation_summary>"))
            messages.append(_assistant_text("Understood — continuing with that continuity established."))
        for turn in replayed:
            messages.append(_sanitize_message(turn.get("user_message")))
            messages.append(_sanitize_message(turn.get("assistant_message")))
        _apply_history_cache_breakpoint(messages, cache_control)
        messages.append(_user_text(user_prompt))
        return messages

    def prepare_prefix(self) -> None:
        """Run the compaction check once, ahead of a group of requests that
        must share a byte-identical prefix.

        ``build_messages`` compacts on every call, so a fold could land
        between two members of the same batch wave: members built before it
        carry a history the members built after it do not, defeating both the
        continuity the wave exists to provide and the single cache key it is
        billed against. The wave loop calls this once, then builds every
        request with ``compact=False``.
        """
        if not self.enabled:
            return
        before = len(self.turns)
        self._maybe_compact()
        if len(self.turns) != before:
            atomic_write_json(self.state_path, self.state)

    def continuity_state(self) -> Dict[str, Any]:
        """What continuity this ledger can actually supply for the next request.

        The chapter envelope used to assert that "earlier source_text
        envelopes" were present without naming one. On the batch path that
        assertion outruns the payload: every request in a wave is built
        against the same frozen history, so a chapter is told its
        predecessors are there while the ledger stops several chapters short.
        Returning the ids lets the envelope declare what it holds instead of
        implying it holds everything.
        """
        verbatim = [str(turn.get("chapter_id") or "").strip() for turn in self.turns]
        verbatim = [chapter_id for chapter_id in verbatim if chapter_id]
        summarized: List[str] = []
        for line in str(self.state.get("summary") or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            chapter_id = stripped[2:].split(":", 1)[0].strip()
            if chapter_id and chapter_id not in summarized:
                summarized.append(chapter_id)
        return {
            "verbatim_chapter_ids": verbatim,
            "summarized_chapter_ids": [c for c in summarized if c not in verbatim],
        }

    def commit(self, chapter_id: str, user_message: Dict[str, Any], assistant_message: Dict[str, Any], chapter_text: str, output_path: Path) -> None:
        if not self.enabled:
            return
        if not assistant_message or not assistant_message.get("content"):
            raise ValueError("Anthropic conversation commit requires the raw assistant message content")
        self.turns.append(
            {
                "chapter_id": chapter_id,
                "user_message": dict(user_message),
                "assistant_message": dict(assistant_message),
                "output_path": str(output_path),
            }
        )
        self._maybe_compact()
        atomic_write_json(self.state_path, self.state)

    def _all_replay_messages(self) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for turn in self.turns:
            messages.append(turn.get("user_message") or {})
            messages.append(turn.get("assistant_message") or {})
        return messages

    def _maybe_compact(self) -> None:
        if not self.turns:
            return
        tokens = self.estimated_tokens()
        ratio = tokens / float(self.context_window)
        # max_verbatim_chapters is a PROXY for context pressure, and on its own
        # a badly calibrated one. Measured on Vol.4 (21 chapters,
        # claude-fable-5-1): the ceiling fired twice at roughly 7% window
        # occupancy and razed the window to two turns each time, folding
        # eighteen chapters of finished English down to short excerpts. The
        # whole volume replayed verbatim estimates at 17% of the declared
        # window - it was never close to needing a trim.
        #
        # The accurate signal is measured two lines above, and the ceiling
        # never looked at it. It does now: trim on count only once the window
        # is genuinely under pressure. Set verbatim_ceiling_ratio: 0.0 to
        # restore the unconditional count cap.
        if len(self.turns) > self.max_verbatim_chapters and len(self.turns) > self.recent_verbatim_chapters:
            if ratio >= self.verbatim_ceiling_ratio:
                self._fold_turns_into_summary(self.turns[: -self.recent_verbatim_chapters])
                self.turns[:] = self.turns[-self.recent_verbatim_chapters :]
                self._record_compaction("verbatim_ceiling", ratio)
                return
            logger.debug(
                "[ANTHROPIC-CONVERSATION] %d turns exceed max_verbatim_chapters=%d, but "
                "context pressure is %.3f (< %.2f); retaining every turn verbatim",
                len(self.turns),
                self.max_verbatim_chapters,
                ratio,
                self.verbatim_ceiling_ratio,
            )
        if ratio >= self.hard_trim_ratio and len(self.turns) > self.recent_verbatim_chapters:
            dropped = self.turns[: -self.recent_verbatim_chapters]
            self._fold_turns_into_summary(dropped)
            self.turns[:] = self.turns[-self.recent_verbatim_chapters :]
            self._record_compaction("hard_trim", ratio)
            return
        if ratio >= self.compact_ratio and len(self.turns) > self.recent_verbatim_chapters:
            overflow = len(self.turns) - self.recent_verbatim_chapters
            self._fold_turns_into_summary(self.turns[:overflow])
            self.turns[:] = self.turns[overflow:]
            self._record_compaction("compact", ratio)
            return
        if ratio >= self.soft_notice_ratio:
            logger.info(
                "[ANTHROPIC-CONVERSATION] context pressure ratio=%.2f tokens≈%d limit=%d",
                ratio,
                tokens,
                self.context_window,
            )

    def _record_compaction(self, reason: str, ratio: float) -> None:
        # Every compaction branch in _maybe_compact trims and then calls this,
        # so stripping here covers all of them by construction rather than by
        # remembering to patch each branch. A fold rewrites what sits ahead of
        # the retained turns, which is precisely what invalidates their
        # thinking blocks on a prefix-bound model.
        stripped = self._strip_thinking_from_turns()
        self.state.setdefault("compaction_events", []).append(
            {
                "reason": reason,
                "ratio": round(ratio, 4),
                "remaining_turns": len(self.turns),
                "thinking_blocks_stripped": stripped,
            }
        )
        logger.info(
            "[ANTHROPIC-CONVERSATION] %s retained %d recent turns (thinking stripped from %d message(s))",
            reason,
            len(self.turns),
            stripped,
        )

    def _strip_thinking_from_turns(self) -> int:
        """Drop thinking/redacted_thinking from every retained turn, in place.

        Returns the number of messages actually altered. Gated on the model:
        claude-opus-5 and claude-sonnet-5 do not enforce the conversation
        binding check, and there is no reason to spend their reasoning traces
        on a constraint that is not theirs.
        """
        if not self.prefix_bound_thinking:
            return 0
        stripped = 0
        for turn in self.turns:
            for key in ("user_message", "assistant_message"):
                original = turn.get(key)
                reduced = _strip_thinking_blocks(original)
                if reduced is not original:
                    turn[key] = reduced
                    stripped += 1
        return stripped

    def _fold_turns_into_summary(self, turns: Iterable[Dict[str, Any]]) -> None:
        """Fold turns into the running summary, keeping each chapter's opening
        AND its ending.

        The excerpt was the first 240 characters and nothing else, which
        throws away the one part of a chapter the next chapter reaches back
        for: the callback, the cliffhanger, the line left hanging. Vol.4 CH03
        needed the closing joke of CH02 and recorded that it could not see it
        (THINKING/CHAPTER_03_THINKING.md). Head and tail both, or the fold
        keeps the shape of continuity without its substance.
        """
        lines: List[str] = []
        for turn in turns:
            blocks = normalize_anthropic_content((turn.get("assistant_message") or {}).get("content"))
            visible = " ".join(
                " ".join(block.text.split()) for block in blocks if block.type == "text"
            ).strip()
            chapter_id = str(turn.get("chapter_id") or "unknown")
            if not visible:
                lines.append(f"- {chapter_id}: (no visible text recorded)")
                continue
            if len(visible) <= self.summary_head_chars or not self.summary_tail_chars:
                lines.append(f"- {chapter_id}: {visible[: self.summary_head_chars] or visible}")
                continue
            head = visible[: self.summary_head_chars].rstrip()
            # Clamped so head and tail can never overlap on a short chapter.
            tail_start = max(self.summary_head_chars, len(visible) - self.summary_tail_chars)
            tail = visible[tail_start:].lstrip()
            lines.append(f"- {chapter_id}: OPENS: {head} [...] ENDS: {tail}")
        if not lines:
            return
        prior = str(self.state.get("summary") or "").strip()
        folded = "Prior chapter continuity:\n" + "\n".join(lines)
        self.state["summary"] = (prior + "\n" + folded).strip() if prior else folded


def _apply_history_cache_breakpoint(
    messages: List[Dict[str, Any]], cache_control: Dict[str, Any] | None
) -> None:
    """Close the replayed history with a cache breakpoint.

    Stamps the last text block of the most recent replayed assistant turn:
    the final byte of the stable prefix, immediately before the volatile
    current-chapter envelope. These message dicts are built fresh by
    ``_sanitize_message``/``_assistant_text`` on every call, so mutating them
    never writes back into the persisted ledger. A no-op when caching is off
    or when there is no replayed history yet to cache.
    """
    if not cache_control or not messages:
        return
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        for block in reversed(message.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = dict(cache_control)
                return
        return


def _system_fingerprint(system_segments: Sequence[str], tool_fingerprint_extra: str = "") -> str:
    """Stable digest of the system prefix plus the active tool configuration.
    NUL-joined so that concatenating segments differently, or a tool-config
    string that happens to share bytes with a segment, can never collide."""
    parts = [str(segment) for segment in system_segments]
    if tool_fingerprint_extra:
        parts.append(tool_fingerprint_extra)
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _strip_thinking_blocks(message: Any) -> Any:
    """Return ``message`` without its thinking blocks, or the original object
    unchanged when there is nothing to strip.

    Identity of the return value is the signal the caller counts on: a new
    dict means something was removed. The message is never emptied — an
    assistant turn stripped down to no content at all would be rejected by the
    API, so in that (unexpected: agent.py refuses a chapter with no visible
    text) case the original is kept and the anomaly logged.
    """
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    kept = [
        block
        for block in content
        if not (isinstance(block, dict) and str(block.get("type") or "") in _THINKING_BLOCK_TYPES)
    ]
    if len(kept) == len(content):
        return message
    if not kept:
        logger.warning(
            "[ANTHROPIC-CONVERSATION] refusing to strip a %r turn down to empty content; "
            "replaying it unchanged",
            message.get("role", "unknown"),
        )
        return message
    return {**message, "content": kept}


def _user_text(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_text(text: str) -> Dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _sanitize_message(message: Any) -> Dict[str, Any]:
    """Replay a stored message: thinking/redacted_thinking blocks are never
    rearranged, edited, or partially dropped, but each block is stripped
    down to the fields the Messages API actually accepts on an inbound
    message — self-healing for turns persisted before that stripping
    existed (see sanitize_replayable_block)."""
    if not isinstance(message, dict):
        return {"role": "user", "content": []}
    content = [
        sanitize_replayable_block(dict(block)) if isinstance(block, dict) else block
        for block in (message.get("content") or [])
    ]
    return {"role": message.get("role", "user"), "content": content}
