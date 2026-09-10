"""GLM/Z.AI error normalization and bounded retry policy."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class GLMAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or "glm_api_error"


class GLMRateLimitError(GLMAPIError):
    pass


class GLMAuthError(GLMAPIError):
    pass


class GLMInvalidRequestError(GLMAPIError):
    pass


class GLMModerationError(GLMAPIError):
    """A completed response stopped with Z.AI's ``sensitive`` reason."""


class GLMContextLimitError(GLMAPIError):
    pass


class GLMNetworkError(GLMAPIError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_ms: int = 500
    max_delay_ms: int = 32_000
    jitter_factor: float = 0.25
    max_429_retries: int = 3


def classify_exception(exc: BaseException) -> GLMAPIError:
    if isinstance(exc, GLMAPIError):
        return exc
    status = _status(exc)
    text = str(exc)
    lower = text.lower()
    code = _code(exc, lower)
    if status == 429 or "rate limit" in lower or "rate_limit" in lower:
        return GLMRateLimitError(text, status_code=status, code=code or "rate_limit")
    if status in {401, 403} or "authentication" in lower or "permission" in lower:
        return GLMAuthError(text, status_code=status, code=code or "authentication_error")
    if status in {400, 404, 405, 413, 422} or "invalid request" in lower or "bad request" in lower:
        return GLMInvalidRequestError(text, status_code=status, code=code or "invalid_request")
    if "context" in lower and ("length" in lower or "window" in lower or "token" in lower):
        return GLMContextLimitError(text, status_code=status, code=code or "context_length_exceeded")
    return GLMNetworkError(text, status_code=status, code=code or "network_error")


def classify_finish_reason(reason: str) -> Optional[GLMAPIError]:
    normalized = str(reason or "").strip().lower()
    if normalized == "sensitive":
        return GLMModerationError("GLM declined the chapter as sensitive content.", code="sensitive")
    if normalized == "model_context_window_exceeded":
        return GLMContextLimitError("GLM context window exceeded.", code=normalized)
    return None


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (GLMAuthError, GLMInvalidRequestError, GLMModerationError, GLMContextLimitError)):
        return False
    status = _status(exc)
    return status in {None, 408, 409, 425, 429, 500, 502, 503, 504} or isinstance(exc, GLMNetworkError)


def call_with_retry(fn: Callable[[], T], policy: RetryPolicy, *, on_retry: Optional[Callable[[int, BaseException, float], None]] = None, sleep: Callable[[float], None] = time.sleep) -> T:
    attempts = 0
    rate_limit_attempts = 0
    while True:
        try:
            return fn()
        except BaseException as exc:
            normalized = classify_exception(exc)
            if not is_retryable(normalized) or attempts >= max(0, policy.max_retries):
                raise normalized from exc
            if isinstance(normalized, GLMRateLimitError):
                rate_limit_attempts += 1
                if rate_limit_attempts > max(0, policy.max_429_retries):
                    raise normalized from exc
            attempts += 1
            delay = _backoff_seconds(attempts, policy)
            if on_retry:
                on_retry(attempts, normalized, delay)
            sleep(delay)


def _backoff_seconds(attempt: int, policy: RetryPolicy) -> float:
    base = max(0, policy.base_delay_ms) / 1000.0
    ceiling = max(base, policy.max_delay_ms / 1000.0)
    delay = min(ceiling, base * (2 ** max(0, attempt - 1)))
    jitter = delay * max(0.0, min(1.0, policy.jitter_factor))
    return max(0.05, delay + random.uniform(-jitter, jitter))


def _status(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _code(exc: BaseException, lower: str) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or error.get("type") or "")
        return str(body.get("code") or body.get("type") or "")
    code = getattr(exc, "code", None)
    return str(code) if code else ("context_length_exceeded" if "context" in lower and "length" in lower else "")
