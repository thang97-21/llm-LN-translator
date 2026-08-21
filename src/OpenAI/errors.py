"""OpenAI Responses API error classification and bounded retry policy."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class OpenAIAPIError(RuntimeError):
    """Base class for normalized OpenAI transport failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or "openai_api_error"


class OpenAIRateLimitError(OpenAIAPIError):
    """Rate limit or transient overload failure."""


class OpenAIAuthError(OpenAIAPIError):
    """Authentication or project-authorization failure."""


class OpenAIInvalidRequestError(OpenAIAPIError):
    """Malformed request that cannot become valid through retrying."""


class OpenAIRefusalError(OpenAIAPIError):
    """A completed transport response that contains a model refusal."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_ms: int = 500
    max_delay_ms: int = 32_000
    jitter_factor: float = 0.25
    max_429_retries: int = 3


def classify_exception(exc: BaseException) -> OpenAIAPIError:
    """Normalize SDK and HTTP errors without importing the SDK at module load."""
    if isinstance(exc, OpenAIAPIError):
        return exc
    status = _extract_status(exc)
    text = str(exc)
    lower = text.lower()
    code = _extract_code(exc, lower)
    if status == 429 or "rate limit" in lower or "rate_limit" in lower:
        return OpenAIRateLimitError(text, status_code=status, code=code or "rate_limit")
    if status in {401, 403} or "authentication" in lower or "permission" in lower:
        return OpenAIAuthError(text, status_code=status, code=code or "authentication_error")
    if status in {400, 404, 405, 413, 422} or "invalid request" in lower or "bad request" in lower:
        return OpenAIInvalidRequestError(text, status_code=status, code=code or "invalid_request")
    return OpenAIAPIError(text, status_code=status, code=code)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (OpenAIAuthError, OpenAIInvalidRequestError, OpenAIRefusalError)):
        return False
    status = _extract_status(exc)
    return status in {None, 408, 409, 425, 429, 500, 502, 503, 504}


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute a request with exponential backoff and a separate 429 ceiling."""
    attempts = 0
    rate_limit_attempts = 0
    while True:
        try:
            return fn()
        except BaseException as exc:  # retry policy works on provider-normalized exceptions
            normalized = classify_exception(exc)
            if not is_retryable(normalized) or attempts >= max(0, policy.max_retries):
                raise normalized from exc
            if isinstance(normalized, OpenAIRateLimitError):
                rate_limit_attempts += 1
                if rate_limit_attempts > max(0, policy.max_429_retries):
                    raise normalized from exc
            attempts += 1
            delay = _backoff_seconds(attempts, policy)
            if on_retry is not None:
                on_retry(attempts, normalized, delay)
            sleep(delay)


def _backoff_seconds(attempt: int, policy: RetryPolicy) -> float:
    base = max(0, policy.base_delay_ms) / 1000.0
    ceiling = max(base, policy.max_delay_ms / 1000.0)
    delay = min(ceiling, base * (2 ** max(0, attempt - 1)))
    jitter = delay * max(0.0, min(1.0, policy.jitter_factor))
    return max(0.05, delay + random.uniform(-jitter, jitter))


def _extract_status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_code(exc: BaseException, lower_text: str) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return str(body.get("code") or body.get("type") or "")
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    if "context_length" in lower_text:
        return "context_length_exceeded"
    return ""
