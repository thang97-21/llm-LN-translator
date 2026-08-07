"""QwenCloud error classification and retry policy.

Maps official QwenCloud / Anthropic-compatible failure modes into MTLS control
flow without depending on DeepSeek client internals.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class QwenAPIError(RuntimeError):
    """Base class for Qwen transport failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or "qwen_api_error"


class QwenModerationError(QwenAPIError):
    """Content moderation / data inspection failure (HTTP 400 family)."""


class QwenRateLimitError(QwenAPIError):
    """Rate limit / overload (HTTP 429 or provider overload)."""


class QwenAuthError(QwenAPIError):
    """Authentication / authorization failure (HTTP 401/403)."""


class QwenInvalidRequestError(QwenAPIError):
    """Malformed request that should not be retried."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_ms: int = 500
    max_delay_ms: int = 32000
    jitter_factor: float = 0.25
    max_429_retries: int = 3


def classify_exception(exc: BaseException) -> QwenAPIError:
    """Normalize SDK/HTTP exceptions into Qwen error types."""
    status = _extract_status(exc)
    text = str(exc)
    lower = text.lower()
    code = _extract_code(exc, lower)

    if status in {401, 403} or "authentication" in lower or "invalid x-api-key" in lower:
        return QwenAuthError(text, status_code=status, code=code or "authentication_error")
    if status == 429 or "rate_limit" in lower or "rate limit" in lower or "overloaded" in lower:
        return QwenRateLimitError(text, status_code=status or 429, code=code or "rate_limit_error")
    if status == 400 and (
        "data_inspection_failed" in lower
        or "datainspectionfailed" in lower
        or "ip_infringement" in lower
        or "ipinfringementsuspect" in lower
        or "inappropriate content" in lower
        or "custom_role_blocked" in lower
        or "faq_rule_blocked" in lower
        or "green network verification" in lower
    ):
        return QwenModerationError(text, status_code=400, code=code or "data_inspection_failed")
    if status == 400 or "invalid_request" in lower or "field required" in lower:
        return QwenInvalidRequestError(text, status_code=status or 400, code=code or "invalid_request_error")
    return QwenAPIError(text, status_code=status, code=code or "qwen_api_error")


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (QwenAuthError, QwenInvalidRequestError, QwenModerationError)):
        return False
    if isinstance(exc, QwenRateLimitError):
        return True
    if isinstance(exc, QwenAPIError):
        return exc.status_code in {None, 408, 409, 425, 429, 500, 502, 503, 504, 529}
    status = _extract_status(exc)
    return status in {None, 408, 409, 425, 429, 500, 502, 503, 504, 529}


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Execute *fn* with exponential backoff for retryable Qwen failures."""
    rate_limit_attempts = 0
    last_exc: Optional[BaseException] = None
    for attempt in range(policy.max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - boundary normalizes all transport failures
            classified = classify_exception(exc)
            last_exc = classified
            if attempt >= policy.max_retries or not is_retryable(classified):
                raise classified from exc
            if isinstance(classified, QwenRateLimitError):
                rate_limit_attempts += 1
                if rate_limit_attempts > policy.max_429_retries:
                    raise classified from exc
            delay = _backoff_seconds(attempt, policy)
            if on_retry is not None:
                on_retry(attempt + 1, classified, delay)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _backoff_seconds(attempt: int, policy: RetryPolicy) -> float:
    base = policy.base_delay_ms / 1000.0
    delay = min(policy.max_delay_ms / 1000.0, base * (2 ** attempt))
    if policy.jitter_factor > 0:
        delay *= 1.0 + random.uniform(-policy.jitter_factor, policy.jitter_factor)
    return max(0.05, delay)


def _extract_status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(error, dict) and isinstance(error.get("status"), int):
            return int(error["status"])
    return None


def _extract_code(exc: BaseException, lower_text: str) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(error, dict):
            code = error.get("type") or error.get("code")
            if code:
                return str(code)
    for token in (
        "data_inspection_failed",
        "ip_infringement_suspect",
        "rate_limit_error",
        "authentication_error",
        "invalid_request_error",
        "overloaded_error",
    ):
        if token in lower_text:
            return token
    return ""
