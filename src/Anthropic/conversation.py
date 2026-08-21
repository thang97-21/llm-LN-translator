"""Persistent messages-array conversation ledger for the Anthropic route."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.Anthropic.response import normalize_anthropic_content, sanitize_replayable_block
from src.Deepseek.common.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


class AnthropicConversationManager:
    """Retain a role:user/assistant message ledger across chapters.

    Unlike OpenAI's Responses "input items" ledger, the Messages API is
    stateless per request: every call resends the full conversation as a
    ``messages`` array. ``thinking``/``redacted_thinking`` blocks inside a
    stored assistant turn are passed back byte-exact — never re-synthesized
    — per Anthropic's "Preserving thinking blocks" contract.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, work_dir: Path, volume_id: str, model: str, endpoint: str, config: Dict[str, Any]):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.endpoint = str(endpoint).rstrip("/")
        self.enabled = bool(config.get("enabled", True))
        self.recent_verbatim_chapters = max(1, int(config.get("recent_verbatim_chapters", 2) or 2))
        self.context_window = max(1, int(config.get("context_window", 1_000_000) or 1_000_000))
        self.soft_notice_ratio = float(config.get("soft_notice_ratio", 0.60) or 0.60)
        self.compact_ratio = float(config.get("compact_ratio", 0.75) or 0.75)
        self.hard_trim_ratio = float(config.get("hard_trim_ratio", 0.90) or 0.90)
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

    @property
    def turns(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("turns", [])

    def estimated_tokens(self, messages: Any = None) -> int:
        serialized = json.dumps(self._all_replay_messages() if messages is None else messages, ensure_ascii=False)
        return max(1, len(serialized) // 4)

    def build_messages(self, user_prompt: str, recent_count: int | None = None) -> List[Dict[str, Any]]:
        """Build the ``messages`` array: folded summary + recent verbatim turns + current prompt."""
        count = self.recent_verbatim_chapters if recent_count is None else max(0, int(recent_count))
        self._maybe_compact()
        messages: List[Dict[str, Any]] = []
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            messages.append(_user_text(f"<anthropic_conversation_summary>\n{summary}\n</anthropic_conversation_summary>"))
            messages.append(_assistant_text("Understood — continuing with that continuity established."))
        for turn in self.turns[-count:] if count else []:
            messages.append(_sanitize_message(turn.get("user_message")))
            messages.append(_sanitize_message(turn.get("assistant_message")))
        messages.append(_user_text(user_prompt))
        return messages

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
        self.state.setdefault("compaction_events", []).append(
            {"reason": reason, "ratio": round(ratio, 4), "remaining_turns": len(self.turns)}
        )
        logger.info("[ANTHROPIC-CONVERSATION] %s retained %d recent turns", reason, len(self.turns))

    def _fold_turns_into_summary(self, turns: Iterable[Dict[str, Any]]) -> None:
        lines: List[str] = []
        for turn in turns:
            blocks = normalize_anthropic_content((turn.get("assistant_message") or {}).get("content"))
            visible = " ".join(block.text for block in blocks if block.type == "text")
            excerpt = " ".join(visible.split())[:240]
            lines.append(f"- {turn.get('chapter_id') or 'unknown'}: {excerpt}")
        if not lines:
            return
        prior = str(self.state.get("summary") or "").strip()
        folded = "Prior chapter continuity:\n" + "\n".join(lines)
        self.state["summary"] = (prior + "\n" + folded).strip() if prior else folded


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
