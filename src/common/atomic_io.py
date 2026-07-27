"""Atomic, durable file writes for MTLS pipeline artifacts.

Replaces non-atomic ``open(path, 'w')`` / ``Path.write_text()`` writes that could
leave a truncated file if the process was interrupted or the OS page cache was
synced before the bytes were flushed (root cause of the manifest/context.xml/EN
truncation incidents). Strategy: write to a temp file in the *same* directory,
``flush()`` + ``os.fsync()`` it, then ``os.replace()`` it into place atomically.
"""
import json
import os
import stat
import tempfile
import time
from typing import Any, Union

__all__ = ["atomic_write_text", "atomic_write_json"]

_REPLACE_RETRY_ATTEMPTS = 12
_REPLACE_RETRY_INITIAL_DELAY_SECONDS = 0.05
_REPLACE_RETRY_MAX_DELAY_SECONDS = 1.0
_RETRYABLE_WINERRORS = {5, 32, 33}


def _is_retryable_replace_error(exc: OSError) -> bool:
    """Return True for transient replace errors, especially Windows file locks."""
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror in _RETRYABLE_WINERRORS:
        return True
    return exc.errno in {13, 16}


def _clear_readonly_bit(path: str) -> None:
    """Best-effort clear of a read-only destination before retrying replace."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & stat.S_IWRITE:
        return
    try:
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


def _replace_with_retries(src: str, dst: str) -> None:
    """Replace ``dst`` with ``src``, retrying transient Windows lock failures."""
    delay = _REPLACE_RETRY_INITIAL_DELAY_SECONDS
    for attempt in range(1, _REPLACE_RETRY_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt == 1:
                _clear_readonly_bit(dst)
            if attempt >= _REPLACE_RETRY_ATTEMPTS or not _is_retryable_replace_error(exc):
                raise OSError(
                    exc.errno,
                    (
                        f"atomic replace failed after {attempt} attempt(s) for "
                        f"{dst!r}: {exc}"
                    ),
                ) from exc
            time.sleep(delay)
            delay = min(delay * 1.7, _REPLACE_RETRY_MAX_DELAY_SECONDS)


def atomic_write_text(
    path: Union[str, "os.PathLike[str]"],
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str = "\n",
) -> None:
    """Durably write ``text`` to ``path`` (temp -> flush -> fsync -> os.replace)."""
    p = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(p))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retries(tmp, p)  # atomic rename on the same filesystem
        try:                # best-effort: persist the directory entry too
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(
    path: Union[str, "os.PathLike[str]"],
    obj: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    newline: str = "\n",
) -> None:
    """Serialize ``obj`` to JSON and write it atomically/durably."""
    atomic_write_text(
        path,
        json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii),
        newline=newline,
    )
