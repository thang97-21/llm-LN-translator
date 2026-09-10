"""Anthropic Message Batches API — submit/poll/retrieve for volume-level batch translation.

Opt-in via ``translation.anthropic.batch.enabled``; the default translation
path remains synchronous per-chapter (see ``agent.py::translate_volume``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.Anthropic.response import as_dict, response_to_llm_response
from src.Deepseek.common.atomic_io import atomic_write_json
from src.Deepseek.common.llm_types import LLMResponse

logger = logging.getLogger(__name__)

# Result types that mean "no answer, but asking again is legitimate": the
# request we built was well-formed, the job simply did not deliver it.
_RETRYABLE_RESULT_TYPES = ("expired", "canceled")


@dataclass
class BatchOutcome:
    """One batch job's results, partitioned by what has to happen next.

    The distinction is not bookkeeping. ``expired`` (the 24-hour window closed
    with the item unprocessed) and a server-side ``errored`` are safe to
    resubmit unchanged; an ``errored`` item whose error type is
    ``invalid_request`` is a fault in the request we assembled, and
    resubmitting it verbatim spends the money again for the identical
    failure. Collapsing both into one dropped warning — as this module did
    before — leaves a permanently broken chapter indistinguishable from a
    merely unlucky one, and leaves the operator no basis for deciding which
    to re-run.
    """

    succeeded: Dict[str, LLMResponse] = field(default_factory=dict)
    retryable: Dict[str, str] = field(default_factory=dict)
    permanent: Dict[str, str] = field(default_factory=dict)

    @property
    def unfinished(self) -> Dict[str, str]:
        """Every custom_id that produced no usable message, with its reason."""
        return {**self.retryable, **self.permanent}


class BatchLedger:
    """Durable record of the batch jobs submitted for one volume.

    A batch is billed when it is accepted, may run for up to 24 hours, and its
    results stay retrievable for 29 days. Holding the id only in the poll
    loop's local variable meant a killed process bought a job nobody could
    ever collect — still charged, results sitting behind an id that existed
    nowhere but in memory. config.yaml has declared
    translation.anthropic.batch.persistence_file since this route was written;
    nothing read it until now.

    Recording is deliberately eager: the entry is written the instant the id
    comes back, before the poll loop starts, because the window this protects
    against is precisely the one where the process does not survive to write
    anything later.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, path: Path, *, volume_id: str, model: str):
        self.path = Path(path)
        self.volume_id = str(volume_id)
        self.model = str(model)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict[str, Any] = {
            "schema_version": self.SCHEMA_VERSION,
            "volume_id": self.volume_id,
            "model": self.model,
            "batches": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("[ANTHROPIC-BATCH] unreadable batch state at %s; starting a fresh ledger", self.path)
            return
        # Same discipline as the conversation ledger: state recorded under a
        # different volume or model describes jobs whose results cannot be
        # used here, so it is not adopted.
        if (
            isinstance(loaded, dict)
            and str(loaded.get("volume_id")) == self.volume_id
            and str(loaded.get("model")) == self.model
        ):
            self.state = loaded
            self.state.setdefault("batches", [])

    def _save(self) -> None:
        atomic_write_json(self.path, self.state)

    @property
    def batches(self) -> List[Dict[str, Any]]:
        return self.state.setdefault("batches", [])

    def _find(self, batch_id: str) -> Optional[Dict[str, Any]]:
        return next((entry for entry in self.batches if entry.get("batch_id") == batch_id), None)

    def record_submitted(self, batch_id: str, *, wave: int, chapter_ids: List[str]) -> None:
        self.batches.append(
            {
                "batch_id": batch_id,
                "wave": int(wave),
                "chapter_ids": list(chapter_ids),
                "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "submitted",
                "resolved_at": None,
                "note": "",
            }
        )
        self._save()
        logger.info("[ANTHROPIC-BATCH] recorded batch_id=%s (wave %d, %d chapters) in %s",
                    batch_id, wave, len(chapter_ids), self.path)

    def record_ended(self, batch_id: str) -> None:
        self._resolve(batch_id, "ended", "")

    def record_abandoned(self, batch_id: str, note: str) -> None:
        self._resolve(batch_id, "abandoned", note)

    def _resolve(self, batch_id: str, status: str, note: str) -> None:
        entry = self._find(batch_id)
        if entry is None:
            return
        entry["status"] = status
        entry["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["note"] = note
        self._save()

    def open_batches(self) -> List[Dict[str, Any]]:
        """Submitted jobs never resolved, oldest wave first — so recovered
        results commit to the conversation ledger in the order they would have
        arrived had the run not been interrupted."""
        return sorted(
            (entry for entry in self.batches if entry.get("status") == "submitted"),
            key=lambda entry: (int(entry.get("wave") or 0), str(entry.get("submitted_at") or "")),
        )


def submit_batch(client, requests: List[Dict[str, Any]]) -> str:
    """Submit one Message Batches job (one request per chapter). Returns the batch id."""
    batch = client._client.messages.batches.create(requests=requests)
    batch_dict = as_dict(batch)
    return str(batch_dict.get("id") or "")


def poll_batch(client, batch_id: str) -> Dict[str, Any]:
    """One non-blocking status check. The caller owns the poll loop's sleep —
    this function never blocks on its own."""
    batch = client._client.messages.batches.retrieve(batch_id)
    return as_dict(batch)


def retrieve_batch_results(client, batch_id: str, *, model: str) -> BatchOutcome:
    """Decode every JSONL result line, keyed by chapter_id (the request's
    custom_id) and sorted into succeeded / retryable / permanent.

    Successes are decoded into the same ``LLMResponse`` shape the synchronous
    path produces — including ``provider_metadata["raw_content"]``, which is
    what lets a batch result be committed to the conversation ledger exactly
    as a synchronous one is.

    Results arrive in arbitrary order; nothing here depends on position.
    """
    outcome = BatchOutcome()
    for entry in client._client.messages.batches.results(batch_id):
        payload = as_dict(entry)
        custom_id = str(payload.get("custom_id") or "")
        if not custom_id:
            logger.warning("[ANTHROPIC-BATCH] %s: result line with no custom_id; cannot attribute it", batch_id)
            continue
        result = as_dict(payload.get("result") or {})
        result_type = str(result.get("type") or "unknown")

        if result_type == "succeeded":
            message = result.get("message") or {}
            # batch=True so the decoded LLMResponse carries the halved
            # Batch API cost, which is what agent._log_usage forwards
            # to the token ledger.
            outcome.succeeded[custom_id] = response_to_llm_response(
                message, model=model, streamed=False, batch=True,
                cache_ttl=getattr(client, "cache_ttl", "5m"),
            )
            continue

        if result_type == "errored":
            error = as_dict(result.get("error") or {})
            error_type = str(error.get("type") or "unknown")
            detail = str(error.get("message") or "").strip()
            reason = f"errored/{error_type}" + (f": {detail}" if detail else "")
            if error_type == "invalid_request":
                # Our request is malformed. Re-running it unchanged repeats
                # the failure at full cost, so it must not be swept into the
                # retry set.
                outcome.permanent[custom_id] = reason
                logger.error("[ANTHROPIC-BATCH] %s %s — fix the request before resubmitting", custom_id, reason)
            else:
                outcome.retryable[custom_id] = reason
                logger.warning("[ANTHROPIC-BATCH] %s %s — safe to resubmit", custom_id, reason)
            continue

        if result_type in _RETRYABLE_RESULT_TYPES:
            outcome.retryable[custom_id] = result_type
            logger.warning("[ANTHROPIC-BATCH] %s %s — safe to resubmit", custom_id, result_type)
            continue

        # An unrecognised type is treated as retryable rather than dropped:
        # a newly added result type should surface as work still to do, not
        # vanish into a completed manifest.
        outcome.retryable[custom_id] = f"unrecognized/{result_type}"
        logger.warning("[ANTHROPIC-BATCH] %s unrecognized result type %r; treating as unfinished", custom_id, result_type)

    logger.info(
        "[ANTHROPIC-BATCH] %s decoded: %d succeeded, %d retryable, %d permanent",
        batch_id, len(outcome.succeeded), len(outcome.retryable), len(outcome.permanent),
    )
    return outcome
