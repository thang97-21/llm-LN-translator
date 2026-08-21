"""OpenAI Phase 2 translator facade with the established filesystem contract."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.config import WORK_DIR, get_safety_fallback_config
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.safety_fallback import fallback_translate_chapter
from src.Deepseek.common.token_telemetry import log_call
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, split_thinking_from_output
from src.OpenAI.client import OpenAIClient
from src.OpenAI.config import (
    get_openai_config,
    get_openai_continuation_config,
    get_openai_conversation_config,
    get_openai_optimization_config,
    get_openai_prompt_path,
)
from src.OpenAI.context import (
    derive_chapter_eps_band,
    parse_character_roster_handles,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
    resolve_voice_aliases,
)
from src.OpenAI.errors import OpenAIAPIError, OpenAIRefusalError
from src.OpenAI.optimization import build_chapter_guidance
from src.OpenAI.prompt_loader import build_chapter_message, build_continuation_message, build_system_instruction

logger = logging.getLogger(__name__)
_CJK_LEAK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")


class OpenAITranslator:
    """Translate a volume through native Responses while preserving MTLS outputs."""

    def __init__(
        self,
        work_dir: Path,
        volume_id: str,
        config: Optional[Dict[str, Any]] = None,
        thinking_log_enabled: Optional[bool] = None,
        dry_run: bool = False,
    ):
        self.work_dir = Path(work_dir)
        self.volume_id = str(volume_id)
        self._config = config or get_openai_config()
        self.dry_run = dry_run
        self.client = OpenAIClient(dry_run=dry_run)
        conversation_cfg = get_openai_conversation_config()
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                conversation_config=conversation_cfg,
            )
        from src.Deepseek.translator.context_manager import load_context_xml

        context_xml = load_context_xml(self.work_dir)
        self._voice_profiles = resolve_voice_aliases(
            parse_voice_fingerprints(context_xml), parse_character_roster_handles(context_xml)
        )
        self._eps_signals = parse_eps_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        self.system_instruction = build_system_instruction(
            prompt_path=get_openai_prompt_path(), context_xml=context_xml
        )
        self._previous_guidance_text: Optional[str] = None
        continuation = get_openai_continuation_config()
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))
        thinking_log_cfg = get_thinking_log_config()
        # `thinking_log_enabled=False` (e.g. from --no-thinking-log) always
        # wins over config.yaml; `None` means "use whatever config.yaml says,"
        # matching the DeepSeek/Qwen routes' "ON by default, config-gated"
        # convention. IMPORTANT CAVEAT, unlike DeepSeek/Qwen: OpenAI never
        # exposes raw reasoning tokens via the API at all. The only thing
        # this log can ever contain is a model-generated PARAPHRASE of its
        # reasoning (`reasoning.summary` in client.py's request), which
        # OpenAI will only return if (a) the request explicitly opts in —
        # wired in client.py, config'd via translation.openai.reasoning.summary
        # — (b) the API account has completed OpenAI's organization
        # verification, and (c) the configured model tier supports summaries
        # at all (gpt-5.6-luna's own model page lists no summary support,
        # unlike plain gpt-5.6). None of (b)/(c) are detectable from this
        # codebase. When unmet, `response.thinking_content` is empty and
        # `_maybe_write_thinking_log` correctly no-ops — no crash, just no
        # file, every chapter, and that silence is NOT a bug to "fix" here.
        # `encrypted_content` (the one thing OpenAI always returns) is
        # genuinely opaque ciphertext — it lives only in the conversation
        # ledger for replay and is never written to a human-readable log.
        self.thinking_log_enabled = (
            thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        )
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")
        self.thinking_density_enabled = thinking_log_cfg.get("density_map", {}).get("enabled", True)

    def translate_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        meta = chapter_meta or {}
        chapter_id = str(meta.get("chapter_id", chapter_path.stem))
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = str(meta.get("eps_band") or derive_chapter_eps_band(signals))
        active_characters = meta.get("active_characters")
        if active_characters is None:
            active_characters = [
                {"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})}
                for signal in signals
                if signal.get("name")
            ]
        guidance = build_chapter_guidance(eps_band, active_characters, get_openai_optimization_config(), self._volume_type)
        prompt = build_chapter_message(
            chapter_id, jp_source, guidance, previous_guidance_text=self._previous_guidance_text
        )
        self._previous_guidance_text = guidance.strip() if guidance else None
        manager = self.client.conversation_manager
        input_items = (
            manager.input_items(
                self.system_instruction,
                prompt,
                int(get_openai_conversation_config().get("recent_verbatim_chapters", 2) or 2),
            )
            if manager is not None and not self.dry_run
            else None
        )
        response = self.client.generate(
            prompt=prompt,
            system_instruction=self.system_instruction,
            input_items=input_items,
            dry_run=self.dry_run,
        )
        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt

            path = write_dry_run_prompt(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_id=chapter_id,
                payload=response.provider_metadata["payload"],
                provider="openai",
            )
            return f"[DRY RUN — no translation performed. Payload written to {path}]"
        if response.termination == LLMTermination.REFUSED:
            return self._safety_fallback_translate(
                chapter_path, chapter_id,
                OpenAIRefusalError(f"OpenAI declined translation of {chapter_id}."),
            )

        chapter_input = _user_input(prompt)
        request_input = input_items or _single_turn_input(self.system_instruction, prompt)
        replay_prefix: List[Dict[str, Any]] = [chapter_input]
        parts = [response.content]
        thinking_parts: List[str] = []
        if response.thinking_content:
            thinking_parts.append(str(response.thinking_content))
        continuation_count = 0
        self._log_usage(chapter_id, response)
        while (
            response.termination == LLMTermination.MAX_OUTPUT
            and self.continuation_enabled
            and continuation_count < self.max_continuations
        ):
            continuation_count += 1
            raw_output = _raw_output(response)
            if not raw_output:
                raise OpenAIAPIError(f"OpenAI response for {chapter_id} ended at the output limit without replayable output items.")
            continuation_input = _user_input(build_continuation_message(chapter_id))
            request_input = [*request_input, *raw_output, continuation_input]
            replay_prefix.extend([*raw_output, continuation_input])
            response = self.client.generate(
                prompt=continuation_input["content"][0]["text"],
                system_instruction=self.system_instruction,
                input_items=request_input,
            )
            if response.termination == LLMTermination.REFUSED:
                return self._safety_fallback_translate(
                    chapter_path, chapter_id,
                    OpenAIRefusalError(f"OpenAI declined continuation of {chapter_id}."),
                )
            parts.append(response.content)
            if response.thinking_content:
                thinking_parts.append(str(response.thinking_content))
            self._log_usage(f"{chapter_id}#continue-{continuation_count}", response)

        raw_text = "\n".join(part for part in parts if part)
        text, leaked_blocks = split_thinking_from_output(raw_text)
        text = _CJK_LEAK_RE.sub("", text)
        if not text.strip():
            raise OpenAIAPIError(f"OpenAI returned no visible translation text for {chapter_id}.")
        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(thinking_parts) or None,
            leaked_blocks=leaked_blocks,
        )
        if manager is not None:
            manager.commit(
                chapter_id=chapter_id,
                user_input=replay_prefix,
                response_output=_raw_output(response),
                chapter_text=text,
                output_path=self.work_dir / "EN" / f"{chapter_id}_EN.md",
            )
        return text

    def _safety_fallback_translate(self, chapter_path: Path, chapter_id: str, exc: OpenAIRefusalError) -> str:
        """OpenAI safety-refusal fallback: do NOT retry. Switch to DeepSeek
        with decision inheritance injected into context.xml, and return the
        EN text. See src/Deepseek/common/safety_fallback.py.

        The caller (translate_and_persist_chapter) writes the returned text
        to EN/ and marks the chapter completed, exactly as it would for a
        normal OpenAI success — the fallback only changes where the text
        came from.
        """
        cfg = get_safety_fallback_config()
        if not cfg.get("enabled", True):
            logger.warning("[OPENAI-SAFETY] %s — safety fallback disabled; re-raising refusal", chapter_id)
            raise exc

        try:
            return fallback_translate_chapter(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_path=chapter_path,
                chapter_id=chapter_id,
                refusal=exc,
                source_provider="OpenAI",
                refusal_code_default="model_refusal",
                dry_run=self.dry_run,
            )
        except OpenAIRefusalError:
            raise
        except Exception as fallback_exc:  # noqa: BLE001 - surface any fallback failure loudly
            logger.error(
                "[OPENAI-SAFETY] %s — DeepSeek fallback failed (%s); re-raising original refusal",
                chapter_id, fallback_exc,
            )
            raise exc from fallback_exc

    def _log_usage(self, call_label: str, response) -> None:
        try:
            cache_metrics = response.provider_metadata.get("cache_telemetry", {}) or {}
            economics = cache_metrics.get("cache_economics", {}) or {}
            log_call(
                phase="translator",
                volume_id=self.volume_id,
                call_label=call_label,
                model=response.model,
                cache_hit_tokens=response.cached_tokens,
                cache_write_tokens=response.cache_creation_tokens,
                fresh_tokens=max(
                    0,
                    response.input_tokens - response.cached_tokens - response.cache_creation_tokens,
                ),
                output_tokens=response.output_tokens,
                cost_usd=response.total_cost_usd,
                provider="openai",
                breakpoint_success_rate=cache_metrics.get("breakpoint_success_rate"),
                prefix_recovery=cache_metrics.get("prefix_recovery"),
                total_cache_coverage=cache_metrics.get("total_cache_coverage"),
                cache_net_savings_usd=economics.get("net_input_savings_usd"),
            )
        except Exception as exc:  # telemetry must never prevent a translation
            logger.warning("[OPENAI] token log failed for %s: %s", call_label, exc)

    def _maybe_write_thinking_log(
        self,
        *,
        chapter_id: str,
        api_thinking: Optional[str],
        leaked_blocks: List[str],
    ) -> None:
        """Archive this chapter's reasoning SUMMARY (a model-generated
        paraphrase — OpenAI never returns raw reasoning tokens) to
        THINKING/<chapter_id>_THINKING.md, if thinking_log.enabled and the
        API actually returned summary text this call. Reasoning tokens are
        already paid for whether or not this runs — the flag only controls
        whether any returned summary text is kept."""
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
            f"- **Provider:** OpenAI\n"
            f"- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            f"## OpenAI Reasoning Summary (model-generated paraphrase, not raw reasoning)\n\n"
        )
        atomic_write_text(thinking_dir / f"{chapter_id}_THINKING.md", header + merged + "\n")
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
                "[OPENAI-THINKING] %s — density map rebuild failed: %s",
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
        return {
            path.stem: self.translate_and_persist_chapter(path, {"chapter_id": path.stem})
            for path in sorted(chapter_files)
        }


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
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if chapters and completed == len(chapters) else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
    }
    atomic_write_json(path, manifest)


def _filter_completed_chapters(work_dir: Path, chapter_files: List[Path]) -> List[Path]:
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return chapter_files
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return chapter_files
    completed = {
        Path(str(entry.get("source_file", ""))).stem
        for entry in manifest.get("chapters", [])
        if isinstance(entry, dict) and entry.get("translation_status") == "completed"
    }
    return [path for path in chapter_files if path.stem not in completed]


def translate_volume(
    volume_id: str,
    chapters: Optional[List[str]] = None,
    thinking_log_enabled: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Path]:
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
    return OpenAITranslator(
        work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run
    ).translate_all(chapter_files)


def _user_input(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": text}]}


def _single_turn_input(system_instruction: str, prompt: str) -> List[Dict[str, Any]]:
    return [
        {"role": "developer", "content": [{"type": "input_text", "text": system_instruction}]},
        _user_input(prompt),
    ]


def _raw_output(response) -> List[Dict[str, Any]]:
    output = response.provider_metadata.get("raw_output") or []
    return [dict(item) for item in output if isinstance(item, dict)]
