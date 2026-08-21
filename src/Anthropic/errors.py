"""Anthropic Messages API error classification and bounded retry policy."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class AnthropicAPIError(RuntimeError):
    """Base class for normalized Anthropic transport failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or "anthropic_api_error"


class AnthropicRateLimitError(AnthropicAPIError):
    """``rate_limit_error`` / HTTP 429."""


class AnthropicOverloadedError(AnthropicAPIError):
    """``overloaded_error`` / HTTP 529 — Anthropic's distinct transient-capacity signal."""


class AnthropicAuthError(AnthropicAPIError):
    """``authentication_error`` / ``permission_error``."""


class AnthropicInvalidRequestError(AnthropicAPIError):
    """``invalid_request_error`` / ``not_found_error`` — not retryable."""


class AnthropicRefusalError(AnthropicAPIError):
    """A completed transport response whose ``stop_reason`` is a model refusal."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    base_delay_ms: int = 500
    max_delay_ms: int = 32_000
    jitter_factor: float = 0.25
    max_429_retries: int = 3
    # Anthropic's overloaded_error/529 is tracked separately from a generic
    # retry ceiling, same reasoning DeepSeek's config already applies to its
    # own max_529_retries: an overload storm should back off harder than an
    # ordinary transient failure, not just retry max_retries times blindly.
    max_529_retries: int = 3


def classify_exception(exc: BaseException) -> AnthropicAPIError:
    """Normalize SDK and HTTP errors without importing the SDK at module load."""
    if isinstance(exc, AnthropicAPIError):
        return exc
    status = _extract_status(exc)
    text = str(exc)
    lower = text.lower()
    code = _extract_code(exc, lower)
    if status == 429 or "rate_limit_error" in lower or "rate limit" in lower:
        return AnthropicRateLimitError(text, status_code=status, code=code or "rate_limit_error")
    if status == 529 or "overloaded_error" in lower or "overloaded" in lower:
        return AnthropicOverloadedError(text, status_code=status, code=code or "overloaded_error")
    if status in {401, 403} or "authentication_error" in lower or "permission_error" in lower:
        return AnthropicAuthError(text, status_code=status, code=code or "authentication_error")
    if status in {400, 404, 413, 422} or "invalid_request_error" in lower or "not_found_error" in lower:
        return AnthropicInvalidRequestError(text, status_code=status, code=code or "invalid_request_error")
    return AnthropicAPIError(text, status_code=status, code=code)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (AnthropicAuthError, AnthropicInvalidRequestError, AnthropicRefusalError)):
        return False
    status = _extract_status(exc)
    return status in {None, 408, 409, 425, 429, 500, 502, 503, 504, 529}


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute a request with exponential backoff and separate 429/529 ceilings."""
    attempts = 0
    rate_limit_attempts = 0
    overload_attempts = 0
    while True:
        try:
            return fn()
        except BaseException as exc:  # retry policy works on provider-normalized exceptions
            normalized = classify_exception(exc)
            if not is_retryable(normalized) or attempts >= max(0, policy.max_retries):
                raise normalized from exc
            if isinstance(normalized, AnthropicRateLimitError):
                rate_limit_attempts += 1
                if rate_limit_attempts > max(0, policy.max_429_retries):
                    raise normalized from exc
            if isinstance(normalized, AnthropicOverloadedError):
                overload_attempts += 1
                if overload_attempts > max(0, policy.max_529_retries):
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
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or "")
        return str(body.get("type") or "")
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    if "context_length" in lower_text or "prompt is too long" in lower_text:
        return "context_length_exceeded"
    return ""
