"""Persistent, cache-aware conversation state for DeepSeek Phase 2.

DeepSeek's Anthropic-compatible Messages endpoint is stateless.  This module
owns the current-volume transcript, persists it atomically, and builds the
append-only ``messages`` prefix submitted with each chapter.  Failed candidates
are staged by the client and committed only after ChapterProcessor accepts and
writes the chapter.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.Deepseek.common.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


_CHECKPOINT_REQUIRED_SECTIONS = {
    "chapter_coverage",
    "plot_state",
    "relationship_state",
    "unresolved_threads",
    "names_and_terms",
    "voice_and_pov",
    "callbacks",
    "translation_decisions",
    "anchor_excerpts",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _json_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeepSeekConversationManager:
    """Own one persisted, current-volume DeepSeek conversation."""

    SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        *,
        work_dir: Path,
        volume_id: str,
        model: str,
        endpoint: str,
        config: Optional[Dict[str, Any]] = None,
        token_counter: Optional[Callable[[str], int]] = None,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.endpoint = str(endpoint).rstrip("/")
        self.config = dict(config or {})
        self.conversation_kind = str(
            self.config.get("conversation_kind", "translation")
        ).strip().lower() or "translation"
        self.enabled = bool(self.config.get("enabled", True))
        self.recent_verbatim_chapters = max(
            1, int(self.config.get("recent_verbatim_chapters", 3) or 3)
        )
        self.checkpoint_trigger_ratio = min(
            0.98,
            max(0.10, float(self.config.get("checkpoint_trigger_ratio", 0.85) or 0.85)),
        )
        # Compaction ladder.  One rung straight to the paid checkpoint means the
        # only response to context pressure is the expensive, prefix-destroying
        # one.  These rungs sit below the trigger so cheaper measures run first
        # and the fold is skipped outright when they alone clear it.  Each is
        # clamped against its neighbour so a misconfigured file cannot invert
        # the ladder and make an upper rung fire before a lower one.
        self.soft_notice_ratio = min(
            self.checkpoint_trigger_ratio,
            max(0.05, float(self.config.get("soft_notice_ratio", 0.60) or 0.60)),
        )
        self.tool_snip_ratio = min(
            self.checkpoint_trigger_ratio,
            max(
                self.soft_notice_ratio,
                float(self.config.get("tool_snip_ratio", 0.70) or 0.70),
            ),
        )
        # Force rung: overrides the stuck guard.  Without it the only override
        # is the hard capacity ceiling, which is the turn that would otherwise
        # raise — the guard would release exactly one turn too late.
        self.checkpoint_force_ratio = min(
            1.0,
            max(
                self.checkpoint_trigger_ratio,
                float(self.config.get("checkpoint_force_ratio", 0.95) or 0.95),
            ),
        )
        self.checkpoint_max_output_tokens = max(
            1024, int(self.config.get("checkpoint_max_output_tokens", 32000) or 32000)
        )
        self.fail_closed = bool(
            self.config.get("fail_closed_on_checkpoint_error", True)
        )
        self.context_window = max(
            1, int(self.config.get("context_window", 1_000_000) or 1_000_000)
        )
        self.cache_user_id = bool(self.config.get("cache_user_id", True))
        self.include_exact_jp_task = bool(
            self.config.get("include_exact_jp_task", True)
        )
        # Stuck guard: checkpointing rewrites the prefix, so a checkpoint firing on
        # consecutive turns resets the cache every turn and the hit ratio never
        # recovers.  After this many back-to-back checkpoints, pause automatic
        # checkpointing and let the prefix grow append-only instead.  Set <= 0 to
        # disable the guard entirely.
        self.checkpoint_stuck_guard_turns = int(
            self.config.get("checkpoint_stuck_guard_turns", 2) or 0
        )
        self._token_counter = token_counter or (lambda text: max(1, len(text or "") // 4))

        raw_path = str(
            self.config.get(
                "persistence_file", ".context/deepseek_conversation.json"
            )
        ).strip()
        candidate = Path(raw_path)
        self.state_path = candidate if candidate.is_absolute() else self.work_dir / candidate
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        self.state: Dict[str, Any] = self._new_state()
        self._load()
        self._verify_canonical_outputs()

    def _new_state(self) -> Dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "endpoint": self.endpoint,
            "conversation_kind": self.conversation_kind,
            "system_hash": "",
            "prefix_shape": {},
            "prefix_change_reasons": [],
            "checkpoint_generation": 0,
            "checkpoint": None,
            "checkpoint_chapter_ids": [],
            "ordered_chapter_ids": [],
            "turn_archive": [],
            "turns": [],
            "cache_telemetry": {},
            "cache_reset_reason": None,
            "last_cache_reset_reason": None,
            "cache_reset_events": [],
            "last_checkpoint_telemetry": {},
            "consecutive_checkpoints": 0,
        }

    def _record_cache_reset(self, reason: str) -> None:
        self.state["cache_reset_reason"] = reason
        self.state["last_cache_reset_reason"] = reason
        events = self.state.setdefault("cache_reset_events", [])
        events.append(
            {
                "reason": reason,
                "checkpoint_generation": int(
                    self.state.get("checkpoint_generation", 0) or 0
                ),
                "represented_chapters": list(self.represented_chapter_ids),
            }
        )

    @property
    def metadata_user_id(self) -> Optional[str]:
        if not self.cache_user_id:
            return None
        digest = hashlib.sha256(self.volume_id.encode("utf-8")).hexdigest()[:24]
        return f"mtls-{digest}"

    @property
    def turns(self) -> List[Dict[str, Any]]:
        raw = self.state.setdefault("turns", [])
        return raw if isinstance(raw, list) else []

    @property
    def committed_chapter_ids(self) -> List[str]:
        return [
            str(item.get("chapter_id"))
            for item in self.turns
            if isinstance(item, dict) and item.get("chapter_id")
        ]

    @property
    def represented_chapter_ids(self) -> List[str]:
        return [
            str(value)
            for value in self.state.get("ordered_chapter_ids", [])
            if value
        ]

    def _load(self) -> None:
        if not self.enabled or not self.state_path.exists():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[DEEPSEEK-CONVERSATION] Failed loading %s: %s; starting clean.",
                self.state_path,
                exc,
            )
            self._record_cache_reset("state_load_failed")
            return
        if not isinstance(loaded, dict):
            self._record_cache_reset("state_not_object")
            return
        identity = (
            loaded.get("schema_version") == self.SCHEMA_VERSION
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
            and str(loaded.get("endpoint", "")).rstrip("/") == self.endpoint
            and str(loaded.get("conversation_kind", "translation")) == self.conversation_kind
        )
        if not identity:
            logger.warning(
                "[DEEPSEEK-CONVERSATION] State identity changed "
                "(schema/volume/model/endpoint); starting a new prefix."
            )
            self._record_cache_reset("state_identity_changed")
            return
        self.state = loaded
        if not isinstance(self.state.get("turn_archive"), list):
            self.state["turn_archive"] = list(self.state.get("turns") or [])
        if not isinstance(self.state.get("ordered_chapter_ids"), list):
            self.state["ordered_chapter_ids"] = [
                *list(self.state.get("checkpoint_chapter_ids") or []),
                *[
                    turn.get("chapter_id")
                    for turn in self.state.get("turns", [])
                    if isinstance(turn, dict) and turn.get("chapter_id")
                ],
            ]
        logger.info(
            "[DEEPSEEK-CONVERSATION] Resumed generation=%d, committed=%s",
            int(self.state.get("checkpoint_generation", 0) or 0),
            ",".join(self.represented_chapter_ids) or "none",
        )

    def persist(self) -> None:
        if self.enabled:
            atomic_write_json(self.state_path, self.state)

    def _resolve_output_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        return path if path.is_absolute() else self.work_dir / path

    def _verify_canonical_outputs(self) -> None:
        """Refresh assistant text when an accepted EN file changed on disk."""
        changed: List[str] = []
        archive = self.state.setdefault("turn_archive", [])
        for turn in archive:
            if not isinstance(turn, dict):
                continue
            raw_path = str(turn.get("output_path") or "").strip()
            if not raw_path:
                continue
            path = self._resolve_output_path(raw_path)
            if not path.exists():
                continue
            try:
                canonical = path.read_text(encoding="utf-8")
            except Exception:
                continue
            canonical_hash = _sha256_text(canonical)
            prior_hash = str(turn.get("canonical_output_sha256") or "")
            if prior_hash and canonical_hash == prior_hash:
                continue
            turn["assistant_response"] = canonical
            turn["assistant_response_sha256"] = canonical_hash
            turn["canonical_output_sha256"] = canonical_hash
            turn["assistant_source"] = "canonical_file_rebuild"
            changed.append(str(turn.get("chapter_id") or "unknown"))
            for active_turn in self.turns:
                if active_turn.get("chapter_id") == turn.get("chapter_id"):
                    active_turn.update(turn)
        if changed:
            if any(
                chapter_id in set(self.state.get("checkpoint_chapter_ids") or [])
                for chapter_id in changed
            ):
                self.state["checkpoint"] = None
                self.state["checkpoint_chapter_ids"] = []
                self.state["turns"] = list(archive)
            self._record_cache_reset(
                "canonical_output_changed:" + ",".join(changed)
            )
            self.persist()
            logger.warning(
                "[DEEPSEEK-CONVERSATION] Rebuilt changed canonical outputs for %s; "
                "the next request will establish a new cache prefix.",
                ", ".join(changed),
            )

    # ── Prefix shape: separated hashing + miss attribution ──────────────────
    #
    # DeepSeek's prefix cache is automatic — a single byte of drift anywhere in
    # the cached span silently costs the hit with no error and no attribution.
    # A single hash over the whole system blob can only say "something moved."
    # Hashing the system text and the tool schemas independently, and carrying
    # the checkpoint generation as a third axis, turns that into "the TOOL
    # SCHEMAS moved", which is the difference between a metric and a lead.

    @staticmethod
    def _system_hash(system: Any) -> str:
        return _sha256_text(_json_stable(system))

    @staticmethod
    def _normalize_tool_schemas(
        tools: Optional[Sequence[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Order-stable view of the tool schemas, for HASHING ONLY.

        Never feed this back into the request payload: reordering what is
        actually sent would itself break the prefix this exists to protect.

        MTLS builds its tool list from a module-level dict literal and Python
        dicts are insertion-ordered, so tool order is already deterministic
        here.  This sort is defensive hardening against a future caller that
        assembles tools from an unordered source — not a fix for a live defect.
        """
        normalized: List[Dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            schema = tool.get("input_schema")
            if schema is None:
                schema = tool.get("parameters")
            normalized.append(
                {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "schema": schema,
                }
            )
        normalized.sort(
            key=lambda item: (
                item["name"],
                item["description"],
                _json_stable(item["schema"]),
            )
        )
        return normalized

    def _tool_schema_token_costs(
        self, normalized_tools: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Per-tool prefix cost, so an expensive schema is visible before it hurts."""
        costs: List[Dict[str, Any]] = []
        for tool in normalized_tools:
            serialized = _json_stable(tool)
            costs.append(
                {
                    "name": tool.get("name", ""),
                    "tokens": self._token_counter(serialized) if serialized else 0,
                }
            )
        return costs

    def _capture_prefix_shape(
        self,
        system: Any,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        normalized_tools = self._normalize_tool_schemas(tools)
        system_hash = self._system_hash(system)
        # Empty rather than a hash-of-nothing: distinguishes "no tools this turn"
        # from "tools changed", so a tool-less turn cannot fake a drift signal.
        tools_hash = (
            _sha256_text(_json_stable(normalized_tools)) if normalized_tools else ""
        )
        return {
            "system_hash": system_hash,
            "tools_hash": tools_hash,
            # Hash-of-hashes rather than hash-of-content: equivalent for change
            # detection, and avoids re-serializing the full system blob.
            "prefix_hash": _sha256_text(
                _json_stable({"system": system_hash, "tools": tools_hash})
            ),
            # MTLS's analogue of a log-rewrite version: the checkpoint is the one
            # deliberate cache-reset point in the conversation.
            "checkpoint_generation": int(
                self.state.get("checkpoint_generation", 0) or 0
            ),
            "tool_schema_tokens": self._tool_schema_token_costs(normalized_tools),
        }

    @staticmethod
    def _compare_prefix_shape(
        previous: Optional[Dict[str, Any]],
        current: Dict[str, Any],
    ) -> List[str]:
        """Named reasons a prefix moved between two turns.

        Every axis is guarded on the previous value being present, so a cold
        first turn — or a legacy state file that predates an axis — reports no
        change rather than a spurious one.
        """
        if not previous:
            return []
        reasons: List[str] = []
        if previous.get("system_hash") and previous.get("system_hash") != current.get(
            "system_hash"
        ):
            reasons.append("system")
        if previous.get("tools_hash") and previous.get("tools_hash") != current.get(
            "tools_hash"
        ):
            reasons.append("tools")
        previous_generation = previous.get("checkpoint_generation")
        if (
            previous_generation is not None
            and previous_generation != current.get("checkpoint_generation")
        ):
            reasons.append("checkpoint_generation")
        return reasons

    def _set_prefix_shape(
        self,
        system: Any,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[str]:
        current = self._capture_prefix_shape(system, tools)
        previous = dict(self.state.get("prefix_shape") or {})
        if not previous and self.state.get("system_hash"):
            # State written before prefix_shape existed: fall back to the flat
            # system hash so an in-flight volume keeps attributing system drift
            # across the upgrade instead of silently starting cold.
            previous = {"system_hash": str(self.state.get("system_hash") or "")}
        reasons = self._compare_prefix_shape(previous, current)
        if "system" in reasons:
            self._record_cache_reset("stable_system_changed")
        if "tools" in reasons:
            self._record_cache_reset("stable_tools_changed")
        if reasons:
            logger.warning(
                "[DEEPSEEK-CONVERSATION] Cached prefix changed (%s); conversation "
                "content is retained but the next prefix will miss cache.",
                ", ".join(reasons),
            )
        self.state["prefix_shape"] = current
        self.state["prefix_change_reasons"] = reasons
        self.state["system_hash"] = current["system_hash"]
        return reasons

    def _set_system(self, system: Any) -> None:
        """System-only shape capture (kept for callers that carry no tools)."""
        self._set_prefix_shape(system, tools=None)

    def cache_miss_attribution(
        self,
        *,
        cache_read_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> Dict[str, Any]:
        """Pair the last observed prefix drift with the usage it produced.

        Lets the client's low-hit-ratio warning name the component that moved
        instead of pointing vaguely at "prompt prefix stability".
        """
        reasons = list(self.state.get("prefix_change_reasons") or [])
        denominator = int(cache_read_tokens or 0) + int(cache_miss_tokens or 0)
        return {
            "prefix_changed": bool(reasons),
            "prefix_change_reasons": reasons,
            "cache_hit_tokens": int(cache_read_tokens or 0),
            "cache_miss_tokens": int(cache_miss_tokens or 0),
            "cache_hit_ratio": (
                int(cache_read_tokens or 0) / denominator if denominator > 0 else 0.0
            ),
            "checkpoint_generation": int(
                self.state.get("checkpoint_generation", 0) or 0
            ),
            "consecutive_checkpoints": int(
                self.state.get("consecutive_checkpoints", 0) or 0
            ),
        }

    @staticmethod
    def _system_with_checkpoint(system: Any, checkpoint: Optional[str]) -> Any:
        if not checkpoint:
            return system
        block = {
            "type": "text",
            "text": (
                "<!-- DEEPSEEK CURRENT-VOLUME CONTINUITY CHECKPOINT -->\n"
                f"{checkpoint.strip()}"
            ),
        }
        if isinstance(system, list):
            return list(system) + [block]
        if isinstance(system, str) and system:
            return [{"type": "text", "text": system}, block]
        return [block]

    def history_messages(self) -> List[Dict[str, Any]]:
        """
        Build the append-only ``messages`` prefix for the current turn.

        Prior chapters are replayed as translation memory, NOT as
        translation targets. The stored user prompt of an earlier turn
        carries that chapter's entire JP <source_text>; re-sending it
        invites the model to treat it as the chapter to translate now
        (observed: DeepSeek V4 Pro re-translated CHAPTER_01 during the
        CHAPTER_02 call, filled its 16k output budget, and truncated
        CHAPTER_02's own translation). Each earlier turn is therefore
        collapsed to a compact stub that names the chapter and marks it
        already-translated, followed by the accepted EN output as the
        assistant message. The full stored ``user_prompt`` remains on the
        turn record for checkpoint compression.
        """
        messages: List[Dict[str, Any]] = []
        if self.conversation_kind == "prep":
            for turn in self.turns:
                if not isinstance(turn, dict):
                    continue
                user_prompt = str(turn.get("user_prompt") or "")
                assistant = str(turn.get("assistant_response") or "")
                if user_prompt:
                    messages.append({"role": "user", "content": user_prompt})
                if assistant:
                    messages.append({"role": "assistant", "content": assistant})
            return messages

        for turn in self.turns:
            if not isinstance(turn, dict):
                continue
            assistant = str(turn.get("assistant_response") or "")
            if not assistant:
                continue
            chapter_id = str(turn.get("chapter_id") or "").strip()
            if chapter_id:
                stub = (
                    f'<DEEPSEEK_PRIOR_CHAPTER_TRANSLATED chapter_id="{chapter_id}">'
                    f"{chapter_id} is already translated — its accepted "
                    "EN output is the assistant message below. Do NOT translate "
                    "or re-translate it. Translate only the source_text "
                    "envelope in the final message."
                    "</DEEPSEEK_PRIOR_CHAPTER_TRANSLATED>"
                )
            else:
                stub = (
                    "<DEEPSEEK_PRIOR_CHAPTER_TRANSLATED>A previous chapter is "
                    "already translated — its accepted EN output is the "
                    "assistant message below. Do NOT translate or re-translate "
                    "it. Translate only the source_text envelope in the final "
                    "message.</DEEPSEEK_PRIOR_CHAPTER_TRANSLATED>"
                )
            messages.append({"role": "user", "content": stub})
            tool_history = turn.get("tool_history")
            if isinstance(tool_history, list) and tool_history:
                messages.extend(tool_history)
            else:
                messages.append({"role": "assistant", "content": assistant})
        return messages

    def _estimate_payload_tokens(
        self,
        *,
        system: Any,
        history: Sequence[Dict[str, Any]],
        current_prompt: str,
    ) -> int:
        payload = {
            "system": system,
            "messages": list(history) + [{"role": "user", "content": current_prompt}],
        }
        return int(self._token_counter(_json_stable(payload)))

    def _canonical_overlay(self) -> str:
        """Build deterministic canon receipts appended to generated checkpoints."""
        blocks: List[str] = []
        for relative_path, label in (
            (Path(".context") / "name_registry.json", "EXACT NAME REGISTRY"),
            (Path(".context") / "cultural_glossary_en.json", "EXACT TERM GLOSSARY"),
            (Path(".context") / "arc_tracker.json", "ARC CLOSING STATE"),
            (Path("term_lock.json"), "EXACT TERM LOCKS"),
            (Path("VREC_ANCHORS_V1.0.json"), "EXACT CALLBACK ANCHORS"),
        ):
            path = self.work_dir / relative_path
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload:
                blocks.append(
                    f"## {label}\n```json\n"
                    f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n```"
                )

        ecr_dir = self.work_dir / "cache"
        ecr_payload: Dict[str, Any] = {}
        if ecr_dir.exists():
            for path in sorted(ecr_dir.glob("ecr_decisions_*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if value:
                    ecr_payload[path.stem] = value
        if ecr_payload:
            blocks.append(
                "## EXACT ECR DECISIONS\n```json\n"
                f"{json.dumps(ecr_payload, ensure_ascii=False, sort_keys=True)}\n```"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _strip_checkpoint_fence(content: str) -> str:
        text = (content or "").strip()
        if text.startswith("```") and text.endswith("```"):
            text = re.sub(r"^```(?:xml)?\s*", "", text, count=1, flags=re.I)
            text = re.sub(r"\s*```$", "", text, count=1)
        return text.strip()

    @classmethod
    def _validate_checkpoint(
        cls, content: str, required_chapter_ids: Sequence[str]
    ) -> List[str]:
        errors: List[str] = []
        text = cls._strip_checkpoint_fence(content)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            return [f"invalid XML: {exc}"]
        if root.tag != "deepseek_volume_checkpoint":
            errors.append("root must be <deepseek_volume_checkpoint>")
        found_sections = {
            str(node.get("name") or "").strip()
            for node in root.findall(".//section")
        }
        missing = sorted(_CHECKPOINT_REQUIRED_SECTIONS - found_sections)
        if missing:
            errors.append("missing sections: " + ", ".join(missing))
        for chapter_id in required_chapter_ids:
            if chapter_id and chapter_id not in text:
                errors.append(f"missing chapter coverage: {chapter_id}")
        return errors

    def _snip_tool_history(self) -> List[str]:
        """Cheap compaction rung: collapse stale tool round-trips.

        ``history_messages`` expands a turn's ``tool_history`` into the full
        assistant/tool exchange when the key is present, and falls back to a
        single assistant message carrying ``assistant_response`` when it is
        not.  For an *old* turn the tool chatter is dead weight — the
        translation it produced already sits in ``assistant_response`` — so
        dropping the key replaces the whole exchange with that one message.

        This is the rung below the checkpoint.  It does rewrite the prefix (the
        history is part of the cached span), but it costs no API call, so it
        runs first and the paid fold is skipped entirely when this alone clears
        the trigger.  Pairing cannot break: the entire assistant-tool_calls /
        tool-result group is replaced together, never split, so no tool_call is
        left orphaned.  Originals survive — ``commit_turn`` shallow-copies each
        turn into ``state["turn_archive"]``, and popping the key off the live
        turn does not touch the archived copy.

        Returns the chapter ids that were snipped (empty when there was nothing
        to snip, which is the common case and must let the ladder continue).
        """
        snipped: List[str] = []
        for turn in self.turns[:-self.recent_verbatim_chapters]:
            if not isinstance(turn, dict) or not turn.get("tool_history"):
                continue
            turn.pop("tool_history", None)
            snipped.append(str(turn.get("chapter_id") or "?"))
        if snipped:
            # Honest accounting: this changed the history, so the cached prefix
            # is gone this turn even though no model was called.
            self._record_cache_reset("tool_history_snip")
            self.state["tool_history_snips"] = int(
                self.state.get("tool_history_snips", 0) or 0
            ) + len(snipped)
            self.persist()
        return snipped

    def _checkpoint(
        self,
        callback: Callable[..., Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if len(self.turns) <= self.recent_verbatim_chapters:
            return None
        evicted = list(self.turns[:-self.recent_verbatim_chapters])
        retained = list(self.turns[-self.recent_verbatim_chapters:])
        prior_ids = [
            str(value)
            for value in self.state.get("checkpoint_chapter_ids", [])
            if value
        ]
        evicted_ids = [
            str(turn.get("chapter_id"))
            for turn in evicted
            if isinstance(turn, dict) and turn.get("chapter_id")
        ]
        coverage = list(dict.fromkeys(prior_ids + evicted_ids))
        previous_checkpoint = self.state.get("checkpoint")
        validation_errors: List[str] = []
        result: Optional[Dict[str, Any]] = None
        attempt_results: List[Dict[str, Any]] = []
        for attempt in range(2):
            result = callback(
                previous_checkpoint=previous_checkpoint,
                evicted_turns=evicted,
                required_chapter_ids=coverage,
                max_output_tokens=self.checkpoint_max_output_tokens,
                validation_errors=validation_errors,
            )
            attempt_results.append(dict(result or {}))
            content = self._strip_checkpoint_fence(
                str((result or {}).get("content") or "")
            )
            validation_errors = self._validate_checkpoint(content, coverage)
            if not validation_errors:
                overlay = self._canonical_overlay()
                if overlay:
                    safe_overlay = overlay.replace("]]>", "]]]]><![CDATA[>")
                    closing_tag = "</deepseek_volume_checkpoint>"
                    overlay_section = (
                        '<section name="deterministic_canon_overlay"><![CDATA['
                        f"{safe_overlay}"
                        "]]></section>"
                    )
                    content = content.replace(
                        closing_tag,
                        f"{overlay_section}{closing_tag}",
                        1,
                    )
                self.state["checkpoint"] = content
                self.state["checkpoint_chapter_ids"] = coverage
                self.state["checkpoint_generation"] = int(
                    self.state.get("checkpoint_generation", 0) or 0
                ) + 1
                self.state["turns"] = retained
                # Stuck-guard counter: this is the point where the prefix is
                # actually rewritten, so it is the only place worth counting.
                self.state["consecutive_checkpoints"] = int(
                    self.state.get("consecutive_checkpoints", 0) or 0
                ) + 1
                reset_reason = (
                    f"checkpoint_generation_{self.state['checkpoint_generation']}"
                )
                self._record_cache_reset(reset_reason)
                self.state["last_checkpoint_telemetry"] = {
                    "source_chapters": evicted_ids,
                    "covered_chapters": coverage,
                    "retained_verbatim_chapters": [
                        str(item.get("chapter_id"))
                        for item in retained
                        if isinstance(item, dict)
                    ],
                    "validation_result": "passed",
                    "attempts": attempt + 1,
                    "input_tokens": sum(
                        int(item.get("input_tokens") or 0)
                        for item in attempt_results
                    ),
                    "output_tokens": sum(
                        int(item.get("output_tokens") or 0)
                        for item in attempt_results
                    ),
                    "cache_hit_tokens": sum(
                        int(item.get("cache_read_tokens") or 0)
                        for item in attempt_results
                    ),
                    "cache_miss_tokens": sum(
                        int(item.get("cache_miss_tokens") or 0)
                        for item in attempt_results
                    ),
                }
                self.persist()
                logger.info(
                    "[DEEPSEEK-CONVERSATION] Checkpoint generation=%d compacted=%s; "
                    "retained verbatim=%s",
                    self.state["checkpoint_generation"],
                    ",".join(evicted_ids),
                    ",".join(
                        str(item.get("chapter_id"))
                        for item in retained
                        if isinstance(item, dict)
                    ),
                )
                aggregated = dict(result or {})
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_miss_tokens",
                ):
                    aggregated[key] = sum(
                        int(item.get(key) or 0) for item in attempt_results
                    )
                aggregated["checkpoint_attempts"] = attempt_results
                return aggregated
            logger.warning(
                "[DEEPSEEK-CONVERSATION] Checkpoint attempt %d failed: %s",
                attempt + 1,
                "; ".join(validation_errors),
            )
        if self.fail_closed:
            self.state["last_checkpoint_telemetry"] = {
                "source_chapters": evicted_ids,
                "covered_chapters": coverage,
                "validation_result": "failed",
                "attempts": 2,
                "errors": validation_errors,
            }
            raise RuntimeError(
                "DeepSeek conversation checkpoint validation failed: "
                + "; ".join(validation_errors)
            )
        return None

    def prepare_turn(
        self,
        *,
        prompt: str,
        system: Any,
        max_output_tokens: int,
        checkpoint_callback: Callable[..., Dict[str, Any]],
        tools: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "telemetry": {"enabled": False},
                "checkpoint_usage": None,
            }
        prefix_change_reasons = self._set_prefix_shape(system, tools=tools)
        checkpoint = self.state.get("checkpoint")
        effective_system = self._system_with_checkpoint(system, checkpoint)
        history = self.history_messages()
        projected = self._estimate_payload_tokens(
            system=effective_system, history=history, current_prompt=prompt
        )
        input_capacity = max(1, self.context_window - int(max_output_tokens or 0))
        trigger = max(1, int(input_capacity * self.checkpoint_trigger_ratio))
        soft_threshold = max(1, int(input_capacity * self.soft_notice_ratio))
        snip_threshold = max(1, int(input_capacity * self.tool_snip_ratio))
        force_threshold = max(1, int(input_capacity * self.checkpoint_force_ratio))
        checkpoint_usage = None
        checkpointed = False
        compactable = len(self.turns) > self.recent_verbatim_chapters

        # --- Ladder rung 1: soft notice.  Costs nothing and touches nothing;
        # its whole job is to make the climb visible before anything is spent.
        ladder_rung = "none"
        if projected > soft_threshold:
            ladder_rung = "soft_notice"
            logger.info(
                "[DEEPSEEK-CONVERSATION] Context at %.0f%% of capacity "
                "(projected=%s, capacity=%s) — watching, no action taken.",
                (projected / input_capacity) * 100,
                f"{projected:,}",
                f"{input_capacity:,}",
            )

        # --- Ladder rung 2: snip stale tool round-trips.  Breaks the prefix but
        # spends no API call, so it runs before the paid fold is even considered
        # and can clear the trigger on its own.
        snipped_chapters: List[str] = []
        snip_reclaimed = 0
        if projected > snip_threshold and compactable:
            snipped_chapters = self._snip_tool_history()
            if snipped_chapters:
                ladder_rung = "tool_snip"
                before = projected
                history = self.history_messages()
                projected = self._estimate_payload_tokens(
                    system=effective_system, history=history, current_prompt=prompt
                )
                snip_reclaimed = max(0, before - projected)
                logger.info(
                    "[DEEPSEEK-CONVERSATION] Snipped stale tool history from %s; "
                    "projected %s -> %s (trigger=%s)%s",
                    ",".join(snipped_chapters),
                    f"{before:,}",
                    f"{projected:,}",
                    f"{trigger:,}",
                    " — paid checkpoint avoided." if projected <= trigger else ".",
                )

        consecutive_checkpoints = int(
            self.state.get("consecutive_checkpoints", 0) or 0
        )
        guard_latched = bool(
            self.checkpoint_stuck_guard_turns > 0
            and consecutive_checkpoints >= self.checkpoint_stuck_guard_turns
        )
        guard_suppressed = False
        guard_overridden = False
        # --- Ladder rung 3: the paid fold.  Evaluated against the POST-snip
        # projection, which is what makes rung 2 a short-circuit rather than
        # merely an extra step before the same expense.
        trigger_fired = projected > trigger and compactable
        if trigger_fired:
            ladder_rung = "checkpoint"
            # A checkpoint rewrites the prefix.  If one fires every turn, the
            # cache resets every turn and the hit ratio never recovers — the
            # compaction meant to save tokens is what is burning them.  Once
            # latched, pause and let the prefix grow append-only so the cache
            # can warm again.
            #
            # --- Ladder rung 4: force.  The pause is only safe with headroom
            # left.  Releasing at the hard ceiling would be one turn too late —
            # that is the turn that raises below.  The force rung sits under the
            # ceiling so the guard breaks while a checkpoint can still save the
            # turn rather than after it is already unsalvageable.
            if guard_latched and projected <= force_threshold:
                guard_suppressed = True
                logger.warning(
                    "[DEEPSEEK-CONVERSATION] Checkpoint stuck guard engaged after "
                    "%s consecutive checkpoints; skipping this one and growing the "
                    "prefix append-only so the cache can warm "
                    "(projected=%s, trigger=%s, force=%s, capacity=%s).",
                    consecutive_checkpoints,
                    f"{projected:,}",
                    f"{trigger:,}",
                    f"{force_threshold:,}",
                    f"{input_capacity:,}",
                )
            else:
                guard_overridden = guard_latched
                if guard_overridden:
                    ladder_rung = "checkpoint_force"
                    logger.warning(
                        "[DEEPSEEK-CONVERSATION] Checkpoint stuck guard overridden at "
                        "the force rung: projected=%s exceeds force=%s "
                        "(capacity=%s); checkpointing anyway.",
                        f"{projected:,}",
                        f"{force_threshold:,}",
                        f"{input_capacity:,}",
                    )
                checkpoint_usage = self._checkpoint(checkpoint_callback)
                checkpointed = checkpoint_usage is not None
                checkpoint = self.state.get("checkpoint")
                effective_system = self._system_with_checkpoint(system, checkpoint)
                history = self.history_messages()
                projected = self._estimate_payload_tokens(
                    system=effective_system, history=history, current_prompt=prompt
                )
        elif consecutive_checkpoints:
            # Pressure genuinely dropped — a turn cleared the trigger on its own.
            # Only this unlatches the guard; a suppressed turn must not, or the
            # guard would release itself on the very next turn and do nothing.
            self.state["consecutive_checkpoints"] = 0
            consecutive_checkpoints = 0
            guard_latched = False
        consecutive_checkpoints = int(
            self.state.get("consecutive_checkpoints", 0) or 0
        )
        if projected > input_capacity:
            raise RuntimeError(
                "DeepSeek conversation exceeds safe input capacity after checkpoint: "
                f"projected={projected:,}, capacity={input_capacity:,}, "
                f"max_output={int(max_output_tokens or 0):,}"
            )
        telemetry = {
            "enabled": True,
            "checkpoint_generation": int(
                self.state.get("checkpoint_generation", 0) or 0
            ),
            "committed_chapters": self.represented_chapter_ids,
            "checkpointed": checkpointed,
            "estimated_input_tokens": projected,
            "input_capacity_tokens": input_capacity,
            "checkpoint_trigger_tokens": trigger,
            "checkpoint_headroom_tokens": max(0, trigger - projected),
            "cache_reset_reason": self.state.get("cache_reset_reason"),
            "prefix_changed": bool(prefix_change_reasons),
            "prefix_change_reasons": list(prefix_change_reasons),
            "prefix_hash": str(
                (self.state.get("prefix_shape") or {}).get("prefix_hash") or ""
            ),
            "tool_schema_tokens": list(
                (self.state.get("prefix_shape") or {}).get("tool_schema_tokens") or []
            ),
            "consecutive_checkpoints": consecutive_checkpoints,
            "checkpoint_guard_latched": guard_latched,
            "checkpoint_guard_suppressed": guard_suppressed,
            "checkpoint_guard_overridden": guard_overridden,
            # Compaction ladder: which rung this turn actually reached, and what
            # the cheap rung bought.  `snip_reclaimed_tokens` is measured, not
            # estimated — projection re-run before and after the snip.
            "ladder_rung": ladder_rung,
            "soft_notice_tokens": soft_threshold,
            "tool_snip_tokens": snip_threshold,
            "checkpoint_force_tokens": force_threshold,
            "tool_history_snipped": list(snipped_chapters),
            "snip_reclaimed_tokens": snip_reclaimed,
            "checkpoint": dict(
                self.state.get("last_checkpoint_telemetry") or {}
            ),
            "user_id": self.metadata_user_id,
        }
        logger.info(
            "[DEEPSEEK-CONVERSATION] generation=%d committed=%s "
            "estimated_input=%s/%s trigger=%s headroom=%s",
            telemetry["checkpoint_generation"],
            ",".join(self.represented_chapter_ids) or "none",
            f"{projected:,}",
            f"{input_capacity:,}",
            f"{trigger:,}",
            f"{telemetry['checkpoint_headroom_tokens']:,}",
        )
        return {
            "system": effective_system,
            "messages": history + [{"role": "user", "content": prompt}],
            "telemetry": telemetry,
            "checkpoint_usage": checkpoint_usage,
        }

    def truncate_from(self, chapter_id: str) -> bool:
        ids = self.represented_chapter_ids
        if chapter_id not in ids:
            return False
        index = ids.index(chapter_id)
        removed = ids[index:]
        retained_ids = set(ids[:index])
        archive = [
            turn
            for turn in self.state.get("turn_archive", [])
            if isinstance(turn, dict) and turn.get("chapter_id") in retained_ids
        ]
        self.state["turn_archive"] = archive
        self.state["turns"] = list(archive)
        self.state["ordered_chapter_ids"] = ids[:index]
        self.state["checkpoint"] = None
        self.state["checkpoint_chapter_ids"] = []
        self._record_cache_reset("chapter_rerun:" + ",".join(removed))
        self.persist()
        logger.warning(
            "[DEEPSEEK-CONVERSATION] Truncated rerun chapter and later turns: %s",
            ", ".join(removed),
        )
        return True

    def commit_turn(
        self,
        *,
        chapter_id: str,
        user_prompt: str,
        assistant_response: str,
        canonical_output: str,
        output_path: Path,
        cache_telemetry: Optional[Dict[str, Any]] = None,
        tool_history: Optional[List[Dict[str, Any]]] = None,
        subturns: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        self.truncate_from(chapter_id)
        try:
            stored_path = str(Path(output_path).resolve().relative_to(self.work_dir.resolve()))
        except Exception:
            stored_path = str(output_path)
        turn = {
            "chapter_id": str(chapter_id),
            "user_prompt": str(user_prompt or ""),
            "user_prompt_sha256": _sha256_text(user_prompt),
            "assistant_response": str(assistant_response or ""),
            "assistant_response_sha256": _sha256_text(assistant_response),
            "assistant_source": "selected_api_response",
            "canonical_output_sha256": _sha256_text(canonical_output),
            "output_path": stored_path,
            "estimated_tokens": int(
                self._token_counter(f"{user_prompt}\n{assistant_response}")
            ),
        }
        if tool_history:
            turn["tool_history"] = tool_history
        if subturns:
            turn["subturns"] = subturns
        self.turns.append(turn)
        self.state.setdefault("turn_archive", []).append(dict(turn))
        self.state.setdefault("ordered_chapter_ids", []).append(str(chapter_id))
        self.state["cache_telemetry"] = dict(cache_telemetry or {})
        self.state["cache_reset_reason"] = None
        self.persist()
        telemetry = {
            "enabled": True,
            "checkpoint_generation": int(
                self.state.get("checkpoint_generation", 0) or 0
            ),
            "committed_chapters": self.represented_chapter_ids,
            "committed_chapter": str(chapter_id),
            "state_path": str(self.state_path),
            **dict(cache_telemetry or {}),
        }
        logger.info(
            "[DEEPSEEK-CONVERSATION] Committed %s; history=%s",
            chapter_id,
            ",".join(self.represented_chapter_ids),
        )
        return telemetry

    def seed_turn_from_files(
        self,
        *,
        chapter_id: str,
        jp_text: str,
        en_text: str,
        output_path: Path,
    ) -> bool:
        """Seed a missing earlier turn for partial/resumed runs."""
        if not self.enabled or chapter_id in self.represented_chapter_ids:
            return False
        prompt = (
            f"<chapter_translation_task chapter_id=\"{chapter_id}\">\n"
            "<SOURCE_TEXT>\n"
            f"{jp_text.strip() if self.include_exact_jp_task else '[SOURCE STORED CANONICALLY]'}\n"
            "</SOURCE_TEXT>\n"
            "</chapter_translation_task>"
        )
        self.commit_turn(
            chapter_id=chapter_id,
            user_prompt=prompt,
            assistant_response=en_text,
            canonical_output=en_text,
            output_path=output_path,
            cache_telemetry={"seeded_from_files": True},
        )
        self._record_cache_reset("seeded_from_canonical_files")
        self.persist()
        return True
