"""Persistent GLM Chat Completions conversation ledger."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json


class GLMConversationManager:
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
        raw = Path(str(config.get("persistence_file", ".context/glm_conversation.json")))
        self.state_path = raw if raw.is_absolute() else self.work_dir / raw
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._new_state()
        self._load()

    def _new_state(self) -> Dict[str, Any]:
        return {"schema_version": self.SCHEMA_VERSION, "volume_id": self.volume_id, "model": self.model, "endpoint": self.endpoint, "turns": [], "summary": "", "compaction_events": []}

    def _load(self) -> None:
        if not self.enabled or not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict) and str(loaded.get("volume_id")) == self.volume_id and str(loaded.get("model")) == self.model and str(loaded.get("endpoint", "")).rstrip("/") == self.endpoint:
            self.state = loaded

    @property
    def turns(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("turns", [])

    def estimated_tokens(self, messages: Optional[List[Dict[str, Any]]] = None) -> int:
        return max(1, len(json.dumps(self._all_messages() if messages is None else messages, ensure_ascii=False)) // 4)

    def messages(self, system: str, user: str, recent_count: Optional[int] = None) -> List[Dict[str, Any]]:
        count = self.recent_verbatim_chapters if recent_count is None else max(0, int(recent_count))
        result = [{"role": "system", "content": system}]
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            result.extend([
                {"role": "user", "content": f"<glm_conversation_summary>\n{summary}\n</glm_conversation_summary>"},
                {"role": "assistant", "content": "Acknowledged. I will preserve continuity from the summary and recent turns."},
            ])
        for turn in self.turns[-count:] if count else []:
            result.extend(turn.get("messages") or [])
        result.append({"role": "user", "content": user})
        return result

    def commit(self, chapter_id: str, messages: List[Dict[str, Any]], assistant_content: str, output_path: Path) -> None:
        if not self.enabled:
            return
        turn = [dict(item) for item in messages if isinstance(item, dict)]
        user = next((item for item in reversed(turn) if item.get("role") == "user"), {"role": "user", "content": ""})
        self.turns.append({"chapter_id": chapter_id, "messages": [user, {"role": "assistant", "content": assistant_content}], "output_path": str(output_path), "output_sha256": hashlib.sha256(assistant_content.encode("utf-8")).hexdigest()})
        self._maybe_compact()
        atomic_write_json(self.state_path, self.state)

    def _all_messages(self) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for turn in self.turns:
            result.extend(turn.get("messages") or [])
        return result

    def _maybe_compact(self) -> None:
        if not self.turns:
            return
        ratio = self.estimated_tokens() / float(self.context_window)
        if ratio < self.compact_ratio or len(self.turns) <= self.recent_verbatim_chapters:
            return
        overflow = len(self.turns) - self.recent_verbatim_chapters
        dropped = self.turns[:overflow]
        lines = []
        for turn in dropped:
            assistant = next((str(m.get("content") or "") for m in reversed(turn.get("messages") or []) if m.get("role") == "assistant"), "")
            lines.append(f"- {turn.get('chapter_id', 'unknown')}: {' '.join(assistant.split())[:240]}")
        self.state["summary"] = (str(self.state.get("summary") or "") + "\nPrior chapter continuity:\n" + "\n".join(lines)).strip()
        self.turns[:] = self.turns[overflow:]
        self.state.setdefault("compaction_events", []).append({"reason": "compact", "ratio": round(ratio, 4), "remaining_turns": len(self.turns)})
