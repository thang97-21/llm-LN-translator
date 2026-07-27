"""
DeepSeekTranslator — the bare send-and-receive Phase 2 client.

This is deliberately ~150 lines, not the ~2500-line orchestrator it replaces.
Deliberately REMOVED (see PLANNING.md Phase 4): provider routing, multimodal
setup, bible loading, voice fingerprint RAG (DOVB static templates only),
arc tracking, VREC compliance, the LCI Draft-Revise loop, tool-use mode,
all validators, preflight audits, cost auditing beyond what DeepSeekClient
already returns, Phase 2.5 hooks, glossary/term-lock enforcement (trust the
prompt), schema extraction, volume context aggregation, and title
translation.

Sequential chapter loop only — no parallel coordination. CCT
(deepseek_optimization.translate_chapters_concurrent) is kept in the
codebase but not wired in here; call it directly if you want it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common.atomic_io import atomic_write_json
from src.common.config import WORK_DIR
from src.translator.config import (
    get_conversation_config,
    get_master_prompt_path,
    get_optimizations_config,
    get_post_processing_config,
)
from src.translator.context_manager import load_context_xml
from src.translator.deepseek_client import DeepSeekClient
from src.translator.deepseek_optimization import (
    assemble_deepseek_chapter_blocks,
    build_deepseek_voice_collective_block,
)
from src.translator.prompt_loader import build_system_instruction, build_user_message
from src.translator.scene_break_formatter import SceneBreakFormatter

logger = logging.getLogger(__name__)

# Stray CJK/fullwidth artifacts that occasionally leak through despite the
# master prompt's explicit "no CJK in output" instruction. This is a safety
# net, not a detector — the full confidence-scored CJKArtifactCleaner lives
# in the main pipeline's post_processor/, which is out of scope here.
_CJK_LEAK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿＀-￯]"
)


class DeepSeekTranslator:
    """Bare Phase 2 client: JP markdown in, EN markdown out, via DeepSeek V4 Pro."""

    def __init__(self, work_dir: Path, volume_id: str, config: Optional[Dict[str, Any]] = None):
        self.work_dir = Path(work_dir)
        self.volume_id = volume_id
        self._config = config or {}

        self.client = DeepSeekClient()

        conversation_cfg = get_conversation_config()
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                conversation_config=conversation_cfg,
            )

        self.optimizations = get_optimizations_config()
        self.post_processing = get_post_processing_config()

        context_xml = load_context_xml(self.work_dir)
        if context_xml is None:
            logger.warning(
                "[CONTEXT-XML] No context.xml found at %s — translating with "
                "inline master-prompt guidance only. Character voice, term "
                "locks, and continuity anchors will NOT be enforced.",
                self.work_dir / "context.xml",
            )

        voice_block = self._build_volume_voice_anchor()
        self.system_instruction = build_system_instruction(
            context_xml=context_xml,
            voice_block=voice_block,
            prompt_path=get_master_prompt_path(),
        )

    def _build_volume_voice_anchor(self) -> Optional[str]:
        """
        Whole-cast DOVB anchor for CHARACTER_VOICE_SLOT (volume-stable, cached).

        The lightweight client has no voice RAG — if the caller wants voice
        anchors, pass character profiles via config.yaml's
        `translation.translator.static_voice_profiles`, keyed by name.
        Absent that, the slot is dropped and the master prompt's inline
        voice guidance is all that applies.
        """
        if not self.optimizations.get("dovb", {}).get("enabled", True):
            return None
        static_profiles = self._config.get("static_voice_profiles") or {}
        if not static_profiles:
            return None
        active_characters = [
            {"name": name, "fingerprint": profile}
            for name, profile in static_profiles.items()
        ]
        return build_deepseek_voice_collective_block(active_characters, "NEUTRAL")

    def translate_chapter(
        self,
        chapter_path: Path,
        chapter_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Translate one chapter. Returns the EN markdown text — the caller
        writes it wherever it wants (see translate_all for the standard
        WORK/<vol>/EN/ layout).
        """
        chapter_meta = chapter_meta or {}
        chapter_id = chapter_meta.get("chapter_id", chapter_path.stem)
        eps_band = chapter_meta.get("eps_band", "NEUTRAL")
        active_characters = chapter_meta.get("active_characters")

        jp_source = Path(chapter_path).read_text(encoding="utf-8")

        guidance_blocks = assemble_deepseek_chapter_blocks(
            eps_band=eps_band,
            active_characters=active_characters,
            reasoning_directive_enabled=self.optimizations.get("drdi", {}).get("enabled", True),
            voice_block_enabled=self.optimizations.get("dovb", {}).get("enabled", True),
        )
        user_message = build_user_message(
            jp_source=jp_source,
            chapter_guidance_blocks=guidance_blocks,
        )

        response = self.client.generate(
            prompt=user_message,
            system_instruction=self.system_instruction,
        )

        en_text = response.content
        en_text = self._post_process(en_text)

        self.client.commit_conversation_turn(
            response=response,
            chapter_id=chapter_id,
            output_path=self._default_output_path(chapter_id),
            canonical_output=en_text,
        )

        return en_text

    def _post_process(self, text: str) -> str:
        if self.post_processing.get("scene_break_formatting", True):
            text, _count = SceneBreakFormatter.format_scene_breaks(text)
        if self.post_processing.get("cjk_cleanup", True):
            text = _CJK_LEAK_RE.sub("", text)
        return text

    def _default_output_path(self, chapter_id: str) -> Path:
        en_dir = self.work_dir / "EN"
        en_dir.mkdir(parents=True, exist_ok=True)
        return en_dir / f"{chapter_id}_EN.md"

    def translate_all(self, chapter_files: List[Path]) -> Dict[str, Path]:
        """
        Sequential loop over chapters. Writes each result to
        WORK/<vol>/EN/<chapter_id>_EN.md and returns {chapter_id: output_path}.
        """
        results: Dict[str, Path] = {}
        for chapter_path in sorted(chapter_files):
            chapter_id = chapter_path.stem
            logger.info("[TRANSLATE] %s — starting", chapter_id)
            en_text = self.translate_chapter(chapter_path, {"chapter_id": chapter_id})
            output_path = self._default_output_path(chapter_id)
            output_path.write_text(en_text, encoding="utf-8")
            results[chapter_id] = output_path
            logger.info("[TRANSLATE] %s — wrote %s", chapter_id, output_path)
        _update_manifest_after_translation(self.work_dir, set(results.keys()))
        return results


def _update_manifest_after_translation(work_dir: Path, translated_stems: set) -> None:
    """
    Mark translated chapters' translation_status="completed" and update
    pipeline_state.translator, so manifest.json stays a truthful source for
    `mtl status`, the QC gate's completeness check, and the TUI dashboard's
    phase strip / translated-chapter count.

    Matches by `source_file` (the literal JP filename on disk), not `id` —
    the manifest's `id` is a semantic slug generated from the chapter title
    (see librarian/agent.py::_generate_chapter_id) and does not necessarily
    equal the filename stem our translator globs from JP/.
    """
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list):
        return

    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        source_stem = Path(str(entry.get("source_file", ""))).stem
        if source_stem in translated_stems:
            entry["translation_status"] = "completed"

    completed = sum(1 for c in chapters if isinstance(c, dict) and c.get("translation_status") == "completed")
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if completed == len(chapters) and chapters else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
    }
    atomic_write_json(manifest_path, manifest)


def translate_volume(volume_id: str, chapters: Optional[List[str]] = None) -> Dict[str, Path]:
    """Convenience entry point: translate a volume's JP/ chapters by volume_id."""
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")

    chapter_files = sorted(jp_dir.glob("CHAPTER_*.md"))
    if chapters:
        wanted = set(chapters)
        chapter_files = [f for f in chapter_files if f.stem in wanted or f.stem.split("_")[-1] in wanted]

    translator = DeepSeekTranslator(work_dir=work_dir, volume_id=volume_id)
    return translator.translate_all(chapter_files)
