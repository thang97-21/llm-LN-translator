"""OpenAI Responses/Batch provider for the preparation phase.

The provider deliberately reuses the prep pipeline's existing prompt builders:
``build_multiturn_system_prompt`` supplies the DeepSeek-derived role, policy,
Librarian shell, Japanese volume, series bible, and search evidence, while
``build_multiturn_task_suffix`` supplies the exact per-block schema and JSON
node contract. OpenAI Batch runs those active blocks independently, then the
existing deterministic node-to-XML assembly writes the normal ``context.xml``.

Every accepted Batch job is recorded before polling. Completed block nodes are
written atomically, so a process interruption can recover the accepted job and
replay only unresolved artifacts without submitting a duplicate volume job.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.config import PIPELINE_ROOT, WORK_DIR, get_config_section
from src.Deepseek.common.llm_types import LLMTermination
from src.OpenAI.batch import (
    BatchLedger,
    batch_is_terminal,
    poll_batch,
    retrieve_batch_results,
    submit_batch,
)
from src.OpenAI.client import OpenAIClient
from src.OpenAI.config import get_openai_prep_config
from src.utility.prep.agent import (
    PrepError,
    _finalize_and_write,
    _read_jp_chapters,
    _replace_prep_block,
    _wrap_jp_chapters,
    discover_series_bible,
)
from src.utility.prep.block_prompts import (
    MULTITURN_BLOCK_ORDER,
    _PRO_TASK_NOTE,
    build_multiturn_system_prompt,
    build_multiturn_task_suffix,
)
from src.utility.prep.json_xml_node import JsonNodeError, extract_json_node, node_to_element
from src.utility.prep.web_search_chain import MetadataSearchChain

logger = logging.getLogger(__name__)

_BATCH_ENDPOINT = "/v1/responses"
_ARTIFACT_DIR_NAME = "openai_prep"
_DEFAULT_MODEL = "gpt-6-sol"
_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class OpenAIPrepError(PrepError):
    """Raised when OpenAI prep cannot produce a complete context document."""


def _artifacts_dir(work_dir: Path) -> Path:
    return work_dir / ".context" / _ARTIFACT_DIR_NAME


def _state_path(work_dir: Path, batch_cfg: Dict[str, Any]) -> Path:
    raw = Path(str(batch_cfg.get("persistence_file", ".context/openai_prep_batch_state.json")))
    return raw if raw.is_absolute() else work_dir / raw


def _completion_window_seconds(value: Any) -> float:
    text = str(value or "24h").strip().lower()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            try:
                return max(0.0, float(text[:-1]) * multiplier)
            except ValueError:
                break
    return 86400.0


def _extract_opf_search_fields(context_xml_text: str) -> Tuple[str, str]:
    try:
        root = ET.fromstring(context_xml_text)
        opf = root.find("opf_metadata")
        data = json.loads(opf.text or "{}") if opf is not None else {}
    except (ET.ParseError, json.JSONDecodeError, TypeError):
        return "", ""
    return (
        str(data.get("dc_title_jp") or data.get("title") or "").strip(),
        str(data.get("author_jp") or data.get("author") or "").strip(),
    )


def _build_prep_inputs(
    work_dir: Path,
    prep_cfg: Dict[str, Any],
    series_id: Optional[str],
) -> Tuple[str, List[Tuple[str, str]], Optional[Tuple[str, Dict[str, Any]]], str, str]:
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise OpenAIPrepError(f"No context.xml at {context_path} — run extract first.")
    existing_context_xml = context_path.read_text(encoding="utf-8")
    chapters = _read_jp_chapters(work_dir)

    bible_dir = Path(prep_cfg.get("bible_dir", "bibles/"))
    if not bible_dir.is_absolute():
        bible_dir = PIPELINE_ROOT / bible_dir
    bible_match = discover_series_bible(existing_context_xml, bible_dir, series_id)
    bible_block = ""
    if bible_match:
        _, bible_data = bible_match
        bible_block = (
            "<bible_context>\n" + json.dumps(bible_data, ensure_ascii=False, indent=2)
            + "\n</bible_context>"
        )

    title_jp, author_jp = _extract_opf_search_fields(existing_context_xml)
    search_result = MetadataSearchChain(prep_cfg.get("web_search", {}) or {}).search(
        title_jp, author_jp
    )
    context_dir = work_dir / ".context"
    context_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(context_dir / "prep_web_search.json", search_result.to_dict())
    return (
        existing_context_xml,
        chapters,
        bible_match,
        search_result.to_prompt_block(),
        bible_block,
    )


def _client_config(provider_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the compact prep YAML menu to ``OpenAIClient``'s schema."""
    cfg = copy.deepcopy(provider_cfg)
    cfg.setdefault("model", _DEFAULT_MODEL)
    cfg.setdefault("endpoint", _DEFAULT_ENDPOINT)
    cfg.setdefault("api_key_env", _DEFAULT_API_KEY_ENV)
    cfg.setdefault("http_timeout_seconds", 600)

    generation = copy.deepcopy(cfg.get("generation", {}) or {})
    if "max_output_tokens" in cfg and "max_output_tokens" not in generation:
        generation["max_output_tokens"] = cfg["max_output_tokens"]
    generation.setdefault("max_output_tokens", 128_000)
    generation.setdefault("service_tier", "default")
    generation.setdefault("text", {"verbosity": "high"})
    cfg["generation"] = generation

    reasoning = copy.deepcopy(cfg.get("reasoning", {}) or {})
    if "effort" in cfg and "effort" not in reasoning:
        reasoning["effort"] = cfg["effort"]
    reasoning.setdefault("mode", "standard")
    reasoning.setdefault("effort", "high")
    reasoning.setdefault("context", "current_turn")
    reasoning.setdefault("summary", None)
    reasoning.setdefault("include_encrypted_content", True)
    cfg["reasoning"] = reasoning

    caching = copy.deepcopy(cfg.get("caching", {}) or {})
    caching.setdefault("mode", "explicit")
    caching.setdefault("ttl", "30m")
    cfg["caching"] = caching
    cfg.setdefault("conversation", {"store": False})
    cfg.setdefault("streaming", {"enabled": False})
    return cfg


def _prompt_signature(model: str, endpoint: str, system_prompt: str) -> str:
    payload = f"{model}\0{endpoint}\0{system_prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def build_prep_batch_requests(
    client: OpenAIClient,
    system_prompt: str,
    block_names: Iterable[str] = MULTITURN_BLOCK_ORDER,
) -> List[Dict[str, Any]]:
    """Build one native Responses Batch request per active prep block."""
    requests: List[Dict[str, Any]] = []
    for block_name in block_names:
        extra_note = _PRO_TASK_NOTE if block_name == "name_map" else ""
        prompt = build_multiturn_task_suffix(block_name, extra_note=extra_note)
        body = client.build_request(
            prompt=prompt,
            system_instruction=system_prompt,
            dry_run=False,
            stream=False,
        )
        requests.append(
            {
                "custom_id": block_name,
                "method": "POST",
                "url": _BATCH_ENDPOINT,
                "body": body,
            }
        )
    return requests


def _response_stats(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
    cache_read = int(getattr(usage, "cache_read_tokens", 0) or 0) if usage is not None else 0
    cache_write = int(getattr(usage, "cache_write_tokens", 0) or 0) if usage is not None else 0
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
    return {
        "cache_hit_tokens": max(0, cache_read),
        "cache_miss_tokens": max(0, input_tokens - cache_read - cache_write),
        "output_tokens": max(0, output_tokens),
    }


def _log_response_usage(volume_id: str, block_name: str, model: str, response: Any) -> None:
    from src.Deepseek.common.token_telemetry import log_call

    stats = _response_stats(response)
    log_call(
        phase="prep",
        volume_id=volume_id,
        call_label=f"openai_prep_{block_name}",
        model=model,
        cache_hit_tokens=stats["cache_hit_tokens"],
        fresh_tokens=stats["cache_miss_tokens"],
        output_tokens=stats["output_tokens"],
    )


def _dump_failed_response(artifacts_dir: Path, block_name: str, attempt: int, raw: str) -> None:
    try:
        atomic_write_text(artifacts_dir / f"{block_name}.attempt{attempt}.raw.txt", raw)
    except OSError as exc:
        logger.warning("[PREP:openai] could not save failed response for %s: %s", block_name, exc)


def _parse_response(response: Any, block_name: str) -> Dict[str, Any]:
    termination = getattr(response, "termination", None)
    if termination in {LLMTermination.REFUSED, LLMTermination.ERROR, LLMTermination.MAX_OUTPUT}:
        raise OpenAIPrepError(
            f"OpenAI response for block '{block_name}' ended with {termination.value}"
        )
    try:
        return extract_json_node(
            str(getattr(response, "content", "") or ""), expected_tag=block_name
        )
    except JsonNodeError as exc:
        raise OpenAIPrepError(
            f"OpenAI returned invalid JSON for block '{block_name}': {exc}"
        ) from exc


def _write_thinking_log(work_dir: Path, prep_cfg: Dict[str, Any], block_name: str, response: Any) -> None:
    thinking = str(getattr(response, "thinking_content", "") or "").strip()
    log_cfg = prep_cfg.get("thinking_log", {}) or {}
    if not thinking or not log_cfg.get("enabled", True) or not log_cfg.get("capture_reasoning", True):
        return
    output_dir = work_dir / str(log_cfg.get("output_dir", "THINKING")) / "prep"
    atomic_write_text(output_dir / f"{block_name}.md", thinking)


def _write_artifact(
    work_dir: Path,
    prep_cfg: Dict[str, Any],
    block_name: str,
    node: Dict[str, Any],
    response: Any,
) -> Path:
    artifacts_dir = _artifacts_dir(work_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = artifacts_dir / f"{block_name}.json"
    atomic_write_text(path, json.dumps(node, ensure_ascii=False, indent=2))
    _write_thinking_log(work_dir, prep_cfg, block_name, response)
    return path


def _load_artifacts(work_dir: Path, block_names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    artifacts: Dict[str, Dict[str, Any]] = {}
    for block_name in block_names:
        path = _artifacts_dir(work_dir) / f"{block_name}.json"
        if not path.exists():
            continue
        try:
            node = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(node, dict) and node.get("tag") == block_name:
                artifacts[block_name] = node
            else:
                logger.warning("[PREP:openai] ignoring artifact %s with the wrong tag", path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[PREP:openai] ignoring unreadable artifact %s: %s", path, exc)
    return artifacts


def _await_batch(
    client: OpenAIClient,
    batch_id: str,
    *,
    poll_seconds: float,
    deadline_seconds: float,
) -> Optional[Dict[str, Any]]:
    started = time.monotonic()
    while True:
        batch = poll_batch(client, batch_id)
        if batch_is_terminal(str(batch.get("status") or "")):
            return batch
        if time.monotonic() - started >= deadline_seconds:
            return None
        time.sleep(max(0.0, poll_seconds))


def _settle_batch(
    client: OpenAIClient,
    ledger: BatchLedger,
    entry: Dict[str, Any],
    expected_blocks: Sequence[str],
    *,
    work_dir: Path,
    prep_cfg: Dict[str, Any],
    volume_id: str,
    model: str,
    poll_seconds: float,
    deadline_seconds: float,
    attempt: int,
) -> Tuple[set[str], Dict[str, str], Optional[str], List[Dict[str, Any]]]:
    batch_id = str(entry.get("batch_id") or "")
    if not batch_id:
        return set(expected_blocks), {}, None, []
    if str(entry.get("status") or "") == "submitted":
        batch = _await_batch(
            client,
            batch_id,
            poll_seconds=poll_seconds,
            deadline_seconds=deadline_seconds,
        )
        if batch is None:
            return set(expected_blocks), {}, batch_id, []
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
        client,
        batch,
        model=model,
        expected_ids=expected_blocks,
    )
    retryable = set(outcome.retryable)
    permanent = dict(outcome.permanent)
    log: List[Dict[str, Any]] = []

    for block_name in expected_blocks:
        response = outcome.succeeded.get(block_name)
        if response is None:
            continue
        try:
            node = _parse_response(response, block_name)
        except OpenAIPrepError as exc:
            retryable.add(block_name)
            _dump_failed_response(
                _artifacts_dir(work_dir),
                block_name,
                attempt,
                str(getattr(response, "content", "")),
            )
            logger.warning("[PREP:openai] %s attempt %d failed validation: %s", block_name, attempt, exc)
            continue
        _write_artifact(work_dir, prep_cfg, block_name, node, response)
        _log_response_usage(volume_id, block_name, model, response)
        log.append({"call": f"batch_{block_name}", **_response_stats(response), "batch_pricing": True})

    for block_name, reason in outcome.unfinished.items():
        if block_name in expected_blocks and block_name not in permanent:
            retryable.add(block_name)
            logger.warning("[PREP:openai] %s unresolved in batch %s: %s", block_name, batch_id, reason)
    return retryable, permanent, None, log


def _assemble_and_finalize(
    work_dir: Path,
    existing_context_xml: str,
    chapters: List[Tuple[str, str]],
    artifacts: Dict[str, Dict[str, Any]],
    *,
    resolved_series_id: Optional[str],
    bible_match: Optional[Tuple[str, Dict[str, Any]]],
    generated_by: str,
    receipt_extra: Dict[str, Any],
) -> Dict[str, Any]:
    root = ET.fromstring(existing_context_xml)
    for block_name in MULTITURN_BLOCK_ORDER:
        try:
            _replace_prep_block(root, node_to_element(artifacts[block_name]))
        except (KeyError, JsonNodeError) as exc:
            raise OpenAIPrepError(
                f"block '{block_name}' artifact is malformed: {exc}; "
                f"artifacts are kept under {_artifacts_dir(work_dir)}"
            ) from exc

    from src.utility.prep.block_assembler import validate_cross_block_consistency

    validation_warnings = validate_cross_block_consistency(root)
    for warning in validation_warnings:
        logger.warning("[PREP:openai] cross-block check: %s", warning)

    receipt = _finalize_and_write(
        work_dir,
        work_dir / "context.xml",
        root,
        resolved_series_id=resolved_series_id,
        chapters=chapters,
        bible_match=bible_match,
        generated_by=generated_by,
    )
    receipt["openai_prep"] = {**receipt_extra, "validation_warnings": validation_warnings}
    return receipt


def _run_sync_blocks(
    client: OpenAIClient,
    work_dir: Path,
    prep_cfg: Dict[str, Any],
    system_prompt: str,
    model: str,
    block_names: Sequence[str],
    *,
    max_attempts: int,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    artifacts: Dict[str, Dict[str, Any]] = {}
    log: List[Dict[str, Any]] = []
    for block_name in block_names:
        prompt = build_multiturn_task_suffix(
            block_name,
            extra_note=_PRO_TASK_NOTE if block_name == "name_map" else "",
        )
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            response = None
            try:
                response = client.generate(
                    prompt=prompt,
                    system_instruction=system_prompt,
                    stream=False,
                )
                node = _parse_response(response, block_name)
                _write_artifact(work_dir, prep_cfg, block_name, node, response)
                _log_response_usage(work_dir.name, block_name, model, response)
                log.append({"call": f"sync_{block_name}", **_response_stats(response), "batch_pricing": False})
                artifacts[block_name] = node
                break
            except Exception as exc:  # noqa: BLE001 - retry only this block
                last_error = exc
                raw = str(getattr(response, "content", "") or exc)
                _dump_failed_response(_artifacts_dir(work_dir), block_name, attempt, raw)
                if attempt < max_attempts:
                    logger.warning("[PREP:openai] retrying sync block %s after: %s", block_name, exc)
        if block_name not in artifacts:
            raise OpenAIPrepError(
                f"OpenAI synchronous prep failed for block '{block_name}' after "
                f"{max_attempts} attempt(s): {last_error}"
            ) from last_error
    return artifacts, log


def run_openai_prep(
    volume_id: str,
    series_id: Optional[str] = None,
    *,
    force_rerun: bool = False,
) -> Dict[str, Any]:
    """Fill every active prep block through OpenAI Responses or Batch."""
    prep_cfg = get_config_section("prep")
    provider_cfg = get_openai_prep_config(prep_cfg)
    client_cfg = _client_config(provider_cfg)
    model = str(client_cfg.get("model", _DEFAULT_MODEL))
    work_dir = WORK_DIR / volume_id
    (
        existing_context_xml,
        chapters,
        bible_match,
        web_search_block,
        bible_block,
    ) = _build_prep_inputs(work_dir, prep_cfg, series_id)
    resolved_series_id = bible_match[0] if bible_match else series_id
    system_prompt = build_multiturn_system_prompt(
        existing_context_xml=existing_context_xml,
        jp_chapters_block=_wrap_jp_chapters(chapters),
        bible_block=bible_block,
        web_search_block=web_search_block,
    )
    client = OpenAIClient(config=client_cfg, model=model, dry_run=False)

    batch_cfg = provider_cfg.get("batch", {}) or {}
    use_batch = bool(batch_cfg.get("enabled", True))
    resume = bool(batch_cfg.get("resume_completed_blocks", True)) and not force_rerun
    artifacts = _load_artifacts(work_dir, MULTITURN_BLOCK_ORDER) if resume else {}
    remaining = [name for name in MULTITURN_BLOCK_ORDER if name not in artifacts]
    cache_log: List[Dict[str, Any]] = []
    submitted_blocks: List[str] = []
    permanent: Dict[str, str] = {}

    if use_batch:
        ledger = BatchLedger(
            _state_path(work_dir, batch_cfg),
            volume_id=volume_id,
            model=model,
            endpoint=_BATCH_ENDPOINT,
        )
        poll_seconds = max(0.0, float(batch_cfg.get("poll_seconds", 60) or 60))
        deadline_seconds = max(
            0.0,
            float(
                batch_cfg.get(
                    "max_wait_seconds",
                    _completion_window_seconds(batch_cfg.get("completion_window", "24h")),
                )
                or 0
            ),
        )

        # Recover accepted jobs before creating any new billable job. An
        # explicit force rerun intentionally ignores completed results, but it
        # must not create a duplicate while an earlier accepted job is open.
        if force_rerun:
            open_batches = ledger.open_batches()
            if open_batches:
                open_ids = ", ".join(str(entry.get("batch_id") or "") for entry in open_batches)
                raise OpenAIPrepError(
                    f"Cannot force-rerun OpenAI prep while Batch job(s) {open_ids} are still running; "
                    f"re-run after completion to avoid duplicate submissions."
                )
            recoverable_entries = []
        else:
            recoverable_entries = ledger.recoverable_batches()

        for entry in recoverable_entries:
            expected = [
                str(block)
                for block in entry.get("chapter_ids") or []
                if str(block) in remaining
            ]
            if not expected:
                continue
            retryable, failed, still_open, recovered_log = _settle_batch(
                client,
                ledger,
                entry,
                expected,
                work_dir=work_dir,
                prep_cfg=prep_cfg,
                volume_id=volume_id,
                model=model,
                poll_seconds=poll_seconds,
                deadline_seconds=deadline_seconds,
                attempt=0,
            )
            cache_log.extend(recovered_log)
            permanent.update(failed)
            if still_open:
                raise OpenAIPrepError(
                    f"OpenAI Batch {still_open} is still running; its accepted requests are "
                    f"recorded in {ledger.path}. Re-run after it reaches a terminal state "
                    "instead of submitting duplicate blocks."
                )
            artifacts.update(_load_artifacts(work_dir, expected))
            remaining = [name for name in remaining if name in retryable or name not in expected]

        if permanent:
            raise OpenAIPrepError(
                "OpenAI Batch returned permanent failures: "
                + "; ".join(f"{name} ({reason})" for name, reason in sorted(permanent.items()))
            )

        max_attempts = max(1, int(batch_cfg.get("max_attempts", 2) or 2))
        for attempt in range(1, max_attempts + 1):
            if not remaining:
                break
            requests = build_prep_batch_requests(client, system_prompt, remaining)
            submitted = submit_batch(
                client,
                requests,
                completion_window=str(batch_cfg.get("completion_window", "24h") or "24h"),
                metadata={
                    "mtls_volume_id": volume_id,
                    "mtls_phase": "prep",
                    "mtls_model": model,
                    "mtls_attempt": str(attempt),
                },
                output_expires_after=batch_cfg.get("output_expires_after"),
            )
            batch_id = str(submitted.get("id") or "")
            if not batch_id:
                raise OpenAIPrepError("OpenAI Batch submission returned no batch id.")
            input_file_id = str(submitted.get("input_file_id") or "")
            submitted_blocks.extend(remaining)
            ledger.record_submitted(
                batch_id,
                wave=attempt,
                chapter_ids=list(remaining),
                input_file_id=input_file_id,
            )
            entry = {
                "batch_id": batch_id,
                "status": "submitted",
                "output_file_id": None,
                "error_file_id": None,
            }
            retryable, failed, still_open, batch_log = _settle_batch(
                client,
                ledger,
                entry,
                list(remaining),
                work_dir=work_dir,
                prep_cfg=prep_cfg,
                volume_id=volume_id,
                model=model,
                poll_seconds=poll_seconds,
                deadline_seconds=deadline_seconds,
                attempt=attempt,
            )
            cache_log.extend(batch_log)
            permanent.update(failed)
            artifacts.update(_load_artifacts(work_dir, remaining))
            remaining = [name for name in remaining if name in retryable or name not in artifacts]
            if still_open:
                raise OpenAIPrepError(
                    f"OpenAI Batch {still_open} is still running; its accepted requests are "
                    f"recorded in {ledger.path}. Re-run after completion to recover them."
                )
            if permanent:
                raise OpenAIPrepError(
                    "OpenAI Batch returned permanent failures: "
                    + "; ".join(f"{name} ({reason})" for name, reason in sorted(permanent.items()))
                )
            if remaining and attempt == max_attempts:
                raise OpenAIPrepError(
                    "OpenAI Batch prep exhausted retries for blocks: " + ", ".join(remaining)
                )
    elif remaining:
        sync_artifacts, sync_log = _run_sync_blocks(
            client,
            work_dir,
            prep_cfg,
            system_prompt,
            model,
            remaining,
            max_attempts=max(1, int(batch_cfg.get("max_attempts", 2) or 2)),
        )
        artifacts.update(sync_artifacts)
        cache_log.extend(sync_log)

    missing = [name for name in MULTITURN_BLOCK_ORDER if name not in artifacts]
    if missing:
        raise OpenAIPrepError(
            "OpenAI prep cannot assemble context.xml; missing blocks: " + ", ".join(missing)
        )

    batch_state = str(_state_path(work_dir, batch_cfg)) if use_batch else ""
    return _assemble_and_finalize(
        work_dir,
        existing_context_xml,
        chapters,
        artifacts,
        resolved_series_id=resolved_series_id,
        bible_match=bible_match,
        generated_by="openai_prep_batch" if use_batch else "openai_prep",
        receipt_extra={
            "model": model,
            "mode": "batch" if use_batch else "responses",
            "batch_endpoint": _BATCH_ENDPOINT if use_batch else "",
            "blocks_submitted": submitted_blocks,
            "block_count": len(MULTITURN_BLOCK_ORDER),
            "prompt_signature": _prompt_signature(model, str(client_cfg.get("endpoint")), system_prompt),
            "batch_state_path": batch_state,
            "calls": cache_log,
        },
    )
