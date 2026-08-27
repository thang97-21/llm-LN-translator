"""
LLM Translator Base Configuration — minimal, DeepSeek-only.

Stripped from the main pipeline's config.py: only path constants, the YAML
loader, and the content-processing regex constants that Librarian + Builder
+ the bare DeepSeek translator actually touch. No provider routing, no
feature flags, no grammar RAG / vector store / world policy references.
"""

import copy
import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional, Sequence
import yaml

from dotenv import load_dotenv

from src.Deepseek.common.os_config import (
    get_current_platform,
    get_os_directories,
    get_os_paths,
    get_os_env,
)

# ============================================================================
# BASE PATHS
# ============================================================================

PIPELINE_ROOT = Path(__file__).parent.parent.parent.parent.resolve()

_env_path = PIPELINE_ROOT / ".env"
load_dotenv(_env_path)

# ── OS-aware directory resolution ───────────────────────────────────────────
# Falls back to the LLM Translator layout (raw/, work/, output/) when
# os_detection is absent from config.yaml — which it is by default here.
_os_dirs = get_os_directories()

INPUT_DIR     = PIPELINE_ROOT / _os_dirs.get("input", "raw/")
WORK_DIR      = PIPELINE_ROOT / _os_dirs.get("work", "work/")
OUTPUT_DIR    = PIPELINE_ROOT / _os_dirs.get("output", "output/")
TEMPLATES_DIR = PIPELINE_ROOT / _os_dirs.get("templates", "templates/")

# Resolved OS-specific runtime paths (cache, log, temp). Not created here —
# callers use them directly.
OS_PATHS = get_os_paths()

_os_env = get_os_env()
for _key, _value in _os_env.items():
    os.environ.setdefault(_key, _value)

for _dir_path in (INPUT_DIR, WORK_DIR, OUTPUT_DIR):
    _dir_path.mkdir(exist_ok=True)

# ============================================================================
# CONFIGURATION LOADER
# ============================================================================

_config_cache: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Load and cache config.yaml from the LLM Translator root."""
    global _config_cache

    if _config_cache is not None:
        return _config_cache

    config_path = PIPELINE_ROOT / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = yaml.safe_load(f)

    return _config_cache


def get_config_section(section: str) -> Dict[str, Any]:
    """Get a top-level section from config.yaml (e.g. 'translation', 'builder').

    Returns a deep copy, not a reference into the process-wide cache — a
    caller that locally overrides a nested key (an operator-style summary
    disable, a test fixture) must not permanently corrupt config.yaml's
    in-memory state for every other caller in the same process.
    """
    config = load_config()
    return copy.deepcopy(config.get(section, {}))


def get_safety_fallback_config() -> Dict[str, Any]:
    """Provider-agnostic ``translation.safety_fallback`` config — the
    Qwen/Anthropic/OpenAI-to-DeepSeek content-refusal fallback. Not scoped
    under any single provider's config block since every non-DeepSeek
    provider shares this one mechanism by default."""
    return get_config_section("translation").get("safety_fallback", {}) or {}


# ============================================================================
# CONTENT PROCESSING CONSTANTS
# ============================================================================

SCENE_BREAK_MARKER = "* * *"

# Legacy format: [ILLUSTRATION: filename]
ILLUSTRATION_PLACEHOLDER_PATTERN = r'\[ILLUSTRATION:?\s*"?([^"\]]+)"?\]'
# Standard markdown format: ![alt](filename) where alt is 'illustration', 'gaiji', or empty
MARKDOWN_IMAGE_PATTERN = r'!\[(illustration|gaiji|)\]\(([^)]+)\)'

REMOVE_RUBY_TAGS = True


# ============================================================================
# LANGUAGE — this client is EN-only
# ============================================================================

def get_target_language() -> str:
    """Always 'en' — LLM Translator is an English-only lightweight client."""
    return "en"


def ensure_utf8_console(streams: Sequence[str] = ("stdout", "stderr")) -> None:
    """
    Force the named std stream(s) to UTF-8.

    Windows hands a redirected/piped stdout the console's ANSI codepage
    ('charmap') by default, which crashes the instant a print() call hits a
    JP character — chapter titles, character names, anything the Librarian
    or Builder logs. `errors="replace"` is belt-and-suspenders only; UTF-8
    itself covers the full JP text these processes ever print.

    NOT applied automatically at import time. `src/mcp/server.py`'s stdout
    is a JSON-RPC channel that must never be touched — only entry points
    that print plain human-readable text (scripts/mtl.py, the librarian/
    builder `__main__` subprocess entry points) should call this, and the
    MCP server should only ever pass `("stderr",)` for its own logging.
    """
    for name in streams:
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def get_language_config(target_language: str = None) -> Dict[str, Any]:
    """
    Get the `project.languages.<lang>` block from config.yaml, if any.

    Unlike the main pipeline's version, this never raises — Builder only
    ever reads fields off the result via `.get(..., default)`, so a missing
    or absent language block is just an empty dict.
    """
    if target_language is None:
        target_language = get_target_language()
    config = load_config()
    languages = config.get("project", {}).get("languages", {})
    return languages.get(target_language, {})

