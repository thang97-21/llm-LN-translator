"""OpenAI Responses-provider configuration accessors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_openai_config() -> Dict[str, Any]:
    """Return the isolated ``translation.openai`` configuration block."""
    return get_config_section("translation").get("openai", {}) or {}


def get_openai_prompt_path() -> Path:
    cfg = get_openai_config()
    relative = cfg.get("master_prompt", "src/OpenAI/prompts/master_prompt_openai_en.md")
    return PIPELINE_ROOT / str(relative)


def get_openai_conversation_config() -> Dict[str, Any]:
    return get_openai_config().get("conversation", {}) or {}


def get_openai_optimization_config() -> Dict[str, Any]:
    return get_openai_config().get("optimizations", {}) or {}


def get_openai_continuation_config() -> Dict[str, Any]:
    return get_openai_config().get("continuation", {}) or {}


def get_openai_retry_config() -> Dict[str, Any]:
    return get_openai_config().get("retry", {}) or {}
