"""OpenAI Responses-provider configuration accessors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_openai_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the isolated ``translation.openai`` configuration block."""
    if source_config is not None:
        return source_config
    return get_config_section("translation").get("openai", {}) or {}


def get_openai_prep_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the isolated ``prep.openai`` configuration block."""
    prep_config = source_config if source_config is not None else get_config_section("prep")
    return prep_config.get("openai", {}) or {}


def get_openai_prompt_path(source_config: Optional[Dict[str, Any]] = None) -> Path:
    cfg = get_openai_config(source_config)
    relative = cfg.get("master_prompt", "src/OpenAI/prompts/master_prompt_openai_en.md")
    return PIPELINE_ROOT / str(relative)


def get_openai_conversation_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_openai_config(source_config).get("conversation", {}) or {}


def get_openai_optimization_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_openai_config(source_config).get("optimizations", {}) or {}


def get_openai_continuation_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_openai_config(source_config).get("continuation", {}) or {}


def get_openai_retry_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_openai_config(source_config).get("retry", {}) or {}


def get_openai_batch_config(source_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return get_openai_config(source_config).get("batch", {}) or {}


def openai_batch_enabled(source_config: Optional[Dict[str, Any]] = None, model: Optional[str] = None) -> bool:
    """Resolve the batch policy without making Astra opt-in by accident.

    ``batch.enabled`` is the explicit switch for GPT-5.6-family routes. Astra
    uses ``auto_for_astra`` (true by default) because its published Batch rate
    is materially lower than Standard processing. Operators can still disable
    Astra batching explicitly with ``auto_for_astra: false``.
    """
    cfg = get_openai_batch_config(source_config)
    if bool(cfg.get("enabled", False)):
        return True
    auto_for_astra = bool(cfg.get("auto_for_astra", True))
    selected = str(model or get_openai_config(source_config).get("model", "") or "").strip().lower()
    return auto_for_astra and selected == "gpt-6-astra"
