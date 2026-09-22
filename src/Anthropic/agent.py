"""Anthropic Phase 2 translator facade with the established filesystem contract."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.Anthropic.advisor_prompts import ESCALATION_BLOCK
from src.Anthropic.client import AnthropicClient
from src.Anthropic.config import (
    get_anthropic_advisor_config,
    get_anthropic_batch_config,
    get_anthropic_config,
    get_anthropic_fidelity_config,
    get_anthropic_telemetry_config,
    get_anthropic_continuation_config,
    get_anthropic_conversation_config,
    get_anthropic_optimization_config,
    get_anthropic_caching_config,
    get_anthropic_prompt_path,
    get_anthropic_websearch_config,
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
from src.Anthropic.optimization import build_advisor_guidance, build_chapter_guidance
from src.Anthropic.prompt_loader import (
    build_chapter_message,
    build_continuation_message,
    build_system_segments,
)
from src.Anthropic.client import (
    THINKING_ALWAYS_ON_MODELS,
    build_advisor_tool,
    build_cache_control,
    build_system_blocks,
    normalize_model_id,
)
from src.Anthropic.response import sanitize_replayable_block
from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.chapter_signals import (
    HIGH_RISK_SIGNALS,
    build_chapter_signal_guidance,
    parse_chapter_signals,
)
from src.Deepseek.common.config import PIPELINE_ROOT, WORK_DIR, get_safety_fallback_config
from src.Deepseek.common.illustration_reinjection import reinject
from src.Deepseek.common.verbatim_anchors import (
    anchors_in_source,
    merge_bible_anchors,
    observe_prior_usage,
    parse_anchors,
    reconcile_chapter,
)
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.structural_fidelity import (
    DEFAULT_RATIO_TOLERANCE,
    length_ratio,
    structural_report,
)
from src.Deepseek.common.safety_fallback import fallback_translate_chapter
from src.Deepseek.common.token_telemetry import cost_breakdown_usd, log_call
from src.Deepseek.translator.config import get_thinking_log_config
from src.Deepseek.translator.thinking_output import merge_thinking_log, sanitize_chapter_output

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
        self._advisor_cfg = get_anthropic_advisor_config()
        self._websearch_cfg = get_anthropic_websearch_config()
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
        # Layer 1 escalation text (proofreading advisor) — appended as its
        # own cache-breakpointed segment, only when enabled, so toggling
        # advisor.enabled invalidates just this one segment's cache entry
        # rather than the whole system prefix. Placed last: after the static
        # craft policy and the volume's project context.
        if bool(self._advisor_cfg.get("enabled", False)):
            self.system_segments.append(ESCALATION_BLOCK)
        self.system_instruction = "\n\n".join(self.system_segments)
        # Tell the ledger which system prefix these turns belong to. On a
        # prefix-bound model (Fable 5.1) the system prompt is part of every
        # thinking block's signature, and context.xml is rewritten in place by
        # the DeepSeek safety fallback — so a resumed run can present a
        # different prefix than the ledger on disk was written under. Binding
        # here lets the manager strip the now-unreplayable blocks once,
        # instead of the volume failing on its next call. A no-op on
        # claude-opus-5 / claude-sonnet-5, which do not bind the prefix.
        #
        # tool_fingerprint_extra folds the active advisor/web_search config
        # into the same check: Anthropic's advisor docs state the tool set is
        # also part of what binds a Fable-5.1 thinking block's signature, so
        # toggling advisor.enabled between runs needs the identical recovery a
        # changed system prompt already gets here.
        if self.client.conversation_manager is not None:
            self.client.conversation_manager.bind_system(
                self.system_segments, tool_fingerprint_extra=self._tool_fingerprint()
            )
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

    def _tool_fingerprint(self) -> str:
        """A short deterministic string capturing the active advisor/web_search
        config, folded into the conversation ledger's system fingerprint (see
        conversation.py::bind_system) so a tool-set change on a prefix-bound
        model gets the same "strip thinking, don't fail" recovery a changed
        system prompt already gets."""
        return "|".join([
            f"advisor={bool(self._advisor_cfg.get('enabled', False))}",
            f"advisor_model={self._advisor_cfg.get('model', '')}",
            f"web_search={bool(self._websearch_cfg.get('enabled', False))}",
        ])

    def _tools_qualify(self, chapter_id: str, eps_band: str) -> bool:
        """Per-chapter economic gate (spec §2.1-2.2): a chapter qualifies for
        the advisor/web_search tools only when it carries an actual risk
        signal -- EPS band WARM/HOT alone is not sufficient. Chapter 7 of
        d77bf8 is this session's own counter-example: the volume's own tracked
        peak, correctly handled with no tool at all (Run 6 vs. Run 8 showed no
        measurable delta on that chapter).

        subculture_reference qualifies unconditionally once prep populates it
        (prep-side detection work is out of scope here, tracked separately in
        the plan's §6.1 -- this branch is forward-compatible, not yet
        reachable, since no signal category by that name exists in
        chapter_signals.py today). Every other HIGH_RISK_SIGNALS member
        requires EPS WARM/HOT alongside it -- AND logic, not OR.
        """
        record = self._chapter_signals.get(chapter_id) or {}
        signals = record.get("signals") or []
        names = {str(s.get("name") or "") for s in signals if isinstance(s, dict)}
        if "subculture_reference" in names:
            return True
        band = str(eps_band or "").upper()
        return band in ("WARM", "HOT") and bool(names & HIGH_RISK_SIGNALS)

    def _resolve_pauses(
        self,
        response,
        current_messages: List[Dict[str, Any]],
        assistant_message: Dict[str, Any],
        *,
        chapter_id: str,
        advisor_enable: bool,
        websearch_enable: bool,
    ):
        """Resend the unchanged assistant message for every PAUSED result,
        synchronously, until the turn actually finishes or the pause budget
        is exhausted. Shared by the sync path (translate_chapter) and the
        batch path (_absorb_results) -- a paused batch result is resolved via
        one sync follow-up call per pause, not a second batch job, since
        Anthropic's own batch docs note the batch worker already runs more
        loop iterations before pausing than the synchronous path does, making
        a batch-path pause an expected rarity rather than the common case.

        advisor_enable/websearch_enable must match whatever the ORIGINAL call
        used: Anthropic's docs are explicit that omitting the advisor tool
        from a resume request with a pending server_tool_use block is a 400 —
        the tool must be present on every resend while a call is pending.

        CONTENT DOES NOT REPEAT ACROSS A PAUSE/RESUME BOUNDARY. Anthropic's
        server-tools docs are explicit: "the server_tool_use block is not
        repeated in the second one" — a resumed response contains only newly
        generated content, not a copy of what the paused response already
        held. A PAUSED response can carry real chapter prose the executor
        wrote before pausing to consult a tool (e.g. "Here's the opening..."
        before it stops to check a reference) — discarding it and keeping
        only the final resumed response's content would silently drop that
        prose. ``resumed_responses`` therefore returns every response object
        this method itself produced, in order, so the caller can fold each
        one's ``.content``/``.thinking_content`` into its own accumulation
        (the caller already has the response passed in as an argument, which
        is why that one is not repeated here either).

        Returns (response, current_messages, assistant_message, pause_count,
        resumed_responses). ``pause_count`` tells the caller whether this
        call's usage was already logged in here (pause_count > 0, under an
        #advisor-resume-N label) or still needs the caller's own _log_usage
        call (pause_count == 0, the common case where the response never
        paused at all) -- logging both would double-count the same response's
        spend under two labels.
        """
        pause_budget = max(0, int(self._advisor_cfg.get("max_pause_resumes", 2) or 2))
        pause_count = 0
        resumed_responses: List[Any] = []
        while response.termination == LLMTermination.PAUSED and pause_count < pause_budget:
            pause_count += 1
            current_messages = [*current_messages, assistant_message]
            response = self.client.generate(
                prompt="",
                system_instruction=self.system_segments,
                messages=current_messages,
                advisor_enable=advisor_enable,
                websearch_enable=websearch_enable,
            )
            resumed_responses.append(response)
            assistant_message = _assistant_message(response)
            self._log_usage(f"{chapter_id}#advisor-resume-{pause_count}", response)
        if response.termination == LLMTermination.PAUSED:
            logger.warning(
                "[ANTHROPIC] %s: still PAUSED after %d resume attempt(s) (max_pause_resumes=%d); "
                "giving up on this call rather than looping indefinitely",
                chapter_id, pause_count, pause_budget,
            )
        return response, current_messages, assistant_message, pause_count, resumed_responses

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

    def _peer_length_ratios(self, chapter_id: str) -> List[float]:
        """JP-chars-per-EN-word for every OTHER chapter already on disk.

        Read from EN/ rather than accumulated in memory so a resumed run, a
        recovered batch, and a straight sequential run all compute the same
        baseline from the same evidence.
        """
        ratios: List[float] = []
        en_dir = self.work_dir / "EN"
        if not en_dir.is_dir():
            return ratios
        for en_path in sorted(en_dir.glob("*_EN.md")):
            peer_id = en_path.name[: -len("_EN.md")]
            if peer_id == chapter_id:
                continue
            try:
                ratio = length_ratio(
                    (self.work_dir / "JP" / f"{peer_id}.md").read_text(encoding="utf-8"),
                    en_path.read_text(encoding="utf-8"),
                )
            except OSError:
                continue
            if ratio:
                ratios.append(ratio)
        return ratios

    def _reinject_illustrations(self, chapter_id: str, en_text: str) -> str:
        """Restore illustration tags the model dropped, before the gate counts them.

        Nothing in this route instructs the model to carry `![illustration](...)`
        tags through -- `<formatting>` now states the rule, but a stated rule is not
        a guarantee, and this failure is silent when it happens (volume 646941 lost
        5 of 10 plates; 6e63bc lost 2). Running here, between generation and
        `_check_chapter_fidelity`, turns a blocking `illustration_parity` mismatch
        into a self-healing one -- while leaving the gate fully able to fail loudly
        on anything re-injection refused to place.

        Placement is proven, never approximated: see
        docs/illustration-reinjection-spec.md. A refusal returns the text unchanged
        rather than guessing, and the gate then blocks the chapter as before.
        """
        try:
            jp_source = (self.work_dir / "JP" / f"{chapter_id}.md").read_text(encoding="utf-8")
        except OSError:
            return en_text  # _check_chapter_fidelity reports the unreadable source

        assets_dir = self.work_dir / "assets" / "illustrations"
        assets = {p.name for p in assets_dir.iterdir()} if assets_dir.is_dir() else None

        repaired, plan = reinject(jp_source, en_text, available_assets=assets)
        if plan.nothing_to_do:
            return en_text
        if plan.ok:
            logger.warning(
                "[ANTHROPIC-ILLUSTRATION] %s: model dropped %d plate(s); re-injected at "
                "aligned positions (ratio %.3f): %s",
                chapter_id,
                len(plan.plates),
                plan.ratio,
                ", ".join(p.target for p in plan.plates),
            )
            return repaired
        logger.error(
            "[ANTHROPIC-ILLUSTRATION] %s: plate(s) dropped and re-injection REFUSED "
            "(ratio %.3f) -- placement could not be proven, so nothing was written: %s",
            chapter_id,
            plan.ratio,
            "; ".join(plan.refusals),
        )
        return en_text

    def _check_chapter_fidelity(self, chapter_id: str, en_text: str) -> List[str]:
        """Verify a finished chapter against its source. Returns blocking reasons.

        Two halves, and the second exists because the first cannot see it.

        The ANCHOR half records whether the locks the source hit survived into
        the English. context.xml's anchors already carry an
        <en_output status="pending"> slot for exactly this, and on a completed
        21-chapter Vol.4 run every one of the fifteen was still "pending" --
        the schema anticipated the check and nothing performed it.

        The STRUCTURAL half counts what no anchor covers: illustration plates
        and translated bulk. Volume 6e63bc's CHAPTER_04 honoured every anchor
        it carried and still shipped without both of its source's plates
        (p085.jpg, p121.jpg) plus a dropped dialogue exchange. Nothing noticed,
        because nothing was counting. The proofreading advisor could not have
        noticed either: it is consulted before drafting and never sees the
        finished prose.

        Neither half is written back into context.xml. That document is the
        cached system prefix, and on a prefix-bound model rewriting it
        mid-volume invalidates the prompt cache and every stored thinking
        block (see AnthropicConversationManager.bind_system). A later QC or
        bible pass can fold this artifact home when the run is over.

        An empty return means the chapter passed and may be stamped completed.
        """
        cfg = get_anthropic_fidelity_config()
        gate_enabled = bool(cfg.get("enabled", True))
        block_on = {
            str(item).strip().lower()
            for item in (cfg.get("block_on") or ["missing", "drifted", "structural"])
        }
        try:
            tolerance = float(cfg.get("ratio_tolerance", DEFAULT_RATIO_TOLERANCE) or DEFAULT_RATIO_TOLERANCE)
        except (TypeError, ValueError):
            tolerance = DEFAULT_RATIO_TOLERANCE

        try:
            jp_source = (self.work_dir / "JP" / f"{chapter_id}.md").read_text(encoding="utf-8")
        except OSError:
            logger.warning(
                "[ANTHROPIC-FIDELITY] %s: JP source unreadable; completeness NOT verified", chapter_id
            )
            return []

        anchor_rows = (
            reconcile_chapter(self._anchors, chapter_id=chapter_id, jp_source=jp_source, en_text=en_text)
            if self._anchors
            else []
        )
        structural_rows = structural_report(
            jp_source, en_text, self._peer_length_ratios(chapter_id), tolerance=tolerance
        )

        path = self.work_dir / ".context" / "anchor_reconciliation.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing.setdefault("volume_id", self.volume_id)
        existing.setdefault("chapters", {})
        # Schema bump: the per-chapter value was a bare list of anchor rows and
        # is now a mapping, so the structural rows have somewhere to live
        # beside them. Readers of the old shape should branch on isinstance.
        existing["chapters"][chapter_id] = {"anchors": anchor_rows, "structural": structural_rows}
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, existing)

        blocking: List[str] = []
        anchor_flagged = [row for row in anchor_rows if str(row.get("status")) in ("missing", "drifted")]
        if anchor_flagged:
            logger.warning(
                "[ANTHROPIC-FIDELITY] %s: %d anchor(s) not honoured: %s",
                chapter_id,
                len(anchor_flagged),
                ", ".join(f"{row['anchor_id']}({row['status']})" for row in anchor_flagged),
            )
        blocking.extend(
            f"anchor {row.get('anchor_id')} {row.get('status')} ({row.get('en')!r})"
            for row in anchor_flagged
            if str(row.get("status")) in block_on
        )

        for row in structural_rows:
            status = str(row.get("status") or "")
            if row.get("check") == "illustration_parity" and status == "mismatch":
                detail = (
                    f"illustration parity: source has {row.get('jp_count')}, output has "
                    f"{row.get('en_count')}; missing={row.get('missing')} unexpected={row.get('unexpected')}"
                )
                logger.error("[ANTHROPIC-FIDELITY] %s: %s", chapter_id, detail)
                if "structural" in block_on:
                    blocking.append(detail)
            elif row.get("check") == "length_ratio" and status == "outlier":
                # Advisory only, never blocking: translation density genuinely
                # varies with a chapter's dialogue-to-narration mix, and a
                # two-line omission cannot move this ratio at all (measured on
                # 6e63bc CH04: 7.7% deviation while missing real content). It
                # earns its keep by pointing a reviewer at the right chapter
                # out of twenty, not by deciding anything on its own.
                logger.warning(
                    "[ANTHROPIC-FIDELITY] %s: translated bulk is an outlier for this volume "
                    "(ratio=%s, median=%s, deviation=%s) — worth a look, not blocking",
                    chapter_id, row.get("ratio"), row.get("median"), row.get("deviation"),
                )

        if blocking and not gate_enabled:
            logger.warning(
                "[ANTHROPIC-FIDELITY] %s: %d fidelity problem(s) found but the gate is disabled; "
                "chapter will be stamped completed anyway: %s",
                chapter_id, len(blocking), "; ".join(blocking),
            )
            return []
        return blocking

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
        # Layer 2 reinforcement (spec §5): a soft nudge on top of Layer 1's
        # escalation block, not a separate trigger -- "" on a chapter with
        # nothing to reinforce.
        advisor_nudge = (
            build_advisor_guidance(
                eps_band,
                self._chapter_signals.get(chapter_id),
                self._preceding_chapter_id(chapter_id),
            )
            if self._advisor_cfg.get("enabled", False)
            else ""
        )
        if advisor_nudge:
            guidance = "\n\n".join(part for part in (guidance, advisor_nudge) if part)
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
        # Economic gate (spec §2.1-2.2): a chapter without an actual risk
        # signal gets the tool(s) withheld for THIS call even when config
        # enables them globally, unless require_signal is explicitly false.
        qualifies = self._tools_qualify(chapter_id, eps_band)
        advisor_enable = qualifies if bool(self._advisor_cfg.get("require_signal", True)) else True
        websearch_enable = qualifies if bool(self._websearch_cfg.get("require_signal", True)) else True
        response = self.client.generate(
            prompt=prompt,
            system_instruction=self.system_segments,
            messages=messages,
            dry_run=self.dry_run,
            advisor_enable=advisor_enable,
            websearch_enable=websearch_enable,
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
        chapter_user_message = _user_message(prompt)
        current_messages = messages or _single_turn_messages(prompt)
        assistant_message = _assistant_message(response)
        first_response = response  # _resolve_pauses reassigns `response`; keep the original for content accumulation
        # A pending advisor/web_search call returns stop_reason: "pause_turn"
        # -- resolved here, before the REFUSED/MAX_OUTPUT checks below, by
        # resending the unchanged assistant message. Not an advisor-only
        # concern: Anthropic's docs confirm a long-running web_search turn can
        # independently pause the same way.
        response, current_messages, assistant_message, pause_count, resumed = self._resolve_pauses(
            response, current_messages, assistant_message,
            chapter_id=chapter_id, advisor_enable=advisor_enable, websearch_enable=websearch_enable,
        )
        if response.termination == LLMTermination.REFUSED:
            return self._safety_fallback_translate(
                chapter_path, chapter_id,
                AnthropicRefusalError(f"Anthropic declined translation of {chapter_id}."),
            )
        if pause_count == 0:
            # Only logged here when nothing paused -- a paused call's final
            # response was already logged inside _resolve_pauses under its own
            # #advisor-resume-N label, and logging it again here would count
            # the same spend twice.
            self._log_usage(chapter_id, response)

        # Content does NOT repeat across a pause/resume boundary (Anthropic's
        # server-tools docs: "the server_tool_use block is not repeated in
        # the second one") -- the original response can carry real prose
        # written before the pause, so every response in the chain is folded
        # in, in order, not just the last.
        parts = [first_response.content] + [r.content for r in resumed]
        thinking_parts: List[str] = []
        advisor_texts: List[str] = []
        if first_response.thinking_content:
            thinking_parts.append(str(first_response.thinking_content))
        advisor_texts.extend(_advisor_consult_texts(first_response))
        for r in resumed:
            if r.thinking_content:
                thinking_parts.append(str(r.thinking_content))
            advisor_texts.extend(_advisor_consult_texts(r))
        continuation_count = 0
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
                advisor_enable=advisor_enable,
                websearch_enable=websearch_enable,
            )
            assistant_message = _assistant_message(response)
            continuation_response = response
            continuation_label = f"{chapter_id}#continue-{continuation_count}"
            response, current_messages, assistant_message, pause_count, resumed = self._resolve_pauses(
                response, current_messages, assistant_message,
                chapter_id=continuation_label, advisor_enable=advisor_enable, websearch_enable=websearch_enable,
            )
            if response.termination == LLMTermination.REFUSED:
                return self._safety_fallback_translate(
                    chapter_path, chapter_id,
                    AnthropicRefusalError(f"Anthropic declined continuation of {chapter_id}."),
                )
            parts.append(continuation_response.content)
            parts.extend(r.content for r in resumed)
            if continuation_response.thinking_content:
                thinking_parts.append(str(continuation_response.thinking_content))
            advisor_texts.extend(_advisor_consult_texts(continuation_response))
            for r in resumed:
                if r.thinking_content:
                    thinking_parts.append(str(r.thinking_content))
                advisor_texts.extend(_advisor_consult_texts(r))
            if pause_count == 0:
                self._log_usage(continuation_label, response)

        raw_text = "\n".join(part for part in parts if part)
        text, leaked_blocks = sanitize_chapter_output(raw_text)
        text = _CJK_LEAK_RE.sub("", text)
        if not text.strip():
            raise AnthropicAPIError(f"Anthropic returned no visible translation text for {chapter_id}.")
        self._maybe_write_thinking_log(
            chapter_id=chapter_id,
            api_thinking="\n\n".join(thinking_parts) or None,
            leaked_blocks=leaked_blocks,
            advisor_texts=advisor_texts,
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

        Also logs one additional row per Proofreading Mode advisor consult
        this response carried (``provider_metadata["advisor_usage"]``, built
        in response.py from ``usage.iterations``'s advisor_message entries) --
        billed at the ADVISOR's own model rate, never folded into the
        executor's row. Before this existed, an advisor consult's real spend
        was invisible: paid for, but attributed nowhere.
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
            for i, advisor_call in enumerate(response.provider_metadata.get("advisor_usage") or [], start=1):
                advisor_model = str(advisor_call.get("model") or "")
                if not advisor_model:
                    continue
                advisor_cache_hit = int(advisor_call.get("cache_read_tokens") or 0)
                advisor_cache_write = int(advisor_call.get("cache_creation_tokens") or 0)
                advisor_fresh = int(advisor_call.get("input_tokens") or 0)
                advisor_output = int(advisor_call.get("output_tokens") or 0)
                advisor_costs = cost_breakdown_usd(
                    model_name=advisor_model,
                    input_tokens=advisor_fresh,
                    output_tokens=advisor_output,
                    cache_read_tokens=advisor_cache_hit,
                    cache_creation_tokens=advisor_cache_write,
                    cache_read_included_in_input=False,
                    cache_ttl=self.client.cache_ttl,
                    batch=bool(response.batch_pricing),
                )
                log_call(
                    phase="translator",
                    volume_id=self.volume_id,
                    call_label=f"{call_label}#advisor-{i}",
                    model=advisor_model,
                    provider="anthropic",
                    cache_hit_tokens=advisor_cache_hit,
                    cache_write_tokens=advisor_cache_write,
                    fresh_tokens=advisor_fresh,
                    output_tokens=advisor_output,
                    cost_usd=float(advisor_costs["total_cost_usd"]),
                    batch=bool(response.batch_pricing),
                )
        except Exception as exc:  # telemetry must never prevent a translation
            logger.warning("[ANTHROPIC] token log failed for %s: %s", call_label, exc)

    def _maybe_write_thinking_log(
        self,
        *,
        chapter_id: str,
        api_thinking: Optional[str],
        leaked_blocks: List[str],
        advisor_texts: Optional[List[str]] = None,
    ) -> None:
        """Archive this chapter's thinking text to THINKING/<chapter_id>_THINKING.md.

        Text is only present when translation.anthropic.thinking.display is
        "summarized" — with "omitted" the API returns an empty `thinking`
        field by design (faster time-to-first-text-token, identical billing
        either way), and this correctly writes nothing rather than a
        near-empty file. Reasoning tokens are already paid for regardless of
        this flag; it only controls whether any returned text is archived.

        ``advisor_texts`` is the Proofreading Mode advisor's own plaintext
        consult(s) for this chapter, if any — populated only when
        ``advisor.model`` returns the plaintext advisor_result variant
        (claude-opus-4-8, claude-sonnet-5); silently empty for an encrypted
        advisor (claude-fable-5-1, claude-opus-5, and the Mythos family),
        whose guidance never reaches this file or any other, by Anthropic's
        own design (see docs/anthropic-advisor-mode-plan.md).
        """
        if not self.thinking_log_enabled:
            return
        merged = merge_thinking_log(api_thinking, leaked_blocks, chapter_id=chapter_id)
        if advisor_texts:
            advisor_section = (
                "## Proofreading Advisor Consult (plaintext advisor_result)\n\n"
                + "\n\n---\n\n".join(advisor_texts)
            )
            merged = f"{merged}\n\n{advisor_section}" if merged else advisor_section
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
        text = self._reinject_illustrations(chapter_id, text)
        output_path.write_text(text, encoding="utf-8")
        blocking = self._check_chapter_fidelity(chapter_id, text)
        if blocking:
            # The text stays on disk so it can be inspected and diffed, but the
            # chapter is NOT stamped completed -- _filter_completed_chapters
            # will offer it again on the next run. Same discipline the batch
            # path already applies to a MAX_OUTPUT fragment: a chapter that
            # lost content quietly deserves the treatment one that lost it
            # loudly already gets.
            logger.error(
                "[ANTHROPIC-FIDELITY] %s: %d fidelity problem(s); written to %s but NOT marked "
                "completed, so it will be re-translated on the next run: %s",
                chapter_id, len(blocking), output_path, "; ".join(blocking),
            )
            _update_manifest_after_translation(
                self.work_dir, set(), {chapter_id: "; ".join(blocking)}
            )
            return output_path
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
            # Captured so a PAUSED result can be resumed with the EXACT
            # messages array its request was submitted with -- rebuilding it
            # later via manager.build_messages would be wrong for any wave
            # member after the first, since earlier siblings commit to the
            # ledger as _absorb_results iterates, and a rebuild at that point
            # would wrongly include turns the original request never had.
            messages_by_id: Dict[str, List[Dict[str, Any]]] = {}
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
                eps_band = derive_chapter_eps_band(self._eps_signals.get(chapter_id, []))
                messages_by_id[chapter_id] = messages
                requests.append({
                    "custom_id": chapter_id,
                    "params": self._batch_params(messages, chapter_id=chapter_id, eps_band=eps_band),
                })

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

                # The advisor beta header is a batch-level parameter, not a
                # per-request one -- populated whenever advisor is enabled at
                # all, regardless of whether every request in THIS group
                # actually wired the tool in (an unused beta header on a
                # request that carries no advisor tool is inert).
                batch_betas = ["advisor-tool-2026-03-01"] if bool(self._advisor_cfg.get("enabled", False)) else None
                batch_id = submit_batch(self.client, group, betas=batch_betas)
                # Recorded before the poll loop starts: the failure this guards
                # against is the process not surviving to write anything later.
                ledger.record_submitted(batch_id, wave=wave_number, chapter_ids=group_ids, betas=batch_betas)
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
                outcome = retrieve_batch_results(self.client, batch_id, model=self.client.model, betas=batch_betas)
                unfinished.update(outcome.unfinished)
                self._absorb_results(
                    outcome, group_paths, prompts, manager, written, unfinished,
                    messages_by_id=messages_by_id,
                )

        self._finish_batch_run(written, unfinished)
        return written

    def _finish_batch_run(self, written: Dict[str, Path], unfinished: Dict[str, str]) -> None:
        if written or unfinished:
            _update_manifest_after_translation(self.work_dir, set(written), unfinished)
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
            outcome = retrieve_batch_results(
                self.client, batch_id, model=self.client.model, betas=entry.get("betas") or None
            )
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
        *,
        messages_by_id: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        """Persist one job's succeeded results and commit them to the
        conversation ledger, in chapter order.

        Shared by the wave loop and the recovery path deliberately: the batch
        and synchronous paths drifted apart precisely because each grew its own
        copy of this logic.

        ``messages_by_id``, when present (the live wave path only), is the
        EXACT ``messages`` array each chapter's request was submitted with --
        needed to resume a PAUSED result correctly. The recovery path
        (_drain_open_batches, after a killed/restarted process) has no such
        record; a PAUSED result there is left unfinished rather than resumed
        with a reconstructed-and-possibly-wrong history.
        """
        for path in wave:
            chapter_id = path.stem
            response = outcome.succeeded.get(chapter_id)
            if response is None:
                continue  # already accounted for in outcome.unfinished

            pause_count = 0
            extra_content_parts: List[str] = []
            extra_thinking_parts: List[str] = []
            extra_advisor_texts: List[str] = []
            if response.termination == LLMTermination.PAUSED:
                if messages_by_id is None or chapter_id not in messages_by_id:
                    # Recovery path: no record of the exact request this
                    # result came from. Anthropic's batch worker already runs
                    # more loop iterations before pausing than the synchronous
                    # path does, so this is expected to be rare -- but resuming
                    # with a reconstructed history risks sending a mismatched
                    # prefix, which is worse than leaving it pending for a
                    # manual re-run.
                    unfinished[chapter_id] = "paused (pause_turn) — no live request record to resume from; re-run"
                    logger.error(
                        "[ANTHROPIC-BATCH] %s: PAUSED result recovered without its original request "
                        "context; NOT resumed. Re-run it (synchronous or batch).",
                        chapter_id,
                    )
                    continue
                eps_band = derive_chapter_eps_band(self._eps_signals.get(chapter_id, []))
                qualifies = self._tools_qualify(chapter_id, eps_band)
                advisor_enable = qualifies if bool(self._advisor_cfg.get("require_signal", True)) else True
                websearch_enable = qualifies if bool(self._websearch_cfg.get("require_signal", True)) else True
                assistant_message = _assistant_message(response)
                # Content does NOT repeat across a pause/resume boundary
                # (Anthropic's server-tools docs: "the server_tool_use block
                # is not repeated in the second one") -- the paused response
                # can carry real chapter prose written before the pause, so
                # it is captured here before _resolve_pauses reassigns
                # `response` to the final one.
                paused_response = response
                response, _messages, _assistant, pause_count, resumed = self._resolve_pauses(
                    response, messages_by_id[chapter_id], assistant_message,
                    chapter_id=chapter_id, advisor_enable=advisor_enable, websearch_enable=websearch_enable,
                )
                if response.termination == LLMTermination.PAUSED:
                    # Exhausted max_pause_resumes without finishing.
                    unfinished[chapter_id] = f"still paused after {pause_count} resume attempt(s) — not persisted"
                    continue
                extra_content_parts = [paused_response.content] + [r.content for r in resumed[:-1]]
                if paused_response.thinking_content:
                    extra_thinking_parts.append(str(paused_response.thinking_content))
                extra_advisor_texts.extend(_advisor_consult_texts(paused_response))
                for r in resumed[:-1]:
                    if r.thinking_content:
                        extra_thinking_parts.append(str(r.thinking_content))
                    extra_advisor_texts.extend(_advisor_consult_texts(r))
                # resumed[-1] IS `response` (the final one) -- its content is
                # read directly off `response` below, not duplicated here.

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
                fallback_path, fallback_blocking = self._write_chapter(chapter_id, text)
                if fallback_blocking:
                    unfinished[chapter_id] = "fidelity gate: " + "; ".join(fallback_blocking)
                    logger.error(
                        "[ANTHROPIC-FIDELITY] %s: safety-fallback text failed its completeness "
                        "check; written to %s but NOT marked completed: %s",
                        chapter_id, fallback_path, "; ".join(fallback_blocking),
                    )
                    continue
                written[chapter_id] = fallback_path
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

            raw_text = "\n".join(part for part in (*extra_content_parts, response.content) if part)
            text, leaked_blocks = sanitize_chapter_output(raw_text)
            text = _CJK_LEAK_RE.sub("", text)
            if not text.strip():
                unfinished[chapter_id] = "no visible translation text"
                logger.error("[ANTHROPIC-BATCH] %s returned no visible text; NOT persisted", chapter_id)
                continue

            combined_thinking = "\n\n".join(
                part for part in (*extra_thinking_parts, response.thinking_content or "") if part
            ) or None
            combined_advisor_texts = [*extra_advisor_texts, *_advisor_consult_texts(response)]
            self._maybe_write_thinking_log(
                chapter_id=chapter_id,
                api_thinking=combined_thinking,
                leaked_blocks=leaked_blocks,
                advisor_texts=combined_advisor_texts,
            )
            if pause_count == 0:
                # A paused-then-resolved response's final call was already
                # logged inside _resolve_pauses under its own
                # #advisor-resume-N label; logging it again here would count
                # the same spend twice.
                self._log_usage(chapter_id, response)
            output_path, blocking = self._write_chapter(chapter_id, text)
            if blocking:
                # Not entered in `written`, so _finish_batch_run never stamps it
                # completed and the next run re-translates it. The conversation
                # ledger is deliberately NOT committed either: a chapter that
                # lost content should not become the continuity its successors
                # are built on.
                unfinished[chapter_id] = "fidelity gate: " + "; ".join(blocking)
                logger.error(
                    "[ANTHROPIC-FIDELITY] %s: %d fidelity problem(s); written to %s but NOT "
                    "marked completed and NOT committed to the ledger: %s",
                    chapter_id, len(blocking), output_path, "; ".join(blocking),
                )
                continue
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
        # The batch path never carried the Layer 2 advisor nudge at all -- a
        # pre-existing gap, not one the review request introduced. Both paths
        # attach the same advisor tool, so both need the same envelope, or a
        # batch run silently loses the previous-chapter review (plan §6.4).
        advisor_nudge = (
            build_advisor_guidance(
                eps_band,
                self._chapter_signals.get(chapter_id),
                self._preceding_chapter_id(chapter_id),
            )
            if self._advisor_cfg.get("enabled", False)
            else ""
        )
        if advisor_nudge:
            guidance = "\n\n".join(part for part in (guidance, advisor_nudge) if part)
        return build_chapter_message(
            chapter_id,
            jp_source,
            guidance,
            continuity=self._continuity_brief(chapter_id),
            anchor_rows=self._anchor_rows(chapter_id, jp_source),
        )

    def _write_chapter(self, chapter_id: str, text: str) -> Tuple[Path, List[str]]:
        """Persist one chapter and verify it. Returns (path, blocking_reasons).

        The text is always written -- a chapter that failed its check is far
        more useful on disk, where it can be read and diffed, than discarded.
        What the blocking reasons govern is whether the caller may record it
        as DONE.
        """
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        text = self._reinject_illustrations(chapter_id, text)
        output_path.write_text(text, encoding="utf-8")
        return output_path, self._check_chapter_fidelity(chapter_id, text)

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

    def _batch_params(self, messages: List[Dict[str, Any]], *, chapter_id: str, eps_band: str) -> Dict[str, Any]:
        """One batch request's params, taking a full ``messages`` array.

        It used to take a bare prompt string and wrap it as a single user
        turn, which is what made the batch path structurally incapable of
        carrying any conversation history at all. The array now arrives built
        — from the conversation ledger in the normal case — and the cached
        system blocks are shared with the synchronous path so a batch run is
        not the only route paying full price for its own prefix.

        ``chapter_id``/``eps_band`` apply the same per-chapter economic gate
        (spec §2.1-2.2) the synchronous path applies in translate_chapter --
        a chapter without an actual risk signal gets the tool(s) withheld
        from ITS request even when config enables them globally.
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

        qualifies = self._tools_qualify(chapter_id, eps_band)
        advisor_enabled = bool(self._advisor_cfg.get("enabled", False)) and (
            qualifies if bool(self._advisor_cfg.get("require_signal", True)) else True
        )
        websearch_enabled = bool(self._websearch_cfg.get("enabled", False)) and (
            qualifies if bool(self._websearch_cfg.get("require_signal", True)) else True
        )
        tools: List[Dict[str, Any]] = []
        if advisor_enabled:
            tools.append(
                build_advisor_tool(
                    self._advisor_cfg, route_caching_cfg=get_anthropic_caching_config()
                )
            )
        if websearch_enabled:
            tools.append({
                "type": str(self._websearch_cfg.get("type", "web_search_20250305")),
                "name": "web_search",
                "max_uses": int(self._websearch_cfg.get("max_uses", 5) or 5),
            })
        if tools:
            params["tools"] = tools
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


def _update_manifest_after_translation(
    work_dir: Path,
    translated_stems: set[str],
    blocked: Optional[Dict[str, str]] = None,
) -> None:
    """Stamp the manifest from one run's outcome.

    ``blocked`` maps a chapter stem to why it did not finish. That reason used
    to be logged and thrown away, which left the manifest with only two words
    for three situations: a chapter that succeeded, a chapter never attempted,
    and a chapter translated and then rejected by the fidelity gate. The last
    two both read "pending", so nothing downstream -- and no human -- could
    tell a 47KB rendering awaiting review from an empty slot.

    Recording the reason changes no policy: only "completed" is filtered out of
    the next run, so a blocked chapter is still offered again exactly as before.
    The status is "blocked" only where a rendering actually exists on disk;
    where nothing was persisted (a truncated max_output fragment, a paused
    request) it stays "pending", because that is the truthful word for it.
    """
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    blocked = blocked or {}
    chapters = manifest.get("chapters", [])
    for entry in chapters if isinstance(chapters, list) else []:
        if not isinstance(entry, dict):
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        if stem in translated_stems:
            entry["translation_status"] = "completed"
            entry.pop("last_failure", None)
        elif stem in blocked:
            rendered = (Path(work_dir) / "EN" / (stem + "_EN.md")).exists()
            entry["translation_status"] = "blocked" if rendered else "pending"
            entry["last_failure"] = blocked[stem]
    completed = sum(1 for entry in chapters if isinstance(entry, dict) and entry.get("translation_status") == "completed")
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if chapters and completed == len(chapters) else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
    }
    atomic_write_json(path, manifest)


def reconcile_manifest_from_disk(work_dir: Path) -> Dict[str, str]:
    """Re-derive chapter status from the renderings actually present in EN/.

    The manifest is stamped from a run's in-memory success set, and a chapter
    the fidelity gate rejects is deliberately left un-stamped so the next run
    offers it again (see translate_and_persist_chapter). What was missing is any
    later re-examination: once the CAUSE of a block is fixed, the chapter stays
    un-stamped forever, and the next run pays full API price to re-translate a
    rendering that has been correct on disk the whole time.

    Measured on volume a6cbaa: CHAPTER_04 sat "pending" beside a 47,673-byte
    translation because anchor a11 was reported missing -- a lock stored with
    U+2026 tested literally against prose written with three periods, fixed in
    verbatim_anchors._surface_pattern. The rendering was never wrong; only the
    verdict was, and nothing revisited it. The volume was then built anyway,
    which is how a 6/7 translator state produced a 7-chapter EPUB.

    This replays the gate against the text on disk, with no API call. A chapter
    already stamped "completed" is never examined, so a pass that cannot see
    bible-sourced forbidden synonyms can only clear a stale block or record a
    live one -- it can never silently downgrade a finished chapter.

    Returns {chapter_id: reason} for the chapters that still fail.
    """
    path = Path(work_dir) / "manifest.json"
    if not path.exists():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list):
        return {}

    try:
        context_xml = (Path(work_dir) / "context.xml").read_text(encoding="utf-8")
    except OSError:
        context_xml = ""
    anchors = parse_anchors(context_xml)

    cfg = get_anthropic_fidelity_config()
    block_on = {
        str(item).strip().lower()
        for item in (cfg.get("block_on") or ["missing", "drifted", "structural"])
    }
    try:
        tolerance = float(cfg.get("ratio_tolerance", DEFAULT_RATIO_TOLERANCE) or DEFAULT_RATIO_TOLERANCE)
    except (TypeError, ValueError):
        tolerance = DEFAULT_RATIO_TOLERANCE

    pairs: Dict[str, Tuple[str, str]] = {}
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        jp_path = Path(work_dir) / "JP" / (stem + ".md")
        en_path = Path(work_dir) / "EN" / (stem + "_EN.md")
        if not (jp_path.exists() and en_path.exists()):
            continue
        try:
            pairs[stem] = (
                jp_path.read_text(encoding="utf-8"),
                en_path.read_text(encoding="utf-8"),
            )
        except OSError:
            continue

    healed: List[str] = []
    still_blocked: Dict[str, str] = {}
    for entry in chapters:
        if not isinstance(entry, dict) or entry.get("translation_status") == "completed":
            continue
        stem = Path(str(entry.get("source_file", ""))).stem
        if stem not in pairs:
            continue  # nothing rendered; "pending" is already the honest word
        jp_source, en_text = pairs[stem]
        peers = [
            ratio
            for other, (other_jp, other_en) in pairs.items()
            if other != stem
            for ratio in [length_ratio(other_jp, other_en)]
            if ratio is not None
        ]
        reasons = [
            "anchor %s %s" % (row.get("anchor_id"), row.get("status"))
            for row in reconcile_chapter(
                anchors, chapter_id=stem, jp_source=jp_source, en_text=en_text
            )
            if str(row.get("status")) in ("missing", "drifted")
            and str(row.get("status")) in block_on
        ]
        if "structural" in block_on:
            reasons.extend(
                "illustration parity: source %s, output %s"
                % (row.get("jp_count"), row.get("en_count"))
                for row in structural_report(jp_source, en_text, peers, tolerance=tolerance)
                if row.get("check") == "illustration_parity"
                and str(row.get("status")) == "mismatch"
            )
        if reasons:
            entry["translation_status"] = "blocked"
            entry["last_failure"] = "; ".join(reasons)
            still_blocked[stem] = entry["last_failure"]
        else:
            entry["translation_status"] = "completed"
            entry.pop("last_failure", None)
            healed.append(stem)

    if not (healed or still_blocked):
        return {}

    completed = sum(
        1 for entry in chapters if isinstance(entry, dict) and entry.get("translation_status") == "completed"
    )
    manifest.setdefault("pipeline_state", {})["translator"] = {
        "status": "completed" if chapters and completed == len(chapters) else "in_progress",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapters_completed": completed,
        "chapters_total": len(chapters),
    }
    atomic_write_json(path, manifest)
    if healed:
        logger.info(
            "[ANTHROPIC-MANIFEST] reconciled from disk: %d chapter(s) stamped completed (%s)",
            len(healed), ", ".join(sorted(healed)),
        )
    if still_blocked:
        logger.warning(
            "[ANTHROPIC-MANIFEST] %d rendering(s) on disk still fail the gate: %s",
            len(still_blocked),
            "; ".join("%s (%s)" % (cid, reason) for cid, reason in sorted(still_blocked.items())),
        )
    return still_blocked

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
        reconcile_manifest_from_disk(work_dir)
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


def _advisor_consult_texts(response) -> List[str]:
    """Plaintext advisor consult text from one response, if any.

    ``block.text`` is populated only for the plaintext advisor_result variant
    (e.g. claude-opus-4-8, claude-sonnet-5 as advisor); it is "" by
    construction for the encrypted advisor_redacted_result variant
    (claude-fable-5-1, claude-opus-5, and the Mythos family), so this is
    naturally silent for those without any type-specific branching.
    """
    return [
        block.text for block in (response.content_blocks or [])
        if block.type == "advisor_result" and block.text
    ]
