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

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.chapter_signals import build_chapter_signal_guidance, parse_chapter_signals
from src.Deepseek.common.config import WORK_DIR, get_target_language
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.translator.config import (
    get_conversation_config,
    get_continuation_config,
    get_master_prompt_path,
    get_optimizations_config,
    get_post_processing_config,
    get_thinking_log_config,
)
from src.Deepseek.translator.context_manager import load_context_xml
from src.Deepseek.translator.deepseek_client import DeepSeekClient
from src.Deepseek.translator.deepseek_optimization import (
    assemble_deepseek_chapter_blocks,
    build_deepseek_voice_collective_block,
    derive_chapter_eps_band,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
)
from src.Deepseek.translator.prompt_loader import (
    build_continuation_prompt,
    build_system_instruction,
    build_user_message,
)
from src.Deepseek.translator.scene_break_formatter import SceneBreakFormatter
from src.Deepseek.translator.thinking_output import merge_thinking_log, split_thinking_from_output

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

    def __init__(
        self,
        work_dir: Path,
        volume_id: str,
        config: Optional[Dict[str, Any]] = None,
        thinking_log_enabled: Optional[bool] = None,
        dry_run: bool = False,
    ):
        self.work_dir = Path(work_dir)
        self.volume_id = volume_id
        self._config = config or {}
        # Developer flag: assemble each chapter's full API payload and write
        # it to WORK/<vol>/DRY_RUN/ instead of sending it. See dry_run.py and
        # DeepSeekClient.generate()'s dry_run docstring for what's guaranteed
        # (zero network calls) and what's traded away to guarantee it
        # (conversation-accumulated payload shape, for multi-turn volumes).
        self.dry_run = dry_run

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

        continuation_cfg = get_continuation_config()
        self.continuation_enabled = bool(continuation_cfg.get("enabled", True))
        self.max_output_continuations = max(
            0, int(continuation_cfg.get("max_continuations", 3) or 3)
        )

        thinking_log_cfg = get_thinking_log_config()
        # `thinking_log_enabled=False` (e.g. from --no-thinking-log) always
        # wins over config.yaml; `None` means "use whatever config.yaml says,"
        # matching the plan's "ON by default, config-gated" framing.
        self.thinking_log_enabled = (
            thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        )
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")

        # Track per-chapter guidance text for DRDI skip optimization.
        # When consecutive chapters share EPS band + active characters,
        # the full DRDI/DOVB block is replaced with a one-line CONTINUE
        # directive — saving ~200-500 uncached input tokens per turn.
        self._previous_guidance_text: Optional[str] = None

        context_xml = load_context_xml(self.work_dir)
        if context_xml is None:
            logger.warning(
                "[CONTEXT-XML] No context.xml found at %s — translating with "
                "inline master-prompt guidance only. Character voice, term "
                "locks, and continuity anchors will NOT be enforced.",
                self.work_dir / "context.xml",
            )

        # Parse the per-chapter canon once, up front. eps_signals gives each
        # chapter its true EPS band and active character set; voice_fingerprints
        # supplies the DOVB voice profiles. Without these, every chapter falls
        # back to eps_band="NEUTRAL"/active_characters=None, which silently
        # defeats the per-chapter narrowing AND makes the CONTINUE optimization
        # fire unconditionally (identical guidance every turn) — asserting "same
        # EPS band as previous chapter" without ever checking. Both maps are
        # empty (safe no-ops) when context.xml is absent or the blocks are
        # missing.
        self._voice_profiles: Dict[str, Dict[str, Any]] = parse_voice_fingerprints(context_xml)
        self._eps_signals: Dict[str, List[Dict[str, Any]]] = parse_eps_signals(context_xml)
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type: str = parse_volume_type(context_xml)

        voice_block = self._build_volume_voice_anchor()
        self.system_instruction = build_system_instruction(
            context_xml=context_xml,
            voice_block=voice_block,
            prompt_path=get_master_prompt_path(),
        )

    def _build_volume_voice_anchor(self) -> Optional[str]:
        """
        Whole-cast DOVB anchor for CHARACTER_VOICE_SLOT (volume-stable, cached).

        Preferred source is config.yaml's
        `translation.deepseek.static_voice_profiles` (operator-curated). When
        that is absent, fall back to the <voice_fingerprints> block parsed from
        context.xml at construction — the same data, already on disk — rather
        than silently dropping the slot. Only when BOTH are missing does the
        anchor vanish, and that now logs a warning instead of failing quiet,
        because an absent volume voice anchor is exactly the condition that lets
        a contrast-critical cast (e.g. this volume's Maria/Kanon) drift.
        """
        if not self.optimizations.get("dovb", {}).get("enabled", True):
            return None
        static_profiles = self._config.get("static_voice_profiles") or {}
        source = static_profiles if static_profiles else self._voice_profiles
        if not source:
            logger.warning(
                "[VOICE-ANCHOR] No voice profiles available (neither "
                "static_voice_profiles in config nor a <voice_fingerprints> "
                "block in context.xml) — CHARACTER_VOICE_SLOT left empty; "
                "whole-cast voice differentiation will NOT be anchored."
            )
            return None
        if not static_profiles:
            logger.info(
                "[VOICE-ANCHOR] static_voice_profiles not set — building volume "
                "voice anchor from context.xml <voice_fingerprints> (%d characters).",
                len(source),
            )
        active_characters = [
            {"name": name, "fingerprint": profile}
            for name, profile in source.items()
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

        # Resolve per-chapter metadata. Explicit chapter_meta always wins (the
        # MCP single-chapter tool may supply it); otherwise fall back to the
        # context.xml eps_signals parsed at construction. The previous behavior
        # — unconditional NEUTRAL/None — is retained only as the final default
        # when neither source has the chapter, so a missing block degrades
        # gracefully instead of failing.
        signal_characters = self._eps_signals.get(chapter_id, [])
        eps_band = chapter_meta.get("eps_band") or derive_chapter_eps_band(signal_characters)
        active_characters = chapter_meta.get("active_characters")
        if active_characters is None and signal_characters:
            active_characters = [
                {"name": c["name"], "fingerprint": self._voice_profiles.get(c["name"], {})}
                for c in signal_characters
                if c.get("name")
            ]
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))

        jp_source = Path(chapter_path).read_text(encoding="utf-8")

        guidance_blocks = assemble_deepseek_chapter_blocks(
            eps_band=eps_band,
            active_characters=active_characters,
            reasoning_directive_enabled=self.optimizations.get("drdi", {}).get("enabled", True),
            voice_block_enabled=self.optimizations.get("dovb", {}).get("enabled", True),
            volume_type=self._volume_type,
        )
        if signal_guidance:
            guidance_blocks.append({"type": "chapter_signals", "text": signal_guidance})

        # Assemble guidance text for comparison (must match the block text
        # that build_user_message assembles from these same blocks).
        current_guidance = "\n\n".join(
            block.get("text", "")
            for block in guidance_blocks
            if isinstance(block, dict) and block.get("text")
        )

        user_message = build_user_message(
            jp_source=jp_source,
            chapter_id=chapter_id,
            chapter_guidance_blocks=guidance_blocks,
            previous_guidance_text=self._previous_guidance_text,
        )

        # Rotate: current becomes previous for the next chapter's comparison.
        self._previous_guidance_text = current_guidance if current_guidance else None

        response = self.client.generate(
            prompt=user_message,
            system_instruction=self.system_instruction,
            dry_run=self.dry_run,
        )

        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt
            out_path = write_dry_run_prompt(
                work_dir=self.work_dir, volume_id=self.volume_id, chapter_id=chapter_id,
                payload=response.provider_metadata["payload"],
            )
            logger.info("[DRY-RUN] %s — payload written to %s (no API call made)", chapter_id, out_path)
            # No thinking log, no cost log, no conversation commit — none of
            # those are true for a call that never happened.
            return f"[DRY RUN — no translation performed. Payload written to {out_path}]"

        from src.Deepseek.common.token_telemetry import log_call
        # Output-cap continuation loop. On this endpoint max_tokens caps
        # reasoning + translation text TOGETHER, so a heavily-reasoned
        # chapter can be cut mid-scene long before the text alone would fill
        # the budget (observed: CHAPTER_02/05 truncated while reasoning
        # consumed ~12k of the 16k-token cap). When the response stops with
        # stop_reason=max_tokens, issue follow-up calls that continue from
        # the exact cut point instead of shipping a truncated chapter.
        raw_parts: List[str] = []
        leaked_blocks_all: List[str] = []
        thinking_parts: List[str] = []
        continuation_count = 0
        while True:
            # Strip any <thinking>...</thinking> that leaked into the content
            # channel alongside a real chapter — a different failure mode from
            # DeepSeekClient's own empty-content reasoning-channel salvage,
            # which only fires when content is empty. This always runs,
            # regardless of thinking_log_enabled: a leaked block has no
            # business in the shipped chapter or in conversation history
            # either way.
            cleaned_content, leaked_blocks = split_thinking_from_output(response.content)
            raw_parts.append(cleaned_content)
            leaked_blocks_all.extend(leaked_blocks or [])
            if response.thinking_content:
                thinking_parts.append(str(response.thinking_content))
            log_call(
                phase="translator",
                volume_id=self.volume_id,
                call_label=chapter_id if continuation_count == 0 else f"{chapter_id}#continue-{continuation_count}",
                model=response.model,
                cache_hit_tokens=response.cached_tokens,
                fresh_tokens=max(0, response.input_tokens - response.cached_tokens),
                output_tokens=response.output_tokens,
                cost_usd=response.total_cost_usd,  # already correctly computed by generate()
            )
            truncated = (
                response.termination == LLMTermination.MAX_OUTPUT
                or str(response.raw_finish_reason or "").strip().lower()
                in {"length", "max_tokens", "max_output_tokens", "max_completion_tokens", "incomplete"}
            )
            if not truncated:
                break
            if not self.continuation_enabled or continuation_count >= self.max_output_continuations:
                logger.warning(
                    "[TRANSLATE] %s — output hit the output cap after %d "
                    "continuation call(s); shipping the partial translation.",
                    chapter_id, continuation_count,
                )
                break
            continuation_count += 1
            logger.info(
                "[TRANSLATE] %s — output truncated; issuing continuation #%d",
                chapter_id, continuation_count,
            )
            response = self.client.generate(
                prompt=build_continuation_prompt(
                    chapter_id=chapter_id,
                    jp_source=jp_source,
                    partial_parts=raw_parts,
                ),
                system_instruction=self.system_instruction,
                dry_run=False,
            )
            if response.provider_metadata.get("dry_run"):
                break

        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(thinking_parts) or None,
            leaked_blocks=leaked_blocks_all,
        )

        en_text = self._post_process(
            "\n".join(part for part in raw_parts if part and part.strip())
        )

        self.client.commit_conversation_turn(
            response=response,
            chapter_id=chapter_id,
            output_path=self._default_output_path(chapter_id),
            canonical_output=en_text,
        )

        return en_text

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
            f"- **Target language:** {get_target_language()}\n"
            f"- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            f"## DeepSeek V4 Pro's Translation Reasoning\n\n"
        )
        atomic_write_text(thinking_dir / f"{chapter_id}_THINKING.md", header + merged + "\n")
        self._maybe_rebuild_density_map()

    def _maybe_rebuild_density_map(self) -> None:
        """Rebuild THINKING/density_map.html from every THINKING/*.md file on
        disk so far. Cheap (local parsing + string-built SVG, no LLM call) —
        safe to redo after every chapter. Never raises: a malformed or
        not-yet-populated context.xml just means the map isn't ready yet,
        not a translation failure."""
        from src.Deepseek.translator.config import get_thinking_log_config
        density_cfg = get_thinking_log_config().get("density_map", {}) or {}
        if not density_cfg.get("enabled", True):
            return
        from src.Deepseek.translator.thinking_density import build_density_report
        try:
            build_density_report(self.work_dir, self.volume_id)
        except Exception as exc:
            logger.warning("[THINKING] %s — density map rebuild failed: %s", self.volume_id, exc)

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

    def translate_and_persist_chapter(
        self,
        chapter_path: Path,
        chapter_meta: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """
        Translate one chapter, write its EN output, and mark it completed in
        manifest.json — all three, every time, in one place.

        This is the one method every caller (the CLI's bulk loop below, the
        MCP `run_translator` tool, and the MCP `translate_chapter` tool) goes
        through. It used to be duplicated: `translate_all()` wrote the file
        itself and only updated the manifest once at the very end of its
        loop, and `translator_server.py`'s single-chapter MCP tool wrote the
        file itself too but never touched the manifest at all. Either path
        left manifest.json silently out of sync with real EN/ output the
        moment anything after it failed — a crash on chapter 6 discarded
        chapters 1-5's completed status even though their files were
        sitting right there on disk. Updating per-chapter, immediately after
        that chapter's file lands, means a later failure can't retroactively
        un-persist earlier successes.
        """
        chapter_meta = chapter_meta or {}
        chapter_id = chapter_meta.get("chapter_id", chapter_path.stem)
        en_text = self.translate_chapter(chapter_path, {**chapter_meta, "chapter_id": chapter_id})
        if self.dry_run:
            # en_text is the dry-run placeholder marker, not a translation —
            # writing it to EN/ or marking the chapter completed would
            # corrupt real pipeline state (QC's completeness check, `mtl
            # status`, the TUI's phase strip all trust translation_status).
            return self.work_dir / "DRY_RUN"
        output_path = self._default_output_path(chapter_id)
        output_path.write_text(en_text, encoding="utf-8")
        _update_manifest_after_translation(self.work_dir, {chapter_id})
        return output_path

    def translate_all(self, chapter_files: List[Path]) -> Dict[str, Path]:
        """
        Sequential loop over chapters. Writes each result to
        WORK/<vol>/EN/<chapter_id>_EN.md, marks it completed in manifest.json
        immediately, and returns {chapter_id: output_path}.
        """
        results: Dict[str, Path] = {}
        for chapter_path in sorted(chapter_files):
            chapter_id = chapter_path.stem
            logger.info("[TRANSLATE] %s — starting", chapter_id)
            output_path = self.translate_and_persist_chapter(chapter_path, {"chapter_id": chapter_id})
            results[chapter_id] = output_path
            logger.info("[TRANSLATE] %s — wrote %s", chapter_id, output_path)
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


def _filter_completed_chapters(work_dir: Path, chapter_files: List[Path]) -> List[Path]:
    """Remove chapters whose manifest entry already says 'completed'.

    Returns the filtered list.  If the manifest is missing, unreadable, or
    has no chapters array, returns the original list unchanged (fail-open:
    better to re-translate than to silently skip everything).
    """
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.exists():
        return chapter_files
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return chapter_files

    manifest_chapters = manifest.get("chapters", [])
    if not isinstance(manifest_chapters, list):
        return chapter_files

    # Build a set of source_file stems that are already completed.
    completed_stems: set[str] = set()
    for entry in manifest_chapters:
        if not isinstance(entry, dict):
            continue
        if entry.get("translation_status") != "completed":
            continue
        source_file = str(entry.get("source_file", ""))
        if source_file:
            completed_stems.add(Path(source_file).stem)

    if not completed_stems:
        return chapter_files

    skipped: list[str] = []
    remaining: list[Path] = []
    for f in chapter_files:
        if f.stem in completed_stems:
            skipped.append(f.stem)
        else:
            remaining.append(f)

    if skipped:
        logger.info(
            "[TRANSLATE] Skipping %d already-completed chapter(s): %s",
            len(skipped), ", ".join(sorted(skipped)),
        )
    return remaining


def translate_volume(
    volume_id: str,
    chapters: Optional[List[str]] = None,
    thinking_log_enabled: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Path]:
    """Convenience entry point: translate a volume's JP/ chapters by volume_id."""
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise FileNotFoundError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")

    chapter_files = sorted(jp_dir.glob("CHAPTER_*.md"))
    if chapters:
        wanted = set(chapters)
        chapter_files = [f for f in chapter_files if f.stem in wanted or f.stem.split("_")[-1] in wanted]

    # Respect manifest: skip chapters already marked completed, so a
    # partial run that crashed mid-volume doesn't re-translate chapters
    # whose EN/ output is already on disk and whose manifest entry is
    # truthful.  The single-chapter MCP tool (translate_chapter) is the
    # escape hatch for intentionally redoing one.  Dry-run bypasses this
    # entirely — it is a developer inspection mode, so it assembles
    # payloads for every requested chapter regardless of manifest state.
    if not dry_run:
        chapter_files = _filter_completed_chapters(work_dir, chapter_files)

    translator = DeepSeekTranslator(
        work_dir=work_dir,
        volume_id=volume_id,
        thinking_log_enabled=thinking_log_enabled,
        dry_run=dry_run,
    )
    return translator.translate_all(chapter_files)
