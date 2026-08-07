"""Qwen Phase 2 translator facade with the existing filesystem contract."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.config import WORK_DIR
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.token_telemetry import log_call
from src.Qwen.client import QwenClient
from src.Qwen.config import get_qwen_config, get_qwen_continuation_config, get_qwen_conversation_config, get_qwen_optimization_config, get_qwen_prompt_path
from src.Qwen.context import derive_chapter_eps_band, parse_character_roster_handles, parse_eps_signals, parse_voice_fingerprints, parse_volume_type, resolve_voice_aliases
from src.Qwen.errors import QwenModerationError
from src.Qwen.optimization import build_chapter_guidance, supports_partial_prefix
from src.Qwen.prompt_loader import build_chapter_message, build_continuation_messages, build_system_instruction
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, split_thinking_from_output

logger = logging.getLogger(__name__)
_CJK_LEAK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")


class QwenTranslator:
    def __init__(self, work_dir: Path, volume_id: str, config: Optional[Dict[str, Any]] = None, thinking_log_enabled: Optional[bool] = None, dry_run: bool = False):
        self.work_dir = Path(work_dir)
        self.volume_id = volume_id
        self._config = config or get_qwen_config()
        self.dry_run = dry_run
        self.client = QwenClient(dry_run=dry_run)
        conversation_cfg = get_qwen_conversation_config()
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(work_dir=self.work_dir, volume_id=volume_id, conversation_config=conversation_cfg)
        from src.Deepseek.translator.context_manager import load_context_xml  # lazy — breaks circular import with translator.__init__
        context_xml = load_context_xml(self.work_dir)
        self._voice_profiles = parse_voice_fingerprints(context_xml)
        # eps_signals uses canonical names (e.g. "Kirigaya Kazuto") while
        # voice_fingerprints keys by EN handle names (e.g. "Kirito").
        # Cross-resolve via character_roster so both forms hit the same profile.
        roster_handles = parse_character_roster_handles(context_xml)
        self._voice_profiles = resolve_voice_aliases(self._voice_profiles, roster_handles)
        self._eps_signals = parse_eps_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        self.system_instruction = build_system_instruction(
            prompt_path=get_qwen_prompt_path(),
            context_xml=context_xml,
        )
        self._previous_guidance_text: Optional[str] = None
        thinking_log_cfg = get_thinking_log_config()
        # `thinking_log_enabled=False` (e.g. from --no-thinking-log) always
        # wins over config.yaml; `None` means "use whatever config.yaml says."
        self.thinking_log_enabled = (
            thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        )
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")
        self.thinking_density_enabled = thinking_log_cfg.get("density_map", {}).get("enabled", True)
        self.optimizations = get_qwen_optimization_config()
        continuation = get_qwen_continuation_config()
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))
        self.use_partial_mode = bool(continuation.get("use_partial_mode", True))

    def translate_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = chapter_meta or {}
        chapter_id = str(meta.get("chapter_id", chapter_path.stem))
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = meta.get("eps_band") or derive_chapter_eps_band(signals)
        active_characters = meta.get("active_characters")
        if active_characters is None:
            active_characters = [
                {"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})}
                for signal in signals
                if signal.get("name")
            ]
        guidance = build_chapter_guidance(eps_band, active_characters, self.optimizations, self._volume_type)
        prompt = build_chapter_message(
            chapter_id, jp_source, guidance,
            previous_guidance_text=self._previous_guidance_text,
        )
        self._previous_guidance_text = guidance.strip() if guidance else None
        # Skipped in dry_run so the ledger is not even read for a preview.
        # QwenClient.generate() drops caller-supplied history in dry_run
        # regardless — that guard is the authoritative one; this only avoids
        # doing the work to build something the client will discard.
        messages = (
            self.client.conversation_manager.messages(self.system_instruction, prompt, int(get_qwen_conversation_config().get("recent_verbatim_chapters", 2) or 2))
            if self.client.conversation_manager and not self.dry_run
            else None
        )
        try:
            response = self.client.generate(prompt=prompt, system_instruction=self.system_instruction, messages=messages, dry_run=self.dry_run)
        except QwenModerationError as exc:
            logger.error("[QWEN-SAFETY] %s blocked by content moderation: %s", chapter_id, exc)
            return self._safety_fallback_translate(chapter_path, chapter_id, exc)
        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt
            path = write_dry_run_prompt(work_dir=self.work_dir, volume_id=self.volume_id, chapter_id=chapter_id, payload=response.provider_metadata["payload"], provider="qwen")
            return f"[DRY RUN — no translation performed. Payload written to {path}]"
        parts = [response.content]
        thinking_parts = [response.thinking_content] if response.thinking_content else []
        continuation_count = 0
        self._log_usage(chapter_id, response, continuation_count)
        while response.termination == LLMTermination.MAX_OUTPUT and self.continuation_enabled and continuation_count < self.max_continuations:
            continuation_count += 1
            thinking_enabled = bool((self._config.get("thinking") or {}).get("enabled", True))
            use_partial = self.use_partial_mode and supports_partial_prefix(False, {"partial_prefix": True})
            if use_partial and thinking_enabled:
                # Official Partial Mode forbids thinking; disable thinking for the continuation turn only.
                partial_prefix = "".join(parts)
                continuation_messages = (messages or [{"role": "user", "content": prompt}]) + [
                    {"role": "assistant", "content": partial_prefix, "partial": True}
                ]
                response = self.client.generate(
                    prompt=prompt,
                    system_instruction=self.system_instruction,
                    messages=continuation_messages,
                    partial=True,
                    thinking_enabled=False,
                )
                parts.append(response.content)
            else:
                continuation_messages = build_continuation_messages(
                    messages or [{"role": "user", "content": prompt}],
                    response.provider_metadata.get("raw_content_blocks") or [{"type": "text", "text": response.content}],
                    chapter_id,
                )
                response = self.client.generate(
                    prompt=continuation_messages[-1]["content"],
                    system_instruction=self.system_instruction,
                    messages=continuation_messages,
                )
                parts.append(response.content)
            if response.thinking_content:
                thinking_parts.append(response.thinking_content)
            self._log_usage(f"{chapter_id}#continue-{continuation_count}", response, continuation_count)
        raw_text = "\n".join(part for part in parts if part)
        # Strip any <thinking>...</thinking> XML that leaked into the content
        # (known DeepSeek API edge case — Qwen may also emit it). Do this
        # regardless of thinking_log_enabled: a leaked block has no business
        # in the shipped chapter.
        text, leaked_blocks = split_thinking_from_output(raw_text)
        text = _CJK_LEAK_RE.sub("", text)
        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(thinking_parts) or None,
            leaked_blocks=leaked_blocks,
        )
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        if self.client.conversation_manager:
            self.client.conversation_manager.commit(
                chapter_id,
                messages or [{"role": "user", "content": prompt}],
                text,
                output_path,
                response.provider_metadata.get("raw_content_blocks"),
            )
        return text

    def _safety_fallback_translate(self, chapter_path: Path, chapter_id: str, exc: QwenModerationError) -> str:
        """Qwen safety-refusal fallback: do NOT retry. Switch to DeepSeek with
        decision inheritance injected into context.xml, and return the EN text.

        The inheritance agent (DeepSeek call on the prep config) summarizes
        the translation decisions Qwen already made in prior EN chapters, and
        that summary is injected into <translation_inheritance> before the
        DeepSeek payload is built — see src/Qwen/safety_fallback.py.

        The caller (translate_and_persist_chapter) writes the returned text to
        EN/ and marks the chapter completed, exactly as it would for a normal
        Qwen success — the fallback only changes where the text came from.
        """
        from src.Qwen.config import get_safety_fallback_config  # local — cheap, keeps imports tidy
        from src.Qwen.safety_fallback import fallback_translate_chapter  # local — avoids import cycle

        cfg = get_safety_fallback_config()
        if not cfg.get("enabled", True):
            logger.warning("[QWEN-SAFETY] %s — safety fallback disabled; re-raising moderation error", chapter_id)
            raise exc

        try:
            return fallback_translate_chapter(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_path=chapter_path,
                chapter_id=chapter_id,
                refusal=exc,
                dry_run=self.dry_run,
            )
        except QwenModerationError:
            raise
        except Exception as fallback_exc:  # noqa: BLE001 - surface any fallback failure loudly
            logger.error(
                "[QWEN-SAFETY] %s — DeepSeek fallback failed (%s); re-raising original moderation error",
                chapter_id, fallback_exc,
            )
            raise exc from fallback_exc

    def _log_usage(self, call_label: str, response, continuation_count: int) -> None:
        try:
            log_call(
                phase="translator",
                volume_id=self.volume_id,
                call_label=call_label,
                model=response.model,
                cache_hit_tokens=response.cached_tokens,
                fresh_tokens=max(0, response.input_tokens - response.cached_tokens),
                output_tokens=response.output_tokens,
                cost_usd=response.total_cost_usd,
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must never fail translation
            logger.warning("[QWEN] token log failed for %s: %s", call_label, exc)

    def _maybe_write_thinking_log(
        self,
        *,
        chapter_id: str,
        api_thinking: Optional[str],
        leaked_blocks: List[str],
    ) -> None:
        """Archive this chapter's reasoning to THINKING/<chapter_id>_THINKING.md,
        if thinking_log.enabled. Reasoning tokens are already paid for whether
        or not this runs — the flag only controls whether they're kept."""
        if not self.thinking_log_enabled:
            return
        merged = merge_thinking_log(api_thinking, leaked_blocks, chapter_id=chapter_id)
        if not merged:
            return

        thinking_dir = self.work_dir / self.thinking_log_dir_name
        thinking_dir.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Thinking Process — {chapter_id}\n\n"
            f"- **Chapter:** {chapter_id}\n"
            f"- **Model:** {self.client.model}\n"
            f"- **Provider:** Qwen\n"
            f"- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            f"## Qwen Translation Reasoning\n\n"
        )
        (thinking_dir / f"{chapter_id}_THINKING.md").write_text(
            header + merged + "\n", encoding="utf-8"
        )
        self._maybe_rebuild_density_map()

    def _maybe_rebuild_density_map(self) -> None:
        """Rebuild THINKING/density_map.html from every THINKING/*.md file on
        disk so far. Cheap (local parsing + string-built SVG, no LLM call) —
        safe to redo after every chapter. Never raises."""
        if not self.thinking_density_enabled:
            return
        from src.Deepseek.translator.thinking_density import build_density_report

        try:
            build_density_report(self.work_dir, self.volume_id)
        except Exception as exc:
            logger.warning(
                "[QWEN-THINKING] %s — density map rebuild failed: %s",
                self.volume_id,
                exc,
            )

    def translate_and_persist_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> Path:
        chapter_id = str((chapter_meta or {}).get("chapter_id", chapter_path.stem))
        text = self.translate_chapter(chapter_path, {**(chapter_meta or {}), "chapter_id": chapter_id})
        if self.dry_run:
            return self.work_dir / "DRY_RUN"
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        _update_manifest_after_translation(self.work_dir, {chapter_id})
        return output_path

    def translate_all(self, chapter_files: List[Path]) -> Dict[str, Path]:
        return {path.stem: self.translate_and_persist_chapter(path, {"chapter_id": path.stem}) for path in sorted(chapter_files)}


def _update_manifest_after_translation(work_dir: Path, translated_stems: set[str]) -> None:
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    chapters = manifest.get("chapters", [])
    for entry in chapters if isinstance(chapters, list) else []:
        if isinstance(entry, dict) and Path(str(entry.get("source_file", ""))).stem in translated_stems:
            entry["translation_status"] = "completed"
    completed = sum(1 for entry in chapters if isinstance(entry, dict) and entry.get("translation_status") == "completed")
    manifest.setdefault("pipeline_state", {})["translator"] = {"status": "completed" if chapters and completed == len(chapters) else "in_progress", "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "chapters_completed": completed, "chapters_total": len(chapters)}
    atomic_write_json(path, manifest)


def _filter_completed_chapters(work_dir: Path, chapter_files: List[Path]) -> List[Path]:
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return chapter_files
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return chapter_files
    completed = {Path(str(entry.get("source_file", ""))).stem for entry in manifest.get("chapters", []) if isinstance(entry, dict) and entry.get("translation_status") == "completed"}
    return [path for path in chapter_files if path.stem not in completed]


def translate_volume(volume_id: str, chapters: Optional[List[str]] = None, thinking_log_enabled: Optional[bool] = None, dry_run: bool = False) -> Dict[str, Path]:
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")
    chapter_files = sorted(jp_dir.glob("CHAPTER_*.md"))
    if chapters:
        wanted = set(chapters)
        chapter_files = [path for path in chapter_files if path.stem in wanted or path.stem.split("_")[-1] in wanted]
    # Dry-run bypasses the manifest completion filter: it is a developer
    # inspection mode and must assemble payloads for every requested
    # chapter regardless of manifest state.
    if not dry_run:
        chapter_files = _filter_completed_chapters(work_dir, chapter_files)
    return QwenTranslator(work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run).translate_all(chapter_files)
