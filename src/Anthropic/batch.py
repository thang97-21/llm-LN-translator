"""Anthropic Message Batches API — submit/poll/retrieve for volume-level batch translation.

Opt-in via ``translation.anthropic.batch.enabled``; the default translation
path remains synchronous per-chapter (see ``agent.py::translate_volume``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.Anthropic.response import as_dict, response_to_llm_response
from src.Deepseek.common.llm_types import LLMResponse

logger = logging.getLogger(__name__)


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


def retrieve_batch_results(client, batch_id: str, *, model: str) -> Dict[str, LLMResponse]:
    """Decode every JSONL result line into the same ``LLMResponse`` shape the
    synchronous path produces, keyed by chapter_id (the request's custom_id)."""
    results: Dict[str, LLMResponse] = {}
    for entry in client._client.messages.batches.results(batch_id):
        payload = as_dict(entry)
        custom_id = str(payload.get("custom_id") or "")
        if not custom_id:
            continue
        result = as_dict(payload.get("result") or {})
        result_type = str(result.get("type") or "")
        if result_type != "succeeded":
            logger.warning("[ANTHROPIC-BATCH] %s result type=%s (not succeeded)", custom_id, result_type or "unknown")
            continue
        message = result.get("message") or {}
        results[custom_id] = response_to_llm_response(message, model=model, streamed=False)
    return results
