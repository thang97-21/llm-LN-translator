"""
OS Detection + Platform-Specific Configuration Resolver.

Detects the running platform (Windows / macOS / Linux) and resolves the
appropriate OS block from config.yaml at load time.  The resolved block is
merged over the shared top-level keys (directories, paths, env vars) so that
platform-specific overrides take effect without duplicating the entire config.

Usage
-----
    from src.common.os_config import resolve_os_config

    cfg = resolve_os_config()          # full os_detection block dict
    platform = get_current_platform()   # "windows" | "macos" | "linux"

    dirs    = get_os_directories()      # resolved directories dict
    paths   = get_os_paths()            # resolved paths dict
    env_map = get_os_env()              # env-var name → value dict

The functions also expand ~ (home) and %VAR% / $VAR environment-variable
references in string values so that paths are immediately usable with
``pathlib.Path`` etc.

Integration
-----------
``pipeline/config.py`` calls ``resolve_os_config()`` inside ``load_config()``
before returning the merged config dict.  No other phase module needs to
call this directly — all directory / path access should go through the
constants and helpers already defined in ``pipeline/config.py``.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ──────────────────────────────────────────────────────────────────────────────
# Internal cache (mirrors the _config_cache pattern in config.py)
# ──────────────────────────────────────────────────────────────────────────────

_os_config_cache: Optional[Dict[str, Any]] = None

# Supported platform keys in config.yaml
_SUPPORTED_PLATFORMS = ("windows", "macos", "linux")

# Order of detection (first match wins)
_PLATFORM_CHECKS = (
    ("windows", lambda: sys.platform == "win32"),
    ("macos",   lambda: sys.platform == "darwin"),
    ("linux",   lambda: sys.platform.startswith("linux")),
)


# ──────────────────────────────────────────────────────────────────────────────
# Core detection
# ──────────────────────────────────────────────────────────────────────────────

def get_current_platform() -> str:
    """
    Return the normalised platform identifier.

    Returns
    -------
    "windows" | "macos" | "linux"
    """
    for name, check in _PLATFORM_CHECKS:
        if check():
            return name
    # Fallback — unlikely to reach here on a modern OS
    return "linux"


def _detect_platform() -> str:
    """Alias for get_current_platform — kept for internal use."""
    return get_current_platform()


# ──────────────────────────────────────────────────────────────────────────────
# Environment-variable expansion
# ──────────────────────────────────────────────────────────────────────────────

# %VAR%  — Windows-style
_WINDOWS_VAR_RE = re.compile(r"%([^%]+)%")

# $VAR   — Unix-style (also handles ${VAR})
_UNIX_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _expand_env(value: str) -> str:
    """
    Recursively expand both ``%VAR%`` (Windows) and ``$VAR`` / ``${VAR}``
    (Unix) environment-variable references in *value*.

    Unresolved references are left as-is so that explicit empty strings and
    literal percent/dollar signs are not accidentally stripped.
    """
    def _replace_win(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(0))

    def _replace_unix(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(0))

    prev = ""
    while prev != value:
        prev = value
        value = _WINDOWS_VAR_RE.sub(_replace_win, value)
        value = _UNIX_VAR_RE.sub(_replace_unix, value)

    # Expand ~ to home directory (Path.resolve handles this, but we do it
    # here so that plain strings returned by helpers are also expanded)
    if value.startswith("~"):
        value = str(Path(value).expanduser())

    return value


def _expand_dict_strings(d: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively expand env vars in all string values of *d*."""
    result: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _expand_env(v)
        elif isinstance(v, dict):
            result[k] = _expand_dict_strings(v)
        elif isinstance(v, list):
            result[k] = [
                _expand_env(item) if isinstance(item, str) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Config loading + resolution
# ──────────────────────────────────────────────────────────────────────────────

def _get_yaml_config() -> Dict[str, Any]:
    """
    Load config.yaml once (uses the same PIPELINE_ROOT resolution as
    pipeline/config.py to avoid import-cycle issues).
    """
    # Resolve relative to the pipeline root — two levels up from common/
    pipeline_root = Path(__file__).parent.parent.parent.resolve()
    config_path = pipeline_root / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return raw


def resolve_os_config() -> Dict[str, Any]:
    """
    Load ``os_detection`` from config.yaml, detect the current platform, and
    return the appropriate platform block with environment variables expanded.

    The returned dict contains the keys defined under the matching
    ``os_detection.<platform>`` key:

        {
            "env_prefix":     "",
            "path_separator": ";",
            "line_ending":    "\r\n",
            "directories": { ... },
            "paths":       { ... },
            "env":         { ... },
        }

    Returns
    -------
    Dict[str, Any]
        The matched platform block, or an empty dict if ``os_detection`` is
        absent from config.yaml (backwards-compatible).
    """
    global _os_config_cache

    if _os_config_cache is not None:
        return _os_config_cache

    raw = _get_yaml_config()
    os_detection = raw.get("os_detection", {})

    if not os_detection.get("enabled", False):
        _os_config_cache = {}
        return _os_config_cache

    detected = get_current_platform()

    # Look for an exact platform match first, then fall back to 'linux'
    # which is the safest unix-like assumption.
    platform_block: Dict[str, Any] = (
        os_detection.get(detected)
        or os_detection.get("linux", {})
    )

    # Expand %VAR% / $VAR / ~ in all string values
    _os_config_cache = _expand_dict_strings(platform_block)
    return _os_config_cache


def get_os_directories() -> Dict[str, str]:
    """
    Return the OS-specific ``directories`` block with env vars expanded.

    Keys: ``input``, ``work``, ``output``, ``prompts``, ``modules``,
          ``templates``, ``bibles``.
    """
    os_cfg = resolve_os_config()
    return dict(os_cfg.get("directories", {}))


def get_os_paths() -> Dict[str, str]:
    """
    Return the OS-specific ``paths`` block with env vars expanded.

    Keys: ``cache_dir``, ``chroma_db``, ``log_dir``, ``temp_dir``.
    """
    os_cfg = resolve_os_config()
    return dict(os_cfg.get("paths", {}))


def get_os_env() -> Dict[str, str]:
    """
    Return the OS-specific ``env`` block — a dict of env-var name → value.

    These are NOT automatically set in ``os.environ``; callers should
    call ``os.environ.update(get_os_env())`` if they want that behaviour.
    """
    os_cfg = resolve_os_config()
    return dict(os_cfg.get("env", {}))


def get_os_line_ending() -> str:
    """Return ``\\r\\n`` on Windows, ``\\n`` everywhere else."""
    os_cfg = resolve_os_config()
    return os_cfg.get("line_ending", "\n")


def get_os_path_separator() -> str:
    """Return ``;`` on Windows, ``:`` everywhere else."""
    os_cfg = resolve_os_config()
    return os_cfg.get("path_separator", ":")


def clear_os_config_cache() -> None:
    """Clear the internal OS config cache. Exposed for testing."""
    global _os_config_cache
    _os_config_cache = None
