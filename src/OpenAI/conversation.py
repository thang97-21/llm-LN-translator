"""Persistent local replay ledger for native OpenAI Responses conversations."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.Deepseek.common.atomic_io import atomic_write_json
from src.OpenAI.response import normalize_openai_output

logger = logging.getLogger(__name__)


class OpenAIConversationManager:
    """Retain local input/output items required for ``store: false`` replay."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, work_dir: Path, volume_id: str, model: str, endpoint: str, config: Dict[str, Any]):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.endpoint = str(endpoint).rstrip("/")
        self.enabled = bool(config.get("enabled", True))
        self.recent_verbatim_chapters = max(1, int(config.get("recent_verbatim_chapters", 2) or 2))
        self.max_verbatim_chapters = max(
            self.recent_verbatim_chapters, int(config.get("max_verbatim_chapters", 8) or 8)
        )
        self.context_window = max(1, int(config.get("context_window", 1_050_000) or 1_050_000))
        self.max_input_tokens = max(1, int(config.get("max_input_tokens", 922_000) or 922_000))
        self.soft_notice_ratio = float(config.get("soft_notice_ratio", 0.60) or 0.60)
        self.compact_ratio = float(config.get("compact_ratio", 0.75) or 0.75)
        self.hard_trim_ratio = float(config.get("hard_trim_ratio", 0.90) or 0.90)
        raw_ceiling_ratio = config.get("verbatim_ceiling_ratio", 0.60)
        self.verbatim_ceiling_ratio = 0.60 if raw_ceiling_ratio is None else float(raw_ceiling_ratio)
        self.summary_head_chars = max(0, int(config.get("summary_head_chars", 240) or 240))
        self.summary_tail_chars = max(0, int(config.get("summary_tail_chars", 480) or 480))
        raw_path = Path(str(config.get("persistence_file", ".context/openai_conversation.json")))
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

    def estimated_tokens(self, items: Any = None) -> int:
        serialized = json.dumps(self._all_replay_items() if items is None else items, ensure_ascii=False)
        return max(1, len(serialized) // 4)

    def input_items(
        self,
        system_instruction: str,
        user_prompt: str,
        recent_count: int | None = None,
        compact: bool = True,
    ) -> List[Dict[str, Any]]:
        """Build native Responses input with complete old output items for replay.

        ``recent_count=None`` intentionally replays every retained turn. The
        batch wave builder uses that form after ``prepare_prefix()`` so all
        requests share one frozen prefix; synchronous callers can pass an
        explicit count when they deliberately want a shorter replay window.
        """
        count = None if recent_count is None else max(0, int(recent_count))
        if compact:
            self._maybe_compact()
        items: List[Dict[str, Any]] = [
            {"role": "developer", "content": [{"type": "input_text", "text": system_instruction}]}
        ]
        summary = str(self.state.get("summary") or "").strip()
        if summary:
            items.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"<openai_conversation_summary>\n{summary}\n</openai_conversation_summary>",
                        }
                    ],
                }
            )
        replayed = self.turns if count is None else (self.turns[-count:] if count else [])
        for turn in replayed:
            items.extend(_sanitize_for_request(_items(turn.get("input_items"))))
            items.extend(_sanitize_for_request(_items(turn.get("response_output"))))
        items.append(_user_input(user_prompt))
        return items

    def prepare_prefix(self) -> None:
        """Compact once before building a batch wave's frozen prefix."""
        if not self.enabled:
            return
        before = (len(self.turns), str(self.state.get("summary") or ""), len(self.state.get("compaction_events") or []))
        self._maybe_compact()
        after = (len(self.turns), str(self.state.get("summary") or ""), len(self.state.get("compaction_events") or []))
        if after != before:
            atomic_write_json(self.state_path, self.state)

    def continuity_state(self) -> Dict[str, Any]:
        """Describe exactly which prior chapters the next request can replay."""
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
            "summarized_chapter_ids": [chapter_id for chapter_id in summarized if chapter_id not in verbatim],
        }

    def commit(self, chapter_id: str, user_input: Any, response_output: Any, chapter_text: str, output_path: Path) -> None:
        """Persist a turn's replay items.

        ``user_input`` is the complete input-side item sequence for this turn
        (the chapter's user turn plus any continuation exchanges) as built by
        the caller — already a list, not a single dict. ``_items()`` also
        accepts a bare dict for callers that only ever send one item.
        """
        if not self.enabled:
            return
        raw_output = list(_items(response_output))
        if not raw_output:
            raise ValueError("OpenAI conversation commit requires the raw Responses output array")
        self.turns.append(
            {
                "chapter_id": chapter_id,
                "input_items": list(_items(user_input)),
                "response_output": raw_output,
                "output_path": str(output_path),
                "output_sha256": hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
            }
        )
        self._maybe_compact()
        atomic_write_json(self.state_path, self.state)

    def _all_replay_items(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for turn in self.turns:
            items.extend(_items(turn.get("input_items")))
            items.extend(_items(turn.get("response_output")))
        return items

    def _maybe_compact(self) -> None:
        if not self.turns:
            return
        tokens = self.estimated_tokens()
        limit = min(self.context_window, self.max_input_tokens)
        ratio = tokens / float(limit)
        if len(self.turns) > self.max_verbatim_chapters and len(self.turns) > self.recent_verbatim_chapters:
            if ratio >= self.verbatim_ceiling_ratio:
                self._fold_turns_into_summary(self.turns[: -self.recent_verbatim_chapters])
                self.turns[:] = self.turns[-self.recent_verbatim_chapters :]
                self._record_compaction("verbatim_ceiling", ratio)
                return
            logger.debug(
                "[OPENAI-CONVERSATION] %d turns exceed max_verbatim_chapters=%d, but "
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
                "[OPENAI-CONVERSATION] context pressure ratio=%.2f tokens≈%d limit=%d",
                ratio,
                tokens,
                limit,
            )

    def _record_compaction(self, reason: str, ratio: float) -> None:
        self.state.setdefault("compaction_events", []).append(
            {"reason": reason, "ratio": round(ratio, 4), "remaining_turns": len(self.turns)}
        )
        logger.info("[OPENAI-CONVERSATION] %s retained %d recent turns", reason, len(self.turns))

    def _fold_turns_into_summary(self, turns: Iterable[Dict[str, Any]]) -> None:
        lines: List[str] = []
        for turn in turns:
            blocks, _ = normalize_openai_output(turn.get("response_output"))
            visible = " ".join(block.text for block in blocks if block.type == "text")
            visible = " ".join(visible.split())
            chapter_id = turn.get("chapter_id") or "unknown"
            if not visible:
                lines.append(f"- {chapter_id}: (no visible text recorded)")
                continue
            if len(visible) <= self.summary_head_chars or not self.summary_tail_chars:
                lines.append(f"- {chapter_id}: {visible[: self.summary_head_chars] or visible}")
                continue
            head = visible[: self.summary_head_chars].rstrip()
            tail_start = max(self.summary_head_chars, len(visible) - self.summary_tail_chars)
            tail = visible[tail_start:].lstrip()
            lines.append(f"- {chapter_id}: OPENS: {head} [...] ENDS: {tail}")
        if not lines:
            return
        prior = str(self.state.get("summary") or "").strip()
        folded = "Prior chapter continuity:\n" + "\n".join(lines)
        self.state["summary"] = (prior + "\n" + folded).strip() if prior else folded


def _user_input(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _items(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return [dict(value)] if isinstance(value, dict) else []


def _sanitize_for_request(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip response-only metadata before an item re-enters a request's input array.

    ``status`` is populated by the API on returned output items ("Populated
    when items are returned via API" per the Responses reference on every
    item type) and is not an accepted field when that same item is echoed
    back as input on a later turn — the live API rejects it with
    ``Unknown parameter: 'input[N].status'``. The persisted ledger keeps the
    full, faithful item (including status) for debugging; only the copy
    placed into an outgoing request is sanitized here.
    """
    sanitized = []
    for item in items:
        copy = dict(item)
        copy.pop("status", None)
        sanitized.append(copy)
    return sanitized
