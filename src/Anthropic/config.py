"""Anthropic-provider configuration accessors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section


def get_anthropic_config() -> Dict[str, Any]:
    """Return the isolated ``translation.anthropic`` configuration block."""
    return get_config_section("translation").get("anthropic", {}) or {}


def get_anthropic_prompt_path() -> Path:
    cfg = get_anthropic_config()
    relative = cfg.get("master_prompt", "src/Anthropic/prompts/master_prompt_anthropic_en.md")
    return PIPELINE_ROOT / str(relative)


def get_anthropic_conversation_config() -> Dict[str, Any]:
    return get_anthropic_config().get("conversation", {}) or {}


def get_anthropic_optimization_config() -> Dict[str, Any]:
    return get_anthropic_config().get("optimizations", {}) or {}


def get_anthropic_continuation_config() -> Dict[str, Any]:
    return get_anthropic_config().get("continuation", {}) or {}


def get_anthropic_retry_config() -> Dict[str, Any]:
    return get_anthropic_config().get("retry", {}) or {}


def get_anthropic_batch_config() -> Dict[str, Any]:
    return get_anthropic_config().get("batch", {}) or {}


def get_anthropic_advisor_config() -> Dict[str, Any]:
    return get_anthropic_config().get("advisor", {}) or {}


def get_anthropic_websearch_config() -> Dict[str, Any]:
    return get_anthropic_config().get("web_search", {}) or {}


def get_anthropic_caching_config() -> Dict[str, Any]:
    return get_anthropic_config().get("caching", {}) or {}


def get_anthropic_fidelity_config() -> Dict[str, Any]:
    """Post-translation completeness gate for the Anthropic route.

    Deterministic, token-free, and the only layer in this pipeline that
    actually verifies a finished chapter against its source: the proofreading
    advisor is consulted BEFORE drafting and never sees the output it is
    named for."""
    return get_anthropic_config().get("fidelity_gate", {}) or {}


def get_anthropic_telemetry_config() -> Dict[str, Any]:
    """Per-call token/cost logging for the Anthropic route. On by default:
    this is the most expensive pipeline in the project and the one whose
    spend is least visible without it."""
    return get_anthropic_config().get("telemetry", {}) or {}
