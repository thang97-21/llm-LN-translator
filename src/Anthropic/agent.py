"""Anthropic Phase 2 translator facade with the established filesystem contract."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from src.Anthropic.client import AnthropicClient
from src.Anthropic.config import (
    get_anthropic_batch_config,
    get_anthropic_config,
    get_anthropic_telemetry_config,
    get_anthropic_continuation_config,
    get_anthropic_conversation_config,
    get_anthropic_optimization_config,
    get_anthropic_caching_config,
    get_anthropic_prompt_path,
)
from src.Anthropic.context import (
    derive_chapter_eps_band,
    parse_character_roster_handles,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
    resolve_voice_aliases,
)
from src.Anthropic.errors import AnthropicAPIError, AnthropicRefusalError
from src.Anthropic.optimization import build_chapter_guidance
from src.Anthropic.prompt_loader import (
    build_chapter_message,
    build_continuation_message,
    build_system_segments,
)
from src.Anthropic.client import THINKING_ALWAYS_ON_MODELS, build_cache_control, build_system_blocks
from src.Anthropic.response import sanitize_replayable_block
from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.chapter_signals import build_chapter_signal_guidance, parse_chapter_signals
from src.Deepseek.common.config import PIPELINE_ROOT, WORK_DIR, get_safety_fallback_config
from src.Deepseek.common.verbatim_anchors import (
    anchors_in_source,
    merge_bible_anchors,
    observe_prior_usage,
    parse_anchors,
    reconcile_chapter,
)
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.safety_fallback import fallback_translate_chapter
from src.Deepseek.common.token_telemetry import log_call
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, split_thinking_from_output

logger = logging.getLogger(__name__)
_CJK_LEAK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿＀-￯]")
# Chapter ids in this pipeline are CHAPTER_NN, zero-padded to a width the
# librarian fixes per volume. Splitting the trailing run of digits lets the
# predecessor be derived exactly, and re-padded to the same width.
_CHAPTER_INDEX_RE = re.compile(r"^(.*?)(\d+)$")


class AnthropicTranslator:
    """Translate a volume through the real Anthropic Messages API while
    preserving MTLS's shared filesystem contract (EN/, THINKING/, manifest.json)."""

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
        self._config = config or get_anthropic_config()
        self.dry_run = dry_run
        self.client = AnthropicClient(dry_run=dry_run)
        conversation_cfg = get_anthropic_conversation_config()
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
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        # The verbatim-anchor lexicon, read once. context.xml supplies the
        # volume's anchors; the series bible supplies the cross-volume policy
        # fields that context.xml only carries as prose -- forbidden_synonyms
        # above all, without which drift detection is blind. A series on its
        # first volume has no bible, and that is not an error.
        self._anchors = merge_bible_anchors(
            parse_anchors(context_xml), self._series_bible_anchors_path()
        )
        if self._anchors:
            logger.info(
                "[ANTHROPIC-ANCHORS] %d verbatim anchor(s) loaded for %s",
                len(self._anchors),
                self.volume_id,
            )
        # Two cache layers: the static craft policy, then this volume's
        # context. Sent as separate blocks so starting a new volume rewrites
        # only the second cache entry instead of discarding the policy with it.
        self.system_segments = build_system_segments(
            prompt_path=get_anthropic_prompt_path(), context_xml=context_xml
        )
        self.system_instruction = "\n\n".join(self.system_segments)
        # Tell the ledger which system prefix these turns belong to. On a
        # prefix-bound model (Fable 5.1) the system prompt is part of every
        # thinking block's signature, and context.xml is rewritten in place by
        # the DeepSeek safety fallback — so a resumed run can present a
        # different prefix than the ledger on disk was written under. Binding
        # here lets the manager strip the now-unreplayable blocks once,
        # instead of the volume failing on its next call. A no-op on
        # claude-opus-5 / claude-sonnet-5, which do not bind the prefix.
        if self.client.conversation_manager is not None:
            self.client.conversation_manager.bind_system(self.system_segments)
        self._previous_guidance_text: Optional[str] = None
        # Characters of the preceding chapter's ENDING spliced into an
        # envelope when the ledger no longer replays that chapter in full.
        # 0 disables the splice.
        self._previous_tail_chars = max(
            0, int(conversation_cfg.get("previous_chapter_tail_chars", 1200) or 1200)
        )
        continuation = get_anthropic_continuation_config()
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))
        thinking_log_cfg = get_thinking_log_config()
        self.thinking_log_enabled = (
            thinking_log_cfg.get("enabled", True) if thinking_log_enabled is None else thinking_log_enabled
        )
        self.thinking_log_dir_name = thinking_log_cfg.get("output_dir", "THINKING")
        self.thinking_density_enabled = thinking_log_cfg.get("density_map", {}).get("enabled", True)

    def _series_bible_anchors_path(self) -> Path:
        """bibles/<series_id>/verbatim_anchors.json for this volume's series.

        The series id is recorded by prep in manifest.json. A volume whose
        manifest predates that field, or whose bible has not been written yet,
        yields a path that simply does not exist -- which merge_bible_anchors
        treats as "no enrichment available" rather than a failure.
        """
        series_id = ""
        try:
            manifest = json.loads((self.work_dir / "manifest.json").read_text(encoding="utf-8"))
            series_id = str(
                ((manifest.get("pipeline_state") or {}).get("prep") or {}).get("series_id") or ""
            )
        except (OSError, ValueError, AttributeError):
            series_id = ""
        return PIPELINE_ROOT / "bibles" / (series_id or "_absent") / "verbatim_anchors.json"

    def _anchor_rows(self, chapter_id: str, jp_source: str) -> List[Dict[str, Any]]:
        """Locked phrases this chapter's source contains, with prior usage.

        Two halves, and the second is the one context.xml cannot provide. The
        lock says what the surface must be; the harvest says what earlier
        chapters actually wrote, quoted from EN/. A chapter whose predecessor
        is in flight beside it in the same batch wave has no other way to
        learn it -- which is how こほろん reached print as "kohoron", "Kohon",
        and "Ahem" across CH18 and CH19 of Vol.4.
        """
        if not self._anchors or not jp_source:
            return []
        present = anchors_in_source(self._anchors, jp_source)
        if not present:
            return []
        observations = observe_prior_usage(
            present, self.work_dir / "EN", exclude_chapter_id=chapter_id
        )
        rows: List[Dict[str, Any]] = []
        for observation in observations:
            anchor = observation.anchor
            rows.append(
                {
                    "anchor_id": anchor.anchor_id,
                    "jp": anchor.jp,
                    "en": anchor.en,
                    "source_occurrences": observation.source_occurrences,
                    "forbidden_synonyms": list(anchor.forbidden_synonyms),
                    "established_in_chapter": observation.established_in_chapter,
                    "citation": observation.citations[0] if observation.citations else "",
                }
            )
        return rows

    def _reconcile_anchors(self, chapter_id: str, en_text: str) -> None:
        """Record whether a finished chapter honoured the locks its source hit.

        The return leg of the reconciliation. context.xml's anchors already
        carry an <en_output status="pending"> slot for exactly this, and on a
        completed 21-chapter Vol.4 run every one of the fifteen was still
        "pending" -- the schema anticipated the check and nothing performed it.

        It is deliberately NOT written back into context.xml. That document is
        the cached system prefix, and on a prefix-bound model rewriting it
        mid-volume invalidates the prompt cache and every stored thinking
        block (see AnthropicConversationManager.bind_system). A later QC or
        bible pass can fold this artifact home when the run is over.
        """
        if not self._anchors:
            return
        try:
            jp_source = (self.work_dir / "JP" / f"{chapter_id}.md").read_text(encoding="utf-8")
        except OSError:
            return
        rows = reconcile_chapter(
            self._anchors, chapter_id=chapter_id, jp_source=jp_source, en_text=en_text
        )
        if not rows:
            return
        path = self.work_dir / ".context" / "anchor_reconciliation.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing.setdefault("volume_id", self.volume_id)
        existing.setdefault("chapters", {})
        existing["chapters"][chapter_id] = rows
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, existing)
        flagged = [row for row in rows if row.get("status") in ("missing", "drifted")]
        if flagged:
            logger.warning(
                "[ANTHROPIC-ANCHORS] %s: %d anchor(s) not honoured: %s",
                chapter_id,
                len(flagged),
                ", ".join(f"{row['anchor_id']}({row['status']})" for row in flagged),
            )

    def _preceding_chapter_id(self, chapter_id: str) -> Optional[str]:
        """The chapter id immediately before this one, or None.

        Derived from the trailing digits and re-padded to the same width, so
        CHAPTER_07 yields CHAPTER_06 rather than CHAPTER_6. A stem with no
        trailing number, or the first chapter, gets None rather than a guess.
        """
        match = _CHAPTER_INDEX_RE.match(str(chapter_id))
        if match is None:
            return None
        prefix, digits = match.group(1), match.group(2)
        index = int(digits)
        if index <= 1:
            return None
        return "%s%0*d" % (prefix, len(digits), index - 1)

    def _chapter_tail_from_disk(self, chapter_id: str) -> str:
        """The closing passage of an already-translated chapter, from EN/.

        The cheapest continuity in the system: a chapter a fold reduced to one
        summary line still has its full English on disk, and its ending is
        precisely the part the next chapter reaches back for.
        """
        if self._previous_tail_chars <= 0:
            return ""
        try:
            text = (self.work_dir / "EN" / f"{chapter_id}_EN.md").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not text:
            return ""
        if len(text) <= self._previous_tail_chars:
            return text
        return text[-self._previous_tail_chars :].lstrip()

    def _continuity_brief(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        """Describe the continuity the request for *chapter_id* will carry.

        Three states matter and the envelope has to tell them apart: the
        preceding chapter replayed in full, folded down to a summary line, or
        not present at all. The third is the batch path's ordinary condition
        inside a wave - every request is built before any sibling runs - and
        it was previously indistinguishable from the first, which is how
        Fable 5.1 came to guess at a CH02 callback it could not see
        (THINKING/CHAPTER_03_THINKING.md on Vol.4).

        Returns None when no ledger is attached, which leaves the envelope
        rendering exactly as it did before any of this existed.
        """
        manager = self.client.conversation_manager
        if manager is None:
            return None
        state = manager.continuity_state()
        verbatim = list(state.get("verbatim_chapter_ids") or [])
        summarized = list(state.get("summarized_chapter_ids") or [])
        brief: Dict[str, Any] = {
            "verbatim_chapter_ids": verbatim,
            "summarized_chapter_ids": summarized,
        }
        previous_id = self._preceding_chapter_id(chapter_id)
        if not previous_id:
            return brief
        brief["previous_chapter_id"] = previous_id
        if previous_id in verbatim:
            brief["previous_chapter_status"] = "translated_in_full_in_this_request"
            return brief
        brief["previous_chapter_status"] = "summarized" if previous_id in summarized else "absent"
        tail = self._chapter_tail_from_disk(previous_id)
        if tail:
            brief["previous_chapter_tail"] = tail
        return brief

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
        guidance = build_chapter_guidance(eps_band, active_characters, get_anthropic_optimization_config(), self._volume_type)
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        prompt = build_chapter_message(
            chapter_id,
            jp_source,
            guidance,
            previous_guidance_text=self._previous_guidance_text,
            continuity=self._continuity_brief(chapter_id),
            anchor_rows=self._anchor_rows(chapter_id, jp_source),
        )
        self._previous_guidance_text = guidance.strip() if guidance else None
        manager = self.client.conversation_manager
        messages = (
            manager.build_messages(prompt, cache_control=self._history_cache_control())
            if manager is not None and not self.dry_run
            else None
        )
        response = self.client.generate(
            prompt=prompt,
            system_instruction=self.system_segments,
            messages=messages,
            dry_run=self.dry_run,
        )
        if response.provider_metadata.get("dry_run"):
            from src.Deepseek.translator.dry_run import write_dry_run_prompt

            path = write_dry_run_prompt(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_id=chapter_id,
                payload=response.provider_metadata["payload"],
                provider="anthropic",
            )
            return f"[DRY RUN — no translation performed. Payload written to {path}]"
        if response.termination == LLMTermination.REFUSED:
            return self._safety_fallback_translate(
                chapter_path, chapter_id,
                AnthropicRefusalError(f"Anthropic declined translation of {chapter_id}."),
            )

        chapter_user_message = _user_message(prompt)
        current_messages = messages or _single_turn_messages(prompt)
        parts = [response.content]
        thinking_parts: List[str] = []
        if response.thinking_content:
            thinking_parts.append(str(response.thinking_content))
        assistant_message = _assistant_message(response)
        continuation_count = 0
        self._log_usage(chapter_id, response)
        while (
            response.termination == LLMTermination.MAX_OUTPUT
            and self.continuation_enabled
            and continuation_count < self.max_continuations
        ):
            continuation_count += 1
            continuation_prompt = build_continuation_message(chapter_id)
            current_messages = [*current_messages, assistant_message, _user_message(continuation_prompt)]
            response = self.client.generate(
                prompt=continuation_prompt,
                system_instruction=self.system_segments,
                messages=current_messages,
            )
            if response.termination == LLMTermination.REFUSED:
                return self._safety_fallback_translate(
                    chapter_path, chapter_id,
                    AnthropicRefusalError(f"Anthropic declined continuation of {chapter_id}."),
                )
            parts.append(response.content)
            if response.thinking_content:
                thinking_parts.append(str(response.thinking_content))
            assistant_message = _assistant_message(response)
            self._log_usage(f"{chapter_id}#continue-{continuation_count}", response)

        raw_text = "\n".join(part for part in parts if part)
        text, leaked_blocks = split_thinking_from_output(raw_text)
        text = _CJK_LEAK_RE.sub("", text)
        if not text.strip():
            raise AnthropicAPIError(f"Anthropic returned no visible translation text for {chapter_id}.")
        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(thinking_parts) or None,
            leaked_blocks=leaked_blocks,
        )
        if manager is not None:
            manager.commit(
                chapter_id=chapter_id,
                user_message=chapter_user_message,
                assistant_message=assistant_message,
                chapter_text=text,
                output_path=self.work_dir / "EN" / f"{chapter_id}_EN.md",
            )
        return text

    def _safety_fallback_translate(self, chapter_path: Path, chapter_id: str, exc: AnthropicRefusalError) -> str:
        """Anthropic safety-refusal fallback: do NOT retry. Switch to DeepSeek
        with decision inheritance injected into context.xml, and return the
        EN text. See src/Deepseek/common/safety_fallback.py.

        The caller (translate_and_persist_chapter) writes the returned text
        to EN/ and marks the chapter completed, exactly as it would for a
        normal Anthropic success — the fallback only changes where the text
        came from.
        """
        cfg = get_safety_fallback_config()
        if not cfg.get("enabled", True):
            logger.warning("[ANTHROPIC-SAFETY] %s — safety fallback disabled; re-raising refusal", chapter_id)
            raise exc

        try:
            return fallback_translate_chapter(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                chapter_path=chapter_path,
                chapter_id=chapter_id,
                refusal=exc,
                source_provider="Anthropic",
                refusal_code_default="model_refusal",
                dry_run=self.dry_run,
            )
        except AnthropicRefusalError:
            raise
        except Exception as fallback_exc:  # noqa: BLE001 - surface any fallback failure loudly
            logger.error(
                "[ANTHROPIC-SAFETY] %s — DeepSeek fallback failed (%s); re-raising original refusal",
                chapter_id, fallback_exc,
            )
            raise exc from fallback_exc

    def _log_usage(self, call_label: str, response) -> None:
        """Write one fully populated row to this run's token log.

        Every column log_call accepts is filled: fresh / cache-read /
        cache-write token counts, the four cache-quality metrics, and the
        actual cost at today's rates. This route is the most expensive in the
        project and was, until now, the least instrumented - its rows carried
        no cache-write count and, because ``provider`` defaults to
        "deepseek", were not even attributed to Anthropic.

        fresh_tokens is response.input_tokens UNSUBTRACTED. On the Messages
        API the three token counts are disjoint, so the old
        ``input_tokens - cached_tokens`` drove the fresh figure to zero
        whenever the cached prefix exceeded the chapter envelope, which is the
        normal case here.
        """
        telemetry_cfg = get_anthropic_telemetry_config()
        if not bool(telemetry_cfg.get("enabled", True)):
            return
        try:
            metrics = dict(response.provider_metadata.get("cache_telemetry") or {})
            economics = dict(metrics.get("cache_economics") or {})
            # The four quality metrics are session-cumulative and only exist on
            # the synchronous path; a batch result has no such session. Left as
            # None there so the row shows "-" rather than a fabricated figure.
            quality = bool(telemetry_cfg.get("cache_quality", True)) and bool(metrics)
            log_call(
                phase="translator",
                volume_id=self.volume_id,
                call_label=call_label,
                model=response.model,
                provider="anthropic",
                cache_hit_tokens=response.cached_tokens,
                cache_write_tokens=response.cache_creation_tokens,
                fresh_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.total_cost_usd,
                batch=bool(response.batch_pricing),
                breakpoint_success_rate=metrics.get("breakpoint_success_rate") if quality else None,
                prefix_recovery=metrics.get("prefix_recovery") if quality else None,
                total_cache_coverage=metrics.get("total_cache_coverage") if quality else None,
                cache_net_savings_usd=economics.get("net_input_savings_usd") if quality else None,
            )
        except Exception as exc:  # telemetry must never prevent a translation
            logger.warning("[ANTHROPIC] token log failed for %s: %s", call_label, exc)

    def _maybe_write_thinking_log(
        self,
        *,
        chapter_id: str,
        api_thinking: Optional[str],
        leaked_blocks: List[str],
    ) -> None:
        """Archive this chapter's thinking text to THINKING/<chapter_id>_THINKING.md.

        Text is only present when translation.anthropic.thinking.display is
        "summarized" — with "omitted" the API returns an empty `thinking`
        field by design (faster time-to-first-text-token, identical billing
        either way), and this correctly writes nothing rather than a
        near-empty file. Reasoning tokens are already paid for regardless of
        this flag; it only controls whether any returned text is archived.
        """
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
            f"- **Provider:** Anthropic\n"
            f"- **Timestamp:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n\n"
            f"## Anthropic Reasoning (summarized thinking block)\n\n"
        )
        atomic_write_text(thinking_dir / f"{chapter_id}_THINKING.md", header + merged + "\n")
        self._maybe_rebuild_density_map()

    def _maybe_rebuild_density_map(self) -> None:
        if not self.thinking_density_enabled:
            return
        from src.Deepseek.translator.thinking_density import build_density_report

        try:
            build_density_report(self.work_dir, self.volume_id)
        except Exception as exc:
            logger.warning(
                "[ANTHROPIC-THINKING] %s — density map rebuild failed: %s",
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
        self._reconcile_anchors(chapter_id, text)
        _update_manifest_after_translation(self.work_dir, {chapter_id})
        return output_path

    def translate_all(self, chapter_files: List[Path]) -> Dict[str, Path]:
        return {
            path.stem: self.translate_and_persist_chapter(path, {"chapter_id": path.stem})
            for path in sorted(chapter_files)
        }

    def translate_volume_batch(self, chapter_files: List[Path]) -> Dict[str, Path]:
        """Translate pending chapters through the Message Batches API in
        *waves*, so a batch run keeps the cross-chapter continuity the
        synchronous path has.

        Why waves and not one job per volume. Every request in a batch is
        submitted before any of them runs, so chapter N cannot carry chapter
        N-1's translation — it does not exist yet. A single volume-wide job
        therefore translates every chapter blind to every other, which is the
        exact failure src/utility/prep/multiturn_agent.py documents for the
        parallel prep path: consistency you can only audit after the fact.

        A wave is the compromise. Every request in one wave shares a
        byte-identical prefix — the cached system blocks plus the conversation
        ledger's replayed turns, frozen for the duration of that wave — and
        the ledger only grows between waves. That prefix is at once the
        continuity carrier and the cache key, so the 50% batch discount and
        the cache-read discount apply to the same bytes.

        ``wave_size`` is the dial: 1 gives the synchronous path's granularity
        at batch prices; a wave the size of the volume reproduces the old
        continuity-free behaviour.

        Every submission is recorded to batch.persistence_file before its poll
        loop begins, and any job a previous run left open is collected first —
        a batch is billed on acceptance, so resubmitting its chapters would
        pay for them twice.

        Opt-in via translation.anthropic.batch.enabled — never a silent
        fallback from the synchronous path, since batch cost and latency are a
        deliberate operator choice, not an automatic substitution.
        """
        from src.Anthropic.batch import BatchLedger, retrieve_batch_results, submit_batch

        batch_cfg = get_anthropic_batch_config()
        pending = sorted(chapter_files)
        if not pending:
            return {}

        manager = self.client.conversation_manager
        wave_size = max(1, int(batch_cfg.get("wave_size", 4) or 4))
        if wave_size > 1:
            logger.info(
                "[ANTHROPIC-BATCH] wave_size=%d: a batch submits every request before any of "
                "them runs, so %d chapter(s) per wave carry no predecessor newer than the "
                "previous wave. Each is told so by name in its continuity_state block, and its "
                "predecessor's ending is spliced from EN/ where one exists. wave_size: 1 gives "
                "full sequential continuity at batch prices.",
                wave_size,
                wave_size - 1,
                )
        poll_seconds = max(1, int(batch_cfg.get("poll_seconds", 60) or 60))
        deadline_seconds = _completion_window_seconds(batch_cfg.get("completion_window", "24h"))

        written: Dict[str, Path] = {}
        unfinished: Dict[str, str] = {}

        ledger = BatchLedger(
            self._batch_state_path(batch_cfg), volume_id=self.volume_id, model=self.client.model
        )
        settled = self._drain_open_batches(
            ledger, manager, {path.stem: path for path in pending}, written, unfinished,
            poll_seconds=poll_seconds, deadline_seconds=deadline_seconds,
        )
        if settled:
            pending = [path for path in pending if path.stem not in settled]
            if not pending:
                self._finish_batch_run(written, unfinished)
                return written

        # Seed the ledger when it is empty. Without at least one committed turn
        # the first wave's chapters are mutually blind, which is the very
        # condition this path exists to avoid.
        #
        # The seed only has to LAND before the next wave is built — it does not
        # have to run synchronously to do that. Submitting it as its own wave of
        # one preserves the invariant exactly (it commits before wave 2's prefix
        # is assembled) while paying the 50% batch rate instead of full price.
        # Measured on Vol.4: the synchronous seed was $2.06 of a $12.50 volume —
        # 16.5% of the bill for 1,503 words, purely because it took the sync
        # route. seed_via_batch buys that back for one extra batch round-trip of
        # latency; set it false for the old immediate-return behaviour.
        seed_count = max(0, int(batch_cfg.get("seed_chapters", 1) or 0))
        seed_via_batch = bool(batch_cfg.get("seed_via_batch", True))
        seed_wave: List[Path] = []
        if manager is not None and not manager.turns and seed_count:
            seed_wave = pending[:seed_count]
            pending = pending[seed_count:]
            if not seed_via_batch:
                for path in seed_wave:
                    logger.info("[ANTHROPIC-BATCH] seeding continuity synchronously: %s", path.stem)
                    written[path.stem] = self.translate_and_persist_chapter(path, {"chapter_id": path.stem})
                seed_wave = []

        # The seed leads as a wave of one so it commits before wave 2 is built.
        waves: List[List[Path]] = ([seed_wave] if seed_wave else []) + list(_chunk(pending, wave_size))
        # auto (default) pilots only the waves that actually race: the ones
        # built after a compaction. always/never force it on or off.
        raw_pilot = batch_cfg.get("cache_pilot", "auto")
        if isinstance(raw_pilot, str):
            pilot_mode = raw_pilot.strip().lower()
        else:
            pilot_mode = "always" if raw_pilot else "never"
        if pilot_mode not in ("auto", "always", "never"):
            logger.warning("[ANTHROPIC-BATCH] unknown cache_pilot=%r; using auto", raw_pilot)
            pilot_mode = "auto"
        compactions_seen = _compaction_count(manager)
        aborted = False

        for wave_number, wave in enumerate(waves, start=1):
            if aborted:
                break
            prompts: Dict[str, str] = {}
            requests: List[Dict[str, Any]] = []
            path_by_id: Dict[str, Path] = {}
            # ONE compaction check for the whole wave, taken before any
            # request is built. build_messages compacts on every call, so
            # without this a fold could land between two members of the same
            # wave: the members built before it would replay a history the
            # members built after it do not, which breaks both the shared
            # prefix the wave is billed against and the continuity it exists
            # to carry. Every request below is therefore built with
            # compact=False against this one frozen state.
            if manager is not None:
                manager.prepare_prefix()
            for path in wave:
                chapter_id = path.stem
                path_by_id[chapter_id] = path
                prompt = self._build_chapter_prompt(chapter_id, path)
                prompts[chapter_id] = prompt
                # Built from the ledger as it stands right now, and therefore
                # identical for every request in this wave. Nothing commits
                # until the wave lands, so the prefix cannot shift underneath
                # a job that is already in flight.
                messages = (
                    manager.build_messages(
                        prompt, cache_control=self._history_cache_control(), compact=False
                    )
                    if manager is not None
                    else [_user_message(prompt)]
                )
                requests.append({"custom_id": chapter_id, "params": self._batch_params(messages)})

            # Cache pilot. Every request in a wave carries the same history
            # breakpoint, so exactly one of them should WRITE the prefix and the
            # rest should read it. Submitted together they race for that write,
            # and the loser is billed too. Measured on Vol.4: CH12 and CH13 each
            # wrote 28,693 tokens, CH18 and CH19 each wrote 20,505 — 49,198
            # duplicate write tokens, 11% of all writes, $0.49 of a $12.50
            # volume.
            #
            # Both losers were the waves built after a compaction. A fold
            # rewrites the replayed prefix, so the next wave meets a cold and
            # unusually small cache entry — small enough that two requests both
            # begin writing before either finishes. Waves whose prefix merely
            # grew by the previous wave's turns did not race: one request wrote
            # and the rest read, every time. So "auto" pilots only after a fold,
            # which on that run is 2 waves out of 5 rather than all of them.
            #
            # Sending the wave's first request alone and letting it land makes
            # the write deterministic. The remainder was already built above,
            # against the pre-wave ledger, so its prefix stays byte-identical
            # and reads what the pilot wrote — wave members are mutually blind
            # either way, so nothing is lost but one batch round-trip.
            #
            # Read after the requests are built, not before. A fold can no
            # longer land mid-assembly - prepare_prefix takes that check once,
            # above - but it can still land in the previous wave's absorb, and
            # that is the case the pilot is for.
            compactions_now = _compaction_count(manager)
            prefix_reset = compactions_now != compactions_seen
            compactions_seen = compactions_now
            use_pilot = len(requests) > 1 and (
                pilot_mode == "always" or (pilot_mode == "auto" and prefix_reset)
            )
            groups: List[List[Dict[str, Any]]] = (
                [requests[:1], requests[1:]] if use_pilot else [requests]
            )
            for group_number, group in enumerate(groups, start=1):
                if not group:
                    continue
                group_ids = [str(req["custom_id"]) for req in group]
                group_paths = [path_by_id[cid] for cid in group_ids]
                role = "" if len(groups) == 1 else (" pilot" if group_number == 1 else " remainder")

                batch_id = submit_batch(self.client, group)
                # Recorded before the poll loop starts: the failure this guards
                # against is the process not surviving to write anything later.
                ledger.record_submitted(batch_id, wave=wave_number, chapter_ids=group_ids)
                logger.info(
                    "[ANTHROPIC-BATCH] wave %d%s submitted batch_id=%s chapters=%d history_turns=%d",
                    wave_number,
                    role,
                    batch_id,
                    len(group),
                    len(manager.turns) if manager is not None else 0,
                )
                if not self._await_batch(batch_id, poll_seconds=poll_seconds, deadline_seconds=deadline_seconds):
                    # Left OPEN in the ledger on purpose, so the next run
                    # collects it instead of paying for these chapters again. Do
                    # not submit the remainder or the following wave either:
                    # both would be built against a ledger that never received
                    # this one's turns.
                    logger.error(
                        "[ANTHROPIC-BATCH] wave %d%s batch_id=%s did not end within the completion window; "
                        "stopping. It stays open in %s and the next run will collect it.",
                        wave_number,
                        role,
                        batch_id,
                        ledger.path,
                    )
                    for path in wave:
                        if path.stem not in written:
                            unfinished[path.stem] = f"batch {batch_id} still open at deadline"
                    aborted = True
                    break

                ledger.record_ended(batch_id)
                outcome = retrieve_batch_results(self.client, batch_id, model=self.client.model)
                unfinished.update(outcome.unfinished)
                self._absorb_results(outcome, group_paths, prompts, manager, written, unfinished)

        self._finish_batch_run(written, unfinished)
        return written

    def _finish_batch_run(self, written: Dict[str, Path], unfinished: Dict[str, str]) -> None:
        if written:
            _update_manifest_after_translation(self.work_dir, set(written))
        if unfinished:
            logger.warning(
                "[ANTHROPIC-BATCH] %d chapter(s) left pending: %s",
                len(unfinished),
                "; ".join(f"{cid} ({reason})" for cid, reason in sorted(unfinished.items())),
            )

    def _batch_state_path(self, batch_cfg: Dict[str, Any]) -> Path:
        raw = Path(str(batch_cfg.get("persistence_file", ".context/anthropic_batch_state.json")))
        return raw if raw.is_absolute() else self.work_dir / raw

    def _drain_open_batches(
        self,
        ledger,
        manager,
        jp_by_id: Dict[str, Path],
        written: Dict[str, Path],
        unfinished: Dict[str, str],
        *,
        poll_seconds: int,
        deadline_seconds: float,
    ) -> set:
        """Collect jobs a previous run submitted but never retrieved.

        A batch is billed when it is accepted and its results stay retrievable
        for 29 days, so a job left open by a killed process is paid work
        waiting to be picked up. Resubmitting those chapters would buy them a
        second time.

        Returns the chapter ids that must NOT be re-submitted this run: every
        chapter that has now been collected, plus every chapter still inside a
        job that has not finished. Chapters whose results were *retryable*
        (expired, cancelled, a server-side error) are deliberately excluded —
        those are the ones a fresh wave should pick up.
        """
        from src.Anthropic.batch import retrieve_batch_results

        settled: set = set()
        for entry in ledger.open_batches():
            batch_id = str(entry.get("batch_id") or "")
            chapter_ids = [cid for cid in entry.get("chapter_ids") or [] if cid in jp_by_id]
            if not batch_id:
                continue
            if not chapter_ids:
                ledger.record_abandoned(batch_id, "no pending chapters remain for this job")
                logger.info("[ANTHROPIC-BATCH] open batch_id=%s covers nothing still pending; closed out", batch_id)
                continue

            logger.info(
                "[ANTHROPIC-BATCH] recovering open batch_id=%s (wave %s, %d chapter(s)) from %s",
                batch_id, entry.get("wave"), len(chapter_ids), ledger.path,
            )
            if not self._await_batch(batch_id, poll_seconds=poll_seconds, deadline_seconds=deadline_seconds):
                # Still running and still billing. Hold these chapters back
                # rather than paying for them twice; it stays open for later.
                for chapter_id in chapter_ids:
                    unfinished[chapter_id] = f"batch {batch_id} still open"
                settled.update(chapter_ids)
                logger.error("[ANTHROPIC-BATCH] batch_id=%s has not ended; its chapters are held back this run", batch_id)
                continue

            ledger.record_ended(batch_id)
            outcome = retrieve_batch_results(self.client, batch_id, model=self.client.model)
            unfinished.update(outcome.unfinished)
            paths = [jp_by_id[cid] for cid in chapter_ids]
            prompts = {path.stem: self._build_chapter_prompt(path.stem, path) for path in paths}
            self._absorb_results(outcome, paths, prompts, manager, written, unfinished)
            # Retryable items are the only ones a new wave should re-send.
            settled.update(cid for cid in chapter_ids if cid not in outcome.retryable)
        return settled

    def _absorb_results(
        self,
        outcome,
        wave: List[Path],
        prompts: Dict[str, str],
        manager,
        written: Dict[str, Path],
        unfinished: Dict[str, str],
    ) -> None:
        """Persist one job's succeeded results and commit them to the
        conversation ledger, in chapter order.

        Shared by the wave loop and the recovery path deliberately: the batch
        and synchronous paths drifted apart precisely because each grew its own
        copy of this logic.
        """
        for path in wave:
            chapter_id = path.stem
            response = outcome.succeeded.get(chapter_id)
            if response is None:
                continue  # already accounted for in outcome.unfinished

            if response.termination == LLMTermination.REFUSED:
                # Parity with translate_chapter: a refusal routes to the
                # DeepSeek fallback with decision inheritance, rather than
                # being logged and dropped as it was before.
                try:
                    text = self._safety_fallback_translate(
                        path,
                        chapter_id,
                        AnthropicRefusalError(f"Anthropic declined translation of {chapter_id}."),
                    )
                except Exception as exc:  # noqa: BLE001 - chapter stays pending, loudly
                    unfinished[chapter_id] = f"refused; safety fallback failed: {exc}"
                    logger.error("[ANTHROPIC-BATCH] %s refused and fallback failed: %s", chapter_id, exc)
                    continue
                written[chapter_id] = self._write_chapter(chapter_id, text)
                continue

            if response.termination == LLMTermination.MAX_OUTPUT:
                # The synchronous path answers this with up to
                # max_continuations follow-up calls; a batch job cannot
                # continue mid-flight, because every request was submitted
                # before any of them ran. Persisting the fragment would stamp
                # it completed in manifest.json, and
                # _filter_completed_chapters would never offer it again — a
                # silently truncated chapter, permanently. So nothing is
                # written and it stays pending.
                unfinished[chapter_id] = "max_output — truncated, not persisted"
                logger.error(
                    "[ANTHROPIC-BATCH] %s hit the output ceiling; NOT persisted. "
                    "Re-run it on the synchronous path, which can continue.",
                    chapter_id,
                )
                continue

            text, leaked_blocks = split_thinking_from_output(response.content)
            text = _CJK_LEAK_RE.sub("", text)
            if not text.strip():
                unfinished[chapter_id] = "no visible translation text"
                logger.error("[ANTHROPIC-BATCH] %s returned no visible text; NOT persisted", chapter_id)
                continue

            self._maybe_write_thinking_log(
                chapter_id=chapter_id,
                api_thinking=response.thinking_content,
                leaked_blocks=leaked_blocks,
            )
            self._log_usage(chapter_id, response)
            output_path = self._write_chapter(chapter_id, text)
            written[chapter_id] = output_path
            if manager is not None:
                # The batch decoder routes through the same
                # response_to_llm_response the synchronous path uses, so
                # provider_metadata["raw_content"] — and with it the
                # byte-exact thinking blocks the ledger replays — is present
                # here exactly as it is there.
                manager.commit(
                    chapter_id=chapter_id,
                    user_message=_user_message(prompts[chapter_id]),
                    assistant_message=_assistant_message(response),
                    chapter_text=text,
                    output_path=output_path,
                )

    def _await_batch(self, batch_id: str, *, poll_seconds: int, deadline_seconds: float) -> bool:
        """Poll until the job ends; return False if the completion window
        elapses first.

        A bare ``while True`` was tolerable when one job was the entire run.
        With waves running in sequence, a job that never ends blocks every
        wave behind it, so the loop takes a ceiling — from
        batch.completion_window, which the config has always declared and
        nothing has ever read.
        """
        from src.Anthropic.batch import poll_batch

        started = time.monotonic()
        while True:
            status = poll_batch(self.client, batch_id)
            if status.get("processing_status") == "ended":
                return True
            if time.monotonic() - started >= deadline_seconds:
                return False
            time.sleep(poll_seconds)

    def _build_chapter_prompt(self, chapter_id: str, chapter_path: Path) -> str:
        """The per-chapter user envelope, identical in shape to the one
        translate_chapter builds.

        previous_guidance_text is deliberately not threaded through here: it is
        a strictly sequential dedup, and within a wave every request is built
        against the same frozen history, so there is no single "previous"
        chapter to point at. The envelope sits after the cache breakpoint in
        any case, so repeating the guidance costs the same either way.
        """
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = derive_chapter_eps_band(signals)
        active_characters = [
            {"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})}
            for signal in signals
            if signal.get("name")
        ]
        guidance = build_chapter_guidance(
            eps_band, active_characters, get_anthropic_optimization_config(), self._volume_type
        )
        return build_chapter_message(
            chapter_id,
            jp_source,
            guidance,
            continuity=self._continuity_brief(chapter_id),
            anchor_rows=self._anchor_rows(chapter_id, jp_source),
        )

    def _write_chapter(self, chapter_id: str, text: str) -> Path:
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        self._reconcile_anchors(chapter_id, text)
        return output_path

    def _history_cache_control(self) -> Optional[Dict[str, Any]]:
        """The breakpoint closing the replayed history, or None when caching is off."""
        caching_cfg = get_anthropic_caching_config()
        return build_cache_control(
            enabled=bool(caching_cfg.get("enabled", True)),
            ttl=str(caching_cfg.get("ttl", "5m") or "5m"),
        )

    def _system_blocks(self) -> List[Dict[str, Any]]:
        """Cached system blocks, shared by the sync and batch paths so a batch
        run is not silently the only route paying full price for its prefix."""
        caching_cfg = get_anthropic_caching_config()
        return build_system_blocks(
            self.system_segments,
            enabled=bool(caching_cfg.get("enabled", True)),
            ttl=str(caching_cfg.get("ttl", "5m") or "5m"),
        )

    def _batch_params(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """One batch request's params, taking a full ``messages`` array.

        It used to take a bare prompt string and wrap it as a single user
        turn, which is what made the batch path structurally incapable of
        carrying any conversation history at all. The array now arrives built
        — from the conversation ledger in the normal case — and the cached
        system blocks are shared with the synchronous path so a batch run is
        not the only route paying full price for its own prefix.
        """
        cfg = self._config
        generation_cfg = cfg.get("generation", {}) or {}
        thinking_cfg = cfg.get("thinking", {}) or {}
        params: Dict[str, Any] = {
            "model": self.client.model,
            "max_tokens": int(generation_cfg.get("max_output_tokens", 128_000) or 128_000),
            "system": self._system_blocks(),
            "messages": messages,
        }
        if bool(thinking_cfg.get("enabled", True)) or self.client.model in THINKING_ALWAYS_ON_MODELS:
            params["thinking"] = {
                "type": "adaptive",
                "display": str(thinking_cfg.get("display", "summarized") or "summarized"),
            }
            params["output_config"] = {"effort": str(thinking_cfg.get("effort", "high") or "high")}
        return params


def _completion_window_seconds(raw: Any, default_seconds: float = 24 * 3600.0) -> float:
    """Parse batch.completion_window ("24h", "90m", "3600s", or a bare number
    of seconds) into seconds. Falls back to Anthropic's own 24-hour maximum
    rather than to no ceiling at all."""
    text = str(raw or "").strip().lower()
    if not text:
        return default_seconds
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    multiplier = units.get(text[-1], 1.0)
    number = text[:-1] if text[-1] in units else text
    try:
        value = float(number)
    except ValueError:
        logger.warning("[ANTHROPIC-BATCH] unparseable completion_window %r; using %.0fs", raw, default_seconds)
        return default_seconds
    return value * multiplier if value > 0 else default_seconds


def _compaction_count(manager) -> int:
    """How many times the conversation ledger has been folded.

    Each fold rewrites everything ahead of the retained turns, so the wave built
    after one faces a cold, small cache entry — the condition under which a
    wave's requests race to write it. Reads the ledger's own durable record
    rather than inferring from turn counts, which reset on every fold.
    """
    if manager is None:
        return 0
    state = getattr(manager, "state", None) or {}
    return len(state.get("compaction_events") or [])


def _chunk(items: List[Path], size: int) -> Iterator[List[Path]]:
    """Split into consecutive waves of at most ``size``, order preserved."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


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
    translator = AnthropicTranslator(work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run)
    batch_cfg = translator._config.get("batch", {}) or {}
    if bool(batch_cfg.get("enabled", False)) and not dry_run:
        return translator.translate_volume_batch(chapter_files)
    return translator.translate_all(chapter_files)


def _user_message(text: str) -> Dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}



def _assistant_message(response) -> Dict[str, Any]:
    raw_content = response.provider_metadata.get("raw_content") or []
    content = [sanitize_replayable_block(dict(item)) for item in raw_content if isinstance(item, dict)]
    if not content and response.content:
        content = [{"type": "text", "text": response.content}]
    return {"role": "assistant", "content": content}


def _single_turn_messages(prompt: str) -> List[Dict[str, Any]]:
    return [_user_message(prompt)]
