"""OpenAI Phase 2 translator facade with the established filesystem contract."""

from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
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
from src.Deepseek.common.chapter_signals import (
    build_chapter_signal_guidance,
    normalize_reasoning_effort,
    parse_chapter_signals,
    select_reasoning_effort,
)
from src.OpenAI.client import OpenAIClient, configuration_update_item
from src.OpenAI.config import (
    get_openai_batch_config,
    get_openai_config,
    get_openai_continuation_config,
    get_openai_conversation_config,
    get_openai_optimization_config,
    get_openai_prompt_path,
    openai_batch_enabled,
)
from src.OpenAI.context import (
    derive_chapter_eps_band,
    parse_character_roster_handles,
    parse_eps_signals,
    parse_voice_fingerprints,
    parse_volume_type,
    resolve_voice_aliases,
)
from src.OpenAI.errors import OpenAIAPIError, OpenAIRefusalError, classify_exception, is_fallback_eligible
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
        self._config = deepcopy(config if config is not None else get_openai_config())
        self._active_config = deepcopy(self._config)
        self.dry_run = dry_run
        self.primary_model = str(self._config.get("model", "gpt-6-sol") or "gpt-6-sol")
        fallback_cfg = self._config.get("fallback", {}) or {}
        self.fallback_enabled = bool(fallback_cfg.get("enabled", False))
        self.fallback_model = str(fallback_cfg.get("model", "gpt-6-luna") or "gpt-6-luna")
        self.allow_mixed_model_volume = bool(fallback_cfg.get("allow_mixed_model_volume", False))
        self.resume_pending = bool(fallback_cfg.get("resume_pending", False))
        self.batch_config = get_openai_batch_config(self._config)
        self.batch_enabled = openai_batch_enabled(self._config, self.primary_model)
        self._batch_fallback_reason: Optional[str] = None
        self._using_fallback = False
        routing_cfg = self._config.get("routing", {}) or {}
        raw_route_path = Path(str(routing_cfg.get("persistence_file", ".context/openai_route.json")))
        self._route_state_path = raw_route_path if raw_route_path.is_absolute() else self.work_dir / raw_route_path
        self._route_state = self._load_route_state()
        self._committed_chapters = int(self._route_state.get("committed_chapters", 0) or 0)
        self._route_blocked = str(self._route_state.get("status") or "") == "fallback_pending"
        self.client = OpenAIClient(config=self._config, model=self.primary_model, dry_run=dry_run)
        conversation_cfg = self._conversation_config_for(self.primary_model, get_openai_conversation_config(self._active_config))
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                conversation_config=conversation_cfg,
            )
        # The continuity envelope can splice the predecessor's ending even
        # on the primary Batch route. Initialise this before any chapter
        # prompt is assembled; fallback activation reuses the same setting.
        self._previous_tail_chars = max(
            0, int(conversation_cfg.get("previous_chapter_tail_chars", 1200) or 1200)
        )
        from src.Deepseek.translator.context_manager import load_context_xml

        context_xml = load_context_xml(self.work_dir)
        self._context_xml = context_xml
        self._voice_profiles = resolve_voice_aliases(
            parse_voice_fingerprints(context_xml), parse_character_roster_handles(context_xml)
        )
        self._eps_signals = parse_eps_signals(context_xml)
        self._chapter_signals = parse_chapter_signals(context_xml)
        self._volume_type = parse_volume_type(context_xml)
        self.system_instruction = build_system_instruction(
            prompt_path=get_openai_prompt_path(self._config), context_xml=context_xml, model=self.client.model
        )
        self._previous_guidance_text: Optional[str] = None
        continuation = get_openai_continuation_config(self._active_config)
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))
        reasoning_cfg = self._active_config.get("reasoning", {}) or {}
        self._chapter_signals_enabled = bool(
            (self._active_config.get("chapter_signals", {}) or {}).get("enabled", True)
        )
        self._applied_reasoning_effort = normalize_reasoning_effort(
            self.client.model, reasoning_cfg.get("effort", "max")
        )
        self._configuration_update_mode_warned = False
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
        # at all on a selected tier. None of (b)/(c) are detectable from this
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

        persisted_model = str(self._route_state.get("selected_model") or "")
        if (
            self.fallback_enabled
            and self.fallback_model != self.primary_model
            and persisted_model == self.fallback_model
        ):
            self._activate_fallback(str(self._route_state.get("reason") or "resumed_route"))
        elif self._route_blocked and self.resume_pending and self.fallback_enabled:
            self.allow_mixed_model_volume = True
            self._activate_fallback("explicit_resume_pending")
            self._route_blocked = False

    def translate_chapter(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
        if self._route_blocked:
            raise OpenAIAPIError(
                f"OpenAI route for {self.volume_id} is fallback_pending after "
                f"{self._committed_chapters} committed chapter(s); set "
                "fallback.resume_pending=true or resolve the route explicitly."
            )
        try:
            return self._translate_chapter_once(chapter_path, chapter_meta)
        except Exception as exc:  # noqa: BLE001 - route boundary must see normalized and SDK errors
            if self._should_fallback(exc):
                self._activate_fallback(str(exc))
                return self._translate_chapter_once(chapter_path, chapter_meta)
            raise

    def _translate_chapter_once(self, chapter_path: Path, chapter_meta: Optional[Dict[str, Any]] = None) -> str:
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
        signal_record = self._chapter_signals.get(chapter_id)
        signal_guidance = build_chapter_signal_guidance(signal_record)
        guidance = build_chapter_guidance(
            eps_band,
            active_characters,
            get_openai_optimization_config(self._active_config),
            self._volume_type,
        )
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        prompt = build_chapter_message(
            chapter_id, jp_source, guidance, previous_guidance_text=self._previous_guidance_text
        )
        self._previous_guidance_text = guidance.strip() if guidance else None
        manager = self.client.conversation_manager
        input_items = (
            manager.input_items(
                self.system_instruction,
                prompt,
                int(get_openai_conversation_config(self._active_config).get("recent_verbatim_chapters", 2) or 2),
            )
            if manager is not None and not self.dry_run
            else None
        )
        configuration_update = self._configuration_update_for(signal_record)
        response = self.client.generate(
            prompt=prompt,
            system_instruction=self.system_instruction,
            input_items=input_items,
            dry_run=self.dry_run,
            configuration_update=configuration_update,
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

        if configuration_update:
            self._applied_reasoning_effort = configuration_update
        chapter_input = _user_input(prompt)
        request_input = input_items or _single_turn_input(self.system_instruction, prompt)
        replay_prefix: List[Dict[str, Any]] = (
            [configuration_update_item(configuration_update), chapter_input]
            if configuration_update
            else [chapter_input]
        )
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
        self._committed_chapters += 1
        self._write_route_state(status="active", reason=self._route_state.get("reason", ""))
        return text

    def _should_fallback(self, exc: BaseException) -> bool:
        if not self.fallback_enabled or self._using_fallback or self.primary_model == self.fallback_model:
            return False
        if not is_fallback_eligible(exc):
            return False
        if self._committed_chapters and not self.allow_mixed_model_volume:
            self._route_blocked = True
            self._write_route_state(status="fallback_pending", reason=str(exc))
            logger.error(
                "[OPENAI-ROUTE] %s — primary failed after %d committed chapter(s); "
                "automatic mixed-model fallback is disabled",
                self.volume_id,
                self._committed_chapters,
            )
            return False
        return True

    def _activate_fallback(self, reason: str) -> None:
        if self._using_fallback:
            return
        fallback_config = self._build_fallback_config()
        self._active_config = fallback_config
        self.batch_config = get_openai_batch_config(self._active_config)
        self.batch_enabled = openai_batch_enabled(self._active_config, self.fallback_model)
        self.client = OpenAIClient(config=fallback_config, model=self.fallback_model, dry_run=self.dry_run)
        conversation_cfg = self._conversation_config_for(
            self.fallback_model,
            fallback_config.get("conversation", {}) or {},
        )
        if conversation_cfg.get("enabled", True):
            self.client.attach_conversation(
                work_dir=self.work_dir,
                volume_id=self.volume_id,
                conversation_config=conversation_cfg,
            )
        self._previous_tail_chars = max(
            0, int(conversation_cfg.get("previous_chapter_tail_chars", 1200) or 1200)
        )
        self.system_instruction = build_system_instruction(
            prompt_path=get_openai_prompt_path(fallback_config),
            context_xml=self._context_xml,
            model=self.client.model,
        )
        continuation = get_openai_continuation_config(self._active_config)
        self.continuation_enabled = bool(continuation.get("enabled", True))
        self.max_continuations = max(0, int(continuation.get("max_continuations", 3) or 3))
        reasoning_cfg = self._active_config.get("reasoning", {}) or {}
        self._chapter_signals_enabled = bool(
            (self._active_config.get("chapter_signals", {}) or {}).get("enabled", True)
        )
        self._applied_reasoning_effort = normalize_reasoning_effort(
            self.client.model, reasoning_cfg.get("effort", "max")
        )
        self._configuration_update_mode_warned = False
        self._using_fallback = True
        self._write_route_state(status="fallback", reason=reason)
        logger.warning(
            "[OPENAI-ROUTE] %s — switched from %s to %s: %s",
            self.volume_id,
            self.primary_model,
            self.fallback_model,
            reason,
        )

    def _build_fallback_config(self) -> Dict[str, Any]:
        fallback_cfg = self._config.get("fallback", {}) or {}
        result = deepcopy(self._config)
        result["model"] = self.fallback_model
        for section in ("generation", "reasoning", "caching", "streaming", "conversation", "continuation", "retry", "optimizations"):
            if section in fallback_cfg and isinstance(fallback_cfg[section], dict):
                result[section] = deepcopy(fallback_cfg[section])
        prompt_path = fallback_cfg.get("master_prompt") or fallback_cfg.get("prompt")
        if prompt_path:
            result["master_prompt"] = str(prompt_path)
        return result

    def _configuration_update_for(self, signal_record: Optional[Dict[str, Any]]) -> Optional[str]:
        """Return the next GPT-6 effort, if this chapter changes the applied tier."""
        if not self._chapter_signals_enabled or self.client.model.lower() not in {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna"}:
            return None
        reasoning_cfg = self._active_config.get("reasoning", {}) or {}
        mode = str(reasoning_cfg.get("mode", "standard") or "standard").strip().lower()
        if mode != "standard":
            if not self._configuration_update_mode_warned:
                logger.warning(
                    "[OPENAI-CONFIGURATION] chapter signal escalation requires "
                    "reasoning.mode=standard; updates disabled for mode=%s",
                    mode,
                )
                self._configuration_update_mode_warned = True
            return None
        target = select_reasoning_effort(
            self.client.model,
            reasoning_cfg.get("effort", "max"),
            signal_record,
        )
        if target is None or target == self._applied_reasoning_effort:
            return None
        return target

    def _conversation_config_for(self, model: str, base: Dict[str, Any]) -> Dict[str, Any]:
        result = deepcopy(base)
        fallback_cfg = self._config.get("fallback", {}) or {}
        explicit = result.get("persistence_file")
        if model != self.primary_model:
            explicit = fallback_cfg.get("persistence_file")
        if not explicit or (model.lower() in {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna"} and explicit == ".context/openai_conversation.json"):
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_").lower()
            explicit = f".context/openai_conversation_{slug}.json"
        result["persistence_file"] = str(explicit)
        return result

    def _load_route_state(self) -> Dict[str, Any]:
        if not self._route_state_path.exists():
            return {}
        try:
            loaded = json.loads(self._route_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(loaded, dict) and str(loaded.get("volume_id")) == self.volume_id:
            return loaded
        return {}

    def _write_route_state(self, *, status: str, reason: str) -> None:
        atomic_write_json(
            self._route_state_path,
            {
                "schema_version": "1.0",
                "volume_id": self.volume_id,
                "primary_model": self.primary_model,
                "fallback_model": self.fallback_model,
                "selected_model": self.client.model,
                "status": status,
                "reason": reason,
                "committed_chapters": self._committed_chapters,
                "using_fallback": self._using_fallback,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    def _preceding_chapter_id(self, chapter_id: str) -> Optional[str]:
        match = re.match(r"^(.*?)(\d+)$", str(chapter_id))
        if match is None:
            return None
        prefix, digits = match.group(1), match.group(2)
        index = int(digits)
        return None if index <= 1 else "%s%0*d" % (prefix, len(digits), index - 1)

    def _chapter_tail_from_disk(self, chapter_id: str) -> str:
        if self._previous_tail_chars <= 0:
            return ""
        try:
            text = (self.work_dir / "EN" / f"{chapter_id}_EN.md").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not text:
            return ""
        return text if len(text) <= self._previous_tail_chars else text[-self._previous_tail_chars :].lstrip()

    def _continuity_brief(self, chapter_id: str) -> Optional[Dict[str, Any]]:
        manager = self.client.conversation_manager
        if manager is None:
            return None
        brief = dict(manager.continuity_state())
        previous_id = self._preceding_chapter_id(chapter_id)
        if not previous_id:
            return brief
        brief["previous_chapter_id"] = previous_id
        verbatim = set(brief.get("verbatim_chapter_ids") or [])
        summarized = set(brief.get("summarized_chapter_ids") or [])
        if previous_id in verbatim:
            brief["previous_chapter_status"] = "translated_in_full_in_this_request"
        elif previous_id in summarized:
            brief["previous_chapter_status"] = "summarized"
        else:
            brief["previous_chapter_status"] = "absent"
        tail = self._chapter_tail_from_disk(previous_id)
        if tail:
            brief["previous_chapter_tail"] = tail
        return brief

    def _build_chapter_prompt(self, chapter_id: str, chapter_path: Path) -> str:
        jp_source = Path(chapter_path).read_text(encoding="utf-8")
        signals = self._eps_signals.get(chapter_id, [])
        eps_band = derive_chapter_eps_band(signals)
        active_characters = [
            {"name": signal["name"], "fingerprint": self._voice_profiles.get(signal["name"], {})}
            for signal in signals
            if signal.get("name")
        ]
        guidance = build_chapter_guidance(
            eps_band,
            active_characters,
            get_openai_optimization_config(self._active_config),
            self._volume_type,
        )
        signal_guidance = build_chapter_signal_guidance(self._chapter_signals.get(chapter_id))
        if signal_guidance:
            guidance = "\n\n".join(part for part in (guidance, signal_guidance) if part)
        return build_chapter_message(
            chapter_id,
            jp_source,
            guidance,
            continuity=self._continuity_brief(chapter_id),
        )

    def _batch_state_path(self, batch_cfg: Dict[str, Any]) -> Path:
        raw = Path(str(batch_cfg.get("persistence_file", ".context/openai_batch_state.json")))
        return raw if raw.is_absolute() else self.work_dir / raw

    def _batch_params(
        self,
        *,
        prompt: str,
        input_items: List[Dict[str, Any]],
        configuration_update: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.client.build_request(
            prompt=prompt,
            system_instruction=self.system_instruction,
            input_items=input_items,
            dry_run=False,
            stream=False,
            configuration_update=configuration_update,
        )

    def translate_volume_batch(self, chapter_files: List[Path]) -> Dict[str, Path]:
        """Translate a volume through OpenAI Batch in continuity-aware waves.

        Each wave freezes one conversation-ledger prefix before building its
        JSONL requests. Results are decoded in chapter order and committed to
        the same local ledger, including raw reasoning output, before the next
        wave is assembled. This is the OpenAI equivalent of the Anthropic Fable
        5.1 batch design; it avoids claiming that sibling requests can see one
        another while preserving long-horizon translation awareness.
        """
        if self._route_blocked:
            raise OpenAIAPIError(
                f"OpenAI route for {self.volume_id} is fallback_pending after "
                f"{self._committed_chapters} committed chapter(s); set "
                "fallback.resume_pending=true or resolve the route explicitly."
            )
        from src.OpenAI.batch import (
            BatchLedger,
            retrieve_batch_results,
            submit_batch,
        )

        pending = sorted(Path(path) for path in chapter_files)
        if not pending:
            return {}
        batch_cfg = self.batch_config
        manager = self.client.conversation_manager
        wave_size = max(1, int(batch_cfg.get("wave_size", 4) or 4))
        poll_seconds = max(1, int(batch_cfg.get("poll_seconds", 60) or 60))
        deadline_seconds = _completion_window_seconds(batch_cfg.get("completion_window", "24h"))
        ledger = BatchLedger(
            self._batch_state_path(batch_cfg),
            volume_id=self.volume_id,
            model=self.client.model,
            endpoint="/v1/responses",
        )
        written: Dict[str, Path] = {}
        unfinished: Dict[str, str] = {}

        settled = self._drain_open_batches(
            ledger,
            manager,
            {path.stem: path for path in pending},
            written,
            unfinished,
            poll_seconds=poll_seconds,
            deadline_seconds=deadline_seconds,
        )
        if self._using_fallback:
            remaining = [path for path in pending if path.stem not in written]
            written.update(self.translate_all(remaining))
            self._finish_batch_run(written, unfinished)
            return written
        if self._route_blocked:
            self._finish_batch_run(written, unfinished)
            return written
        pending = [path for path in pending if path.stem not in settled]
        if not pending:
            self._finish_batch_run(written, unfinished)
            return written

        seed_count = max(0, int(batch_cfg.get("seed_chapters", 1) or 0))
        seed_via_batch = bool(batch_cfg.get("seed_via_batch", True))
        seed_wave: List[Path] = []
        if manager is not None and not manager.turns and seed_count:
            seed_wave = pending[:seed_count]
            pending = pending[seed_count:]
            if not seed_via_batch:
                for path in seed_wave:
                    written[path.stem] = self.translate_and_persist_chapter(
                        path, {"chapter_id": path.stem}
                    )
                seed_wave = []
        waves: List[List[Path]] = ([seed_wave] if seed_wave else []) + list(_chunk_paths(pending, wave_size))

        for wave_number, wave in enumerate(waves, start=1):
            if not wave:
                continue
            if manager is not None:
                manager.prepare_prefix()
            prompts: Dict[str, str] = {}
            replay_inputs: Dict[str, List[Dict[str, Any]]] = {}
            requests: List[Dict[str, Any]] = []
            for path in wave:
                chapter_id = path.stem
                prompt = self._build_chapter_prompt(chapter_id, path)
                signal_record = self._chapter_signals.get(chapter_id)
                update = self._configuration_update_for(signal_record)
                input_items = (
                    manager.input_items(
                        self.system_instruction,
                        prompt,
                        recent_count=None,
                        compact=False,
                    )
                    if manager is not None
                    else _single_turn_input(self.system_instruction, prompt)
                )
                body = self._batch_params(
                    prompt=prompt,
                    input_items=input_items,
                    configuration_update=update,
                )
                requests.append(
                    {
                        "custom_id": chapter_id,
                        "method": "POST",
                        "url": "/v1/responses",
                        "body": body,
                    }
                )
                prompts[chapter_id] = prompt
                replay_inputs[chapter_id] = (
                    [configuration_update_item(update)] if update else []
                ) + [_user_input(prompt)]

            try:
                submission = submit_batch(
                    self.client,
                    requests,
                    completion_window=str(batch_cfg.get("completion_window", "24h") or "24h"),
                    metadata={
                        "mtls_volume_id": self.volume_id,
                        "mtls_model": self.client.model,
                        "mtls_wave": str(wave_number),
                    },
                )
            except Exception as exc:  # noqa: BLE001 - normalize at the route boundary
                normalized = classify_exception(exc)
                if self._should_fallback(normalized):
                    self._activate_fallback(str(normalized))
                    remaining = [
                        path
                        for path in [*wave, *pending]
                        if path.stem not in written
                    ]
                    return {
                        **written,
                        **self.translate_all(remaining),
                    }
                raise normalized

            batch_id = str(submission.get("id") or "")
            input_file_id = str(submission.get("input_file_id") or "")
            chapter_ids = [path.stem for path in wave]
            ledger.record_submitted(
                batch_id,
                wave=wave_number,
                chapter_ids=chapter_ids,
                input_file_id=input_file_id,
            )
            completed_batch = self._await_batch(
                batch_id,
                poll_seconds=poll_seconds,
                deadline_seconds=deadline_seconds,
            )
            if completed_batch is None:
                for chapter_id in chapter_ids:
                    unfinished[chapter_id] = f"batch {batch_id} still open"
                logger.error(
                    "[OPENAI-BATCH] batch_id=%s did not end within the completion window; "
                    "leaving it in %s for recovery",
                    batch_id,
                    ledger.path,
                )
                break

            status = str(completed_batch.get("status") or "unknown")
            ledger.record_ended(
                batch_id,
                status=status,
                output_file_id=str(completed_batch.get("output_file_id") or ""),
                error_file_id=str(completed_batch.get("error_file_id") or ""),
            )
            outcome = retrieve_batch_results(
                self.client,
                completed_batch,
                model=self.client.model,
                expected_ids=chapter_ids,
            )
            unfinished.update(outcome.unfinished)
            self._absorb_batch_results(
                outcome,
                wave,
                prompts,
                replay_inputs,
                manager,
                written,
                unfinished,
            )
            if outcome.fallback_eligible:
                reason = next(iter(outcome.fallback_eligible.values()))
                fallback_error = OpenAIAPIError(reason, code="model_not_found")
                if self._should_fallback(fallback_error):
                    self._activate_fallback(reason)
                    remaining = [
                        path
                        for path in [*wave, *pending]
                        if path.stem not in written
                    ]
                    written.update(self.translate_all(remaining))
                    self._finish_batch_run(written, unfinished)
                    return written
            if outcome.unfinished or status != "completed":
                # Do not build a later wave against a ledger that lacks one of
                # this wave's turns. A rerun can safely recover/resubmit the
                # retryable entries from the durable ledger.
                break

        self._finish_batch_run(written, unfinished)
        return written

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
    ) -> set[str]:
        from src.OpenAI.batch import batch_is_terminal, poll_batch, retrieve_batch_results

        settled: set[str] = set()
        for entry in ledger.recoverable_batches():
            batch_id = str(entry.get("batch_id") or "")
            chapter_ids = [str(cid) for cid in entry.get("chapter_ids") or [] if str(cid) in jp_by_id]
            if not batch_id or not chapter_ids:
                if batch_id:
                    ledger.record_abandoned(batch_id, "no pending chapters remain for this job")
                continue
            if str(entry.get("status") or "") == "submitted":
                batch = self._await_batch(
                    batch_id,
                    poll_seconds=poll_seconds,
                    deadline_seconds=deadline_seconds,
                )
                if batch is None:
                    for chapter_id in chapter_ids:
                        unfinished[chapter_id] = f"batch {batch_id} still open"
                    settled.update(chapter_ids)
                    continue
            else:
                batch = {
                    "id": batch_id,
                    "status": entry.get("status", "completed"),
                    "output_file_id": entry.get("output_file_id"),
                    "error_file_id": entry.get("error_file_id"),
                }
            status = str(batch.get("status") or "unknown")
            ledger.record_ended(
                batch_id,
                status=status,
                output_file_id=str(batch.get("output_file_id") or ""),
                error_file_id=str(batch.get("error_file_id") or ""),
            )
            outcome = retrieve_batch_results(
                self.client,
                batch,
                model=self.client.model,
                expected_ids=chapter_ids,
            )
            unfinished.update(outcome.unfinished)
            paths = [jp_by_id[chapter_id] for chapter_id in chapter_ids]
            prompts = {path.stem: self._build_chapter_prompt(path.stem, path) for path in paths}
            replay_inputs = {path.stem: [_user_input(prompts[path.stem])] for path in paths}
            self._absorb_batch_results(
                outcome,
                paths,
                prompts,
                replay_inputs,
                manager,
                written,
                unfinished,
            )
            if outcome.fallback_eligible:
                reason = next(iter(outcome.fallback_eligible.values()))
                fallback_error = OpenAIAPIError(reason, code="model_not_found")
                if self._should_fallback(fallback_error):
                    self._activate_fallback(reason)
                    self._batch_fallback_reason = reason
            settled.update(chapter_id for chapter_id in chapter_ids if chapter_id not in outcome.retryable)
            settled.difference_update(outcome.fallback_eligible)
        return settled

    def _absorb_batch_results(
        self,
        outcome,
        wave: List[Path],
        prompts: Dict[str, str],
        replay_inputs: Dict[str, List[Dict[str, Any]]],
        manager,
        written: Dict[str, Path],
        unfinished: Dict[str, str],
    ) -> None:
        for path in wave:
            chapter_id = path.stem
            response = outcome.succeeded.get(chapter_id)
            if response is None:
                continue
            if response.termination == LLMTermination.REFUSED:
                try:
                    text = self._safety_fallback_translate(
                        path,
                        chapter_id,
                        OpenAIRefusalError(f"OpenAI declined translation of {chapter_id}."),
                    )
                except Exception as exc:  # noqa: BLE001 - leave chapter pending
                    unfinished[chapter_id] = f"refused; safety fallback failed: {exc}"
                    continue
                written[chapter_id] = self._write_chapter(chapter_id, text)
                continue
            if response.termination == LLMTermination.MAX_OUTPUT:
                unfinished[chapter_id] = "max_output — truncated, not persisted"
                continue
            text, leaked_blocks = split_thinking_from_output(response.content)
            text = _CJK_LEAK_RE.sub("", text)
            if not text.strip():
                unfinished[chapter_id] = "no visible translation text"
                continue
            self._maybe_write_thinking_log(
                chapter_id=chapter_id,
                api_thinking=response.thinking_content,
                leaked_blocks=leaked_blocks,
            )
            self._log_usage(chapter_id, response)
            output_path = self._write_chapter(chapter_id, text)
            written[chapter_id] = output_path
            raw_output = _raw_output(response)
            if manager is not None:
                if not raw_output:
                    unfinished[chapter_id] = "missing raw Responses output; not persisted"
                    written.pop(chapter_id, None)
                    continue
                manager.commit(
                    chapter_id=chapter_id,
                    user_input=replay_inputs.get(chapter_id) or [_user_input(prompts[chapter_id])],
                    response_output=raw_output,
                    chapter_text=text,
                    output_path=output_path,
                )
            self._committed_chapters += 1
            self._write_route_state(status="active", reason=self._route_state.get("reason", ""))

    def _write_chapter(self, chapter_id: str, text: str) -> Path:
        output_path = self.work_dir / "EN" / f"{chapter_id}_EN.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        return output_path

    def _await_batch(
        self,
        batch_id: str,
        *,
        poll_seconds: int,
        deadline_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        from src.OpenAI.batch import batch_is_terminal, poll_batch

        started = time.monotonic()
        while True:
            status = poll_batch(self.client, batch_id)
            if batch_is_terminal(str(status.get("status") or "")):
                return status
            if time.monotonic() - started >= deadline_seconds:
                return None
            time.sleep(poll_seconds)

    def _finish_batch_run(self, written: Dict[str, Path], unfinished: Dict[str, str]) -> None:
        if written:
            _update_manifest_after_translation(self.work_dir, set(written))
        if unfinished:
            logger.warning(
                "[OPENAI-BATCH] %d chapter(s) left pending: %s",
                len(unfinished),
                "; ".join(f"{chapter_id} ({reason})" for chapter_id, reason in sorted(unfinished.items())),
            )

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
    translator = OpenAITranslator(
        work_dir, volume_id, thinking_log_enabled=thinking_log_enabled, dry_run=dry_run
    )
    if translator.batch_enabled and not dry_run:
        return translator.translate_volume_batch(chapter_files)
    return translator.translate_all(chapter_files)


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


def _completion_window_seconds(raw: Any, default_seconds: float = 24 * 3600.0) -> float:
    text = str(raw or "").strip().lower()
    if not text:
        return default_seconds
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    multiplier = units.get(text[-1], 1.0)
    number = text[:-1] if text[-1] in units else text
    try:
        value = float(number)
    except ValueError:
        logger.warning("[OPENAI-BATCH] unparseable completion_window %r; using %.0fs", raw, default_seconds)
        return default_seconds
    return value * multiplier if value > 0 else default_seconds


def _chunk_paths(items: List[Path], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
