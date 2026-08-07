"""Persistent Qwen conversation ledger with lightweight compaction."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


class QwenConversationManager:
    SCHEMA_VERSION = "1.1"

    def __init__(self, work_dir: Path, volume_id: str, model: str, endpoint: str, config: Dict[str, Any]):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = model
        self.endpoint = endpoint.rstrip("/")
        self.enabled = bool(config.get("enabled", True))
        self.recent_verbatim_chapters = max(1, int(config.get("recent_verbatim_chapters", 2) or 2))
        self.context_window = max(1, int(config.get("context_window", 1_000_000) or 1_000_000))
        self.soft_notice_ratio = float(config.get("soft_notice_ratio", 0.60) or 0.60)
        self.compact_ratio = float(config.get("compact_ratio", 0.75) or 0.75)
        self.hard_trim_ratio = float(config.get("hard_trim_ratio", 0.90) or 0.90)
        raw = Path(str(config.get("persistence_file", ".context/qwen_conversation.json")))
        self.state_path = raw if raw.is_absolute() else self.work_dir / raw
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
        identity = (
            isinstance(loaded, dict)
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
            and str(loaded.get("endpoint", "")).rstrip("/") == self.endpoint
        )
        if identity:
            self.state = loaded
            self.state.setdefault("summary", "")
            self.state.setdefault("compaction_events", [])

    @property
    def turns(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("turns", [])

    def estimated_tokens(self, messages: Optional[List[Dict[str, Any]]] = None) -> int:
        payload = messages if messages is not None else self._all_messages()
        text = json.dumps(payload, ensure_ascii=False)
        return max(1, len(text) // 4)

    def messages(self, system: str, user: str, recent_count: Optional[int] = None) -> List[Dict[str, Any]]:
        _ = system  # system is sent as the top-level Anthropic parameter, not a message role
        count = self.recent_verbatim_chapters if recent_count is None else max(0, int(recent_count))
        self._maybe_compact()
        messages: List[Dict[str, Any]] = []
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            messages.append(
                {
                    "role": "user",
                    "content": f"<qwen_conversation_summary>\n{summary}\n</qwen_conversation_summary>",
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": "Acknowledged. I will preserve continuity from the summary and recent turns.",
                }
            )
        recent = self.turns[-count:] if count else []
        for turn in recent:
            messages.extend(turn.get("messages") or [])
        messages.append({"role": "user", "content": user})
        return messages

    def commit(
        self,
        chapter_id: str,
        messages: List[Dict[str, Any]],
        assistant_content: str,
        output_path: Path,
        assistant_blocks: Any = None,
    ) -> None:
        if not self.enabled:
            return
        # Qwen's Anthropic route ignores reasoning_content in assistant
        # messages by default — same as the documented OpenAI-compatible
        # rule: "retain only the content field and ignore the
        # reasoning_content field." Including thinking blocks wastes input
        # tokens on content the model won't read (preserve_thinking isn't
        # supported on this route). Strip them before storing.
        content = _strip_thinking(assistant_blocks) or assistant_content
        latest_user = None
        for message in reversed(messages):
            if message.get("role") == "user":
                latest_user = message
                break
        turn_messages = []
        if latest_user is not None:
            turn_messages.append(latest_user)
        turn_messages.append({"role": "assistant", "content": content})
        self.turns.append(
            {
                "chapter_id": chapter_id,
                "messages": turn_messages,
                "output_path": str(output_path),
                "output_sha256": hashlib.sha256(assistant_content.encode("utf-8")).hexdigest(),
            }
        )
        self._maybe_compact()
        atomic_write_json(self.state_path, self.state)

    def _all_messages(self) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for turn in self.turns:
            messages.extend(turn.get("messages") or [])
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
            self.state.setdefault("compaction_events", []).append(
                {"reason": "hard_trim", "ratio": round(ratio, 4), "remaining_turns": len(self.turns)}
            )
            logger.info("[QWEN-CONVERSATION] hard-trimmed history to %d recent turns", len(self.turns))
            return
        if ratio >= self.compact_ratio and len(self.turns) > self.recent_verbatim_chapters:
            overflow = len(self.turns) - self.recent_verbatim_chapters
            dropped = self.turns[:overflow]
            self._fold_turns_into_summary(dropped)
            self.turns[:] = self.turns[overflow:]
            self.state.setdefault("compaction_events", []).append(
                {"reason": "compact", "ratio": round(ratio, 4), "remaining_turns": len(self.turns)}
            )
            logger.info("[QWEN-CONVERSATION] compacted %d older turns into summary", overflow)
            return
        if ratio >= self.soft_notice_ratio:
            logger.info(
                "[QWEN-CONVERSATION] context pressure notice ratio=%.2f tokens≈%d window=%d",
                ratio,
                tokens,
                self.context_window,
            )

    def _fold_turns_into_summary(self, turns: List[Dict[str, Any]]) -> None:
        lines = []
        for turn in turns:
            chapter_id = turn.get("chapter_id") or "unknown"
            assistant = ""
            for message in turn.get("messages") or []:
                if message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, list):
                        assistant = " ".join(
                            str(block.get("text") or block.get("thinking") or "")
                            for block in content
                            if isinstance(block, dict)
                        )
                    else:
                        assistant = str(content or "")
            excerpt = " ".join(assistant.split())[:240]
            lines.append(f"- {chapter_id}: {excerpt}")
        prior = str(self.state.get("summary") or "").strip()
        folded = "Prior chapter continuity:\n" + "\n".join(lines)
        self.state["summary"] = (prior + "\n" + folded).strip() if prior else folded


def _strip_thinking(blocks: Any) -> Any:
    """Remove thinking blocks from assistant content blocks.

    Qwen ignores reasoning_content in assistant messages by default.
    Including them wastes input tokens — the model won't read them,
    and preserve_thinking isn't supported on the Anthropic-compatible route.
    """
    if isinstance(blocks, list):
        filtered = [b for b in blocks if isinstance(b, dict) and b.get("type") != "thinking"]
        return filtered if filtered else None
    return blocks if blocks else None
