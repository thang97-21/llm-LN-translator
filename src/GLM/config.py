"""GLM-provider configuration accessors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_glm_config() -> Dict[str, Any]:
    return get_config_section("translation").get("glm", {}) or {}


def get_glm_prompt_path() -> Path:
    relative = get_glm_config().get("master_prompt", "src/GLM/prompts/master_prompt_glm_en.xml")
    return PIPELINE_ROOT / str(relative)


def get_glm_conversation_config() -> Dict[str, Any]:
    return get_glm_config().get("conversation", {}) or {}


def get_glm_optimization_config() -> Dict[str, Any]:
    return get_glm_config().get("optimizations", {}) or {}


def get_glm_continuation_config() -> Dict[str, Any]:
    return get_glm_config().get("continuation", {}) or {}


def get_glm_retry_config() -> Dict[str, Any]:
    return get_glm_config().get("retry", {}) or {}
