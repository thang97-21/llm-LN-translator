"""GLM Phase 2 translator facade with MTLS's filesystem contract."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.chapter_signals import build_chapter_signal_guidance, parse_chapter_signals
from src.Deepseek.common.config import WORK_DIR, get_safety_fallback_config
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.safety_fallback import fallback_translate_chapter
from src.Deepseek.common.token_telemetry import log_call
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, split_thinking_from_output
from src.GLM.client import GLMClient
from src.GLM.config import get_glm_config, get_glm_continuation_config, get_glm_conversation_config, get_glm_optimization_config, get_glm_prompt_path
from src.GLM.context import derive_chapter_eps_band, parse_character_roster_handles, parse_eps_signals, parse_voice_fingerprints, parse_volume_type, resolve_voice_aliases
from src.GLM.errors import GLMModerationError
from src.GLM.optimization import build_chapter_guidance, directive_for_band
from src.GLM.prompt_loader import build_chapter_message, build_continuation_message, build_system_instruction

logger = logging.getLogger(__name__)


class GLMTranslator:
    def __init__(self, work_dir: Path, volume_id: str, config: Optional[Dict[str, Any]] = None, thinking_log_enabled: Optional[bool] = None, dry_run: bool = False):
        self.work_dir = Path(work_dir)
        self.volume_id = volume_id
        self._config = config or get_glm_config()
        self.dry_run = dry_run
        self.client = GLMClient(dry_run=dry_run)
        conversation_cfg = get_glm_conversation_config()
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(work_dir=self.work_dir, volume_id=volume_id, conversation_config=conversation_cfg)
        from src.Deepseek.translator.context_manager import load_context_xml

        context_xml = load_context_xml(self.work_dir)
        profiles = parse_voice_fingerprints(context_xml)
        profiles = resolve_voice_aliases(profiles, parse_character_roster_handles(context_xml))
        self._voice_profiles = profiles
        self._eps_signals = parse_eps_signals(context_xml)
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        self.system_instruction = build_system_instruction(prompt_path=get_glm_prompt_path(), context_xml=context_xml)
        self._previous_guidance_text: Optional[str] = None
        self._previous_strategy_directive: Optional[str] = None
        thinking_log_cfg = get_thinking_log_config()
        self.thinking_log_enabled = thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")
        self.thinking_density_enabled = thinking_log_cfg.get("density_map", {}).get("enabled", True)
        self.optimizations = get_glm_optimization_config()
        continuation = get_glm_continuation_config()
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))

    def translate_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = chapter_meta or {}
        chapter_id = str(meta.get("chapter_id", chapter_path.stem))
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = str(meta.get("eps_band") or derive_chapter_eps_band(signals)).upper()
        active_characters = meta.get("active_characters")
        if active_characters is None:
            active_characters = [{"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})} for signal in signals if signal.get("name")]
        guidance = build_chapter_guidance(eps_band, active_characters, self.optimizations, self._volume_type)
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        strategy_directive, reasoning_effort = directive_for_band(eps_band)
        if not self.optimizations.get("eps_guidance", True):
            strategy_directive = ""
        prompt = build_chapter_message(chapter_id, jp_source, guidance, strategy_directive=strategy_directive, previous_guidance_text=self._previous_guidance_text, previous_strategy_directive=self._previous_strategy_directive)
        self._previous_guidance_text = guidance.strip() if guidance else None
        self._previous_strategy_directive = strategy_directive.strip() if strategy_directive else None
        messages = (
            self.client.conversation_manager.messages(self.system_instruction, prompt, int(get_glm_conversation_config().get("recent_verbatim_chapters", 2) or 2))
            if self.client.conversation_manager and not self.dry_run else None
        )
        try:
            response = self.client.generate(prompt=prompt, system_instruction=self.system_instruction, messages=messages, dry_run=self.dry_run, reasoning_effort=reasoning_effort)
        except GLMModerationError as exc:
            logger.error("[GLM-SAFETY] %s blocked by content moderation: %s", chapter_id, exc)
            return self._safety_fallback_translate(chapter_path, chapter_id, exc)
        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt

            path = write_dry_run_prompt(work_dir=self.work_dir, volume_id=self.volume_id, chapter_id=chapter_id, payload=response.provider_metadata["payload"], provider="glm")
            return f"[DRY RUN — no translation performed. Payload written to {path}]"

        parts = [response.content]
        thinking_parts = [response.thinking_content] if response.thinking_content else []
        self._log_usage(chapter_id, response)
        continuation_count = 0
        while response.termination == LLMTermination.MAX_OUTPUT and self.continuation_enabled and continuation_count < self.max_continuations:
            continuation_count += 1
            continuation_messages = list(messages or [{"role": "system", "content": self.system_instruction}, {"role": "user", "content": prompt}])
            continuation_messages.extend([
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": build_continuation_message(chapter_id)},
            ])
            response = self.client.generate(
                prompt=continuation_messages[-1]["content"],
                system_instruction=self.system_instruction,
                messages=continuation_messages,
                reasoning_effort=reasoning_effort,
            )
            parts.append(response.content)
            if response.thinking_content:
                thinking_parts.append(response.thinking_content)
            self._log_usage(f"{chapter_id}#continue-{continuation_count}", response)
            messages = continuation_messages

        text, leaked_blocks = split_thinking_from_output("\n".join(part for part in parts if part))
        self._maybe_write_thinking_log(chapter_id=chapter_id, api_thinking="\n\n".join(thinking_parts) or None, leaked_blocks=leaked_blocks)
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        if self.client.conversation_manager:
            self.client.conversation_manager.commit(chapter_id, messages or [{"role": "user", "content": prompt}], text, output_path)
        return text

    def _safety_fallback_translate(self, chapter_path: Path, chapter_id: str, exc: GLMModerationError) -> str:
        cfg = get_safety_fallback_config()
        if not cfg.get("enabled", True):
            raise exc
        try:
            return fallback_translate_chapter(work_dir=self.work_dir, volume_id=self.volume_id, chapter_path=chapter_path, chapter_id=chapter_id, refusal=exc, source_provider="GLM", refusal_code_default="sensitive", dry_run=self.dry_run)
        except Exception as fallback_exc:
            logger.error("[GLM-SAFETY] %s — DeepSeek fallback failed (%s); re-raising original refusal", chapter_id, fallback_exc)
            raise exc from fallback_exc

    def _log_usage(self, call_label: str, response: Any) -> None:
        try:
            log_call(phase="translator", volume_id=self.volume_id, call_label=call_label, model=response.model, cache_hit_tokens=response.cached_tokens, fresh_tokens=max(0, response.input_tokens - response.cached_tokens), output_tokens=response.output_tokens, cost_usd=response.total_cost_usd, provider="glm")
        except Exception as exc:
            logger.warning("[GLM] token log failed for %s: %s", call_label, exc)

    def _maybe_write_thinking_log(self, *, chapter_id: str, api_thinking: Optional[str], leaked_blocks: List[str]) -> None:
        if not self.thinking_log_enabled:
            return
        merged = merge_thinking_log(api_thinking, leaked_blocks, chapter_id=chapter_id)
        if not merged:
            return
        thinking_dir = self.work_dir / self.thinking_log_dir_name
        thinking_dir.mkdir(parents=True, exist_ok=True)
        header = f"# Thinking Process — {chapter_id}\n\n- **Chapter:** {chapter_id}\n- **Model:** {self.client.model}\n- **Provider:** GLM\n- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n## GLM reasoning (reasoning_content)\n\n"
        (thinking_dir / f"{chapter_id}_THINKING.md").write_text(header + merged + "\n", encoding="utf-8")
        if self.thinking_density_enabled:
            try:
                from src.Deepseek.translator.thinking_density import build_density_report

                build_density_report(self.work_dir, self.volume_id)
            except Exception as exc:
                logger.warning("[GLM-THINKING] density map rebuild failed: %s", exc)

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
    return [chapter for chapter in chapter_files if chapter.stem not in completed]


def translate_volume(volume_id: str, chapters: Optional[List[str]] = None, thinking_log_enabled: Optional[bool] = None, dry_run: bool = False) -> Dict[str, Path]:
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")
    chapter_files = sorted(jp_dir.glob("CHAPTER_*.md"))
    if chapters:
        wanted = set(chapters)
        chapter_files = [path for path in chapter_files if path.stem in wanted or path.stem.split("_")[-1] in wanted]
    if not dry_run:
        chapter_files = _filter_completed_chapters(work_dir, chapter_files)
    return GLMTranslator(work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run).translate_all(chapter_files)
