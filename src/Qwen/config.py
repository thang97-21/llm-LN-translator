"""Qwen-only configuration accessors."""

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_qwen_config() -> Dict[str, Any]:
    return get_config_section("translation").get("qwen", {}) or {}


def get_qwen_prompt_path() -> Path:
    cfg = get_qwen_config()
    relative = cfg.get("master_prompt", "src/Qwen/prompts/master_prompt_qwen_en.xml")
    return PIPELINE_ROOT / str(relative)


def get_qwen_conversation_config() -> Dict[str, Any]:
    return get_qwen_config().get("conversation", {}) or {}


def get_qwen_optimization_config() -> Dict[str, Any]:
    return get_qwen_config().get("optimizations", {}) or {}


def get_qwen_continuation_config() -> Dict[str, Any]:
    return get_qwen_config().get("continuation", {}) or {}


def get_qwen_retry_config() -> Dict[str, Any]:
    return get_qwen_config().get("retry", {}) or {}


def get_qwen_streaming_config() -> Dict[str, Any]:
    return get_qwen_config().get("streaming", {}) or {}
