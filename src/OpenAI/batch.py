"""OpenAI Batch API helpers for volume-scale Responses translation.

The Batch API is file-backed and asynchronous, unlike the synchronous
Responses route. This module keeps the transport details small and explicit so
``OpenAITranslator`` can reuse its persistent conversation ledger and commit
results in chapter order.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.llm_types import LLMResponse
from src.OpenAI.errors import OpenAIAPIError
from src.OpenAI.response import as_dict, response_to_llm_response

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = {"expired", "cancelled", "canceled"}
_PERMANENT_ERROR_TYPES = {"invalid_request", "invalid_request_error", "authentication_error", "permission_error"}
_MAX_BATCH_REQUESTS = 50_000
_MAX_BATCH_BYTES = 200 * 1024 * 1024


@dataclass
class BatchOutcome:
    """Partition one Batch job into results that can be committed or retried."""

    succeeded: Dict[str, LLMResponse] = field(default_factory=dict)
    retryable: Dict[str, str] = field(default_factory=dict)
    permanent: Dict[str, str] = field(default_factory=dict)
    fallback_eligible: Dict[str, str] = field(default_factory=dict)

    @property
    def unfinished(self) -> Dict[str, str]:
        return {**self.retryable, **self.permanent}


class BatchLedger:
    """Durable record of accepted Batch jobs for one volume and model."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, path: Path, *, volume_id: str, model: str, endpoint: str = "/v1/responses"):
        self.path = Path(path)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.endpoint = str(endpoint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "endpoint": self.endpoint,
            "batches": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[OPENAI-BATCH] unreadable batch state at %s; starting fresh", self.path)
            return
        if (
            isinstance(loaded, dict)
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
            and str(loaded.get("endpoint", self.endpoint)) == self.endpoint
        ):
            self.state = loaded
            self.state.setdefault("batches", [])

    @property
    def batches(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("batches", [])

    def _find(self, batch_id: str) -> Optional[Dict[str, Any]]:
        return next((entry for entry in self.batches if entry.get("batch_id") == batch_id), None)

    def record_submitted(
        self,
        batch_id: str,
        *,
        wave: int,
        chapter_ids: List[str],
        input_file_id: str,
    ) -> None:
        self.batches.append(
            {
                "batch_id": str(batch_id),
                "input_file_id": str(input_file_id),
                "wave": int(wave),
                "chapter_ids": list(chapter_ids),
                "submitted_at": _utc_now(),
                "status": "submitted",
                "resolved_at": None,
                "output_file_id": None,
                "error_file_id": None,
                "note": "",
            }
        )
        self._save()
        logger.info(
            "[OPENAI-BATCH] recorded batch_id=%s (wave %d, %d chapters) in %s",
            batch_id,
            wave,
            len(chapter_ids),
            self.path,
        )

    def record_ended(
        self,
        batch_id: str,
        *,
        status: str = "ended",
        output_file_id: str = "",
        error_file_id: str = "",
        note: str = "",
    ) -> None:
        entry = self._find(batch_id)
        if entry is None:
            return
        entry["status"] = str(status)
        entry["resolved_at"] = _utc_now()
        if output_file_id:
            entry["output_file_id"] = str(output_file_id)
        if error_file_id:
            entry["error_file_id"] = str(error_file_id)
        entry["note"] = str(note)
        self._save()

    def record_abandoned(self, batch_id: str, note: str) -> None:
        self.record_ended(batch_id, status="abandoned", note=note)

    def open_batches(self) -> List[Dict[str, Any]]:
        return sorted(
            (entry for entry in self.batches if entry.get("status") == "submitted"),
            key=lambda entry: (int(entry.get("wave") or 0), str(entry.get("submitted_at") or "")),
        )

    def recoverable_batches(self) -> List[Dict[str, Any]]:
        """Return submitted or ended jobs whose result files are persisted.

        A process can die after ``record_ended`` but before chapter output and
        manifest commits. Keeping those entries recoverable avoids creating a
        second billable job merely because the first one already finished.
        """
        return sorted(
            (
                entry
                for entry in self.batches
                if entry.get("status") == "submitted"
                or entry.get("output_file_id")
                or entry.get("error_file_id")
            ),
            key=lambda entry: (int(entry.get("wave") or 0), str(entry.get("submitted_at") or "")),
        )

    def _save(self) -> None:
        atomic_write_json(self.path, self.state)


def submit_batch(
    client,
    requests: List[Dict[str, Any]],
    *,
    completion_window: str = "24h",
    metadata: Optional[Dict[str, str]] = None,
    output_expires_after: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upload JSONL and create one ``/v1/responses`` Batch job."""
    if not requests:
        raise ValueError("OpenAI Batch requires at least one request")
    if len(requests) > _MAX_BATCH_REQUESTS:
        raise ValueError(
            f"OpenAI Batch accepts at most {_MAX_BATCH_REQUESTS:,} requests per input file"
        )
    payload = "".join(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n" for request in requests)
    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) > _MAX_BATCH_BYTES:
        raise ValueError(
            "OpenAI Batch input JSONL exceeds the 200 MB file limit; "
            "reduce batch.wave_size"
        )
    file_obj = io.BytesIO(payload_bytes)
    file_obj.name = "mtls_openai_batch.jsonl"
    uploaded = client._client.files.create(file=file_obj, purpose="batch")
    uploaded_dict = as_dict(uploaded)
    input_file_id = str(uploaded_dict.get("id") or "")
    if not input_file_id:
        raise OpenAIAPIError("OpenAI Batch upload returned no input file id.", code="batch_upload_error")

    kwargs: Dict[str, Any] = {
        "input_file_id": input_file_id,
        "endpoint": "/v1/responses",
        "completion_window": str(completion_window or "24h"),
    }
    if metadata:
        kwargs["metadata"] = {str(key): str(value) for key, value in metadata.items()}
    if output_expires_after:
        kwargs["output_expires_after"] = dict(output_expires_after)
    batch = client._client.batches.create(**kwargs)
    batch_dict = as_dict(batch)
    batch_id = str(batch_dict.get("id") or "")
    if not batch_id:
        raise OpenAIAPIError("OpenAI Batch creation returned no batch id.", code="batch_create_error")
    batch_dict.setdefault("input_file_id", input_file_id)
    return batch_dict


def poll_batch(client, batch_id: str) -> Dict[str, Any]:
    return as_dict(client._client.batches.retrieve(str(batch_id)))


def retrieve_batch_results(
    client,
    batch: Dict[str, Any],
    *,
    model: str,
    expected_ids: Optional[Iterable[str]] = None,
) -> BatchOutcome:
    """Decode the output/error JSONL files into canonical ``LLMResponse`` objects."""
    outcome = BatchOutcome()
    batch_dict = as_dict(batch)
    batch_id = str(batch_dict.get("id") or batch_dict.get("batch_id") or "")
    output_file_id = str(batch_dict.get("output_file_id") or "")
    error_file_id = str(batch_dict.get("error_file_id") or "")
    seen: set[str] = set()

    if output_file_id:
        for entry in _read_jsonl(client, output_file_id):
            custom_id = str(entry.get("custom_id") or "")
            if not custom_id:
                logger.warning("[OPENAI-BATCH] %s: output line without custom_id", batch_id)
                continue
            seen.add(custom_id)
            response_payload = as_dict(entry.get("response") or {})
            status_code = _as_int(response_payload.get("status_code"), 200)
            body = response_payload.get("body")
            body_dict = as_dict(body if body is not None else response_payload)
            if status_code >= 400:
                _classify_error(outcome, custom_id, status_code, body_dict)
                continue
            outcome.succeeded[custom_id] = response_to_llm_response(
                body_dict,
                model=model,
                streamed=False,
                batch=True,
            )

    if error_file_id:
        for entry in _read_jsonl(client, error_file_id):
            custom_id = str(entry.get("custom_id") or "")
            if not custom_id:
                logger.warning("[OPENAI-BATCH] %s: error line without custom_id", batch_id)
                continue
            seen.add(custom_id)
            error = as_dict(entry.get("error") or entry.get("response") or {})
            status_code = _as_int(error.get("status_code") or error.get("code"), 500)
            _classify_error(outcome, custom_id, status_code, error)

    for custom_id in expected_ids or []:
        key = str(custom_id)
        if key not in seen and key not in outcome.succeeded:
            outcome.retryable[key] = "missing_result"
            logger.warning("[OPENAI-BATCH] %s missing result for %s", batch_id, key)

    logger.info(
        "[OPENAI-BATCH] %s decoded: %d succeeded, %d retryable, %d permanent",
        batch_id,
        len(outcome.succeeded),
        len(outcome.retryable),
        len(outcome.permanent),
    )
    return outcome


def batch_is_terminal(status: str) -> bool:
    return str(status or "").strip().lower() in {
        "completed",
        "failed",
        "expired",
        "cancelled",
        "canceled",
    }


def batch_status_retryable(status: str) -> bool:
    return str(status or "").strip().lower() in _RETRYABLE_STATUSES


def _classify_error(outcome: BatchOutcome, custom_id: str, status_code: int, payload: Dict[str, Any]) -> None:
    error_type = str(payload.get("type") or payload.get("code") or "unknown").strip().lower()
    message = str(payload.get("message") or payload.get("error") or "").strip()
    reason = f"{status_code}/{error_type}" + (f": {message}" if message else "")
    access_text = f"{error_type} {message.lower()}"
    access_error = any(
        marker in access_text
        for marker in (
            "model_not_found",
            "model not found",
            "model_not_available",
            "model not available",
            "model_not_permitted",
            "model not permitted",
            "model_access_denied",
            "model access denied",
            "model_not_enabled",
            "model not enabled",
            "unsupported_model",
            "unsupported model",
            "unsupported_region",
            "unsupported region",
            "requires_trusted_access",
            "requires trusted access",
        )
    )
    if access_error or error_type in _PERMANENT_ERROR_TYPES or 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}:
        outcome.permanent[custom_id] = reason
        if access_error:
            outcome.fallback_eligible[custom_id] = reason
        logger.error("[OPENAI-BATCH] %s %s — not resubmitting unchanged", custom_id, reason)
        return
    outcome.retryable[custom_id] = reason
    logger.warning("[OPENAI-BATCH] %s %s — safe to resubmit", custom_id, reason)


def _read_jsonl(client, file_id: str) -> Iterable[Dict[str, Any]]:
    raw = client._client.files.content(str(file_id))
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    elif isinstance(raw, str):
        text = raw
    elif hasattr(raw, "text"):
        text = str(raw.text)
    elif hasattr(raw, "content"):
        content = raw.content
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
    elif hasattr(raw, "read"):
        content = raw.read()
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
    else:
        text = str(raw)
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("[OPENAI-BATCH] ignoring malformed JSONL result line")
            continue
        if isinstance(parsed, dict):
            yield parsed


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
