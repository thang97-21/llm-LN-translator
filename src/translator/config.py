"""
DeepSeek Translator Configuration — the single control knob.

Everything DeepSeekClient needs comes out of config.yaml's
`translation.translator` block through get_deepseek_config(), remapped to
the field names the (verbatim-copied) client code already expects.
"""

from pathlib import Path
from typing import Any, Dict

from src.common.config import load_config, get_config_section


def get_master_prompt_path() -> Path:
    """Path to the active DeepSeek master prompt XML, from config.yaml."""
    from src.common.config import PIPELINE_ROOT

    translation = get_config_section("translation")
    relative = translation.get("master_prompt", "src/prompt/master_prompt_deepseek_en.xml")
    return PIPELINE_ROOT / relative


def get_deepseek_config() -> Dict[str, Any]:
    """
    Return the DeepSeek client config, remapped from config.yaml's
    `translation.translator` block into the shape deepseek_client.py expects
    (endpoint_type/base_url/thinking_mode/caching/cache_monitor/...).
    """
    translator_cfg = get_config_section("translation").get("translator", {}) or {}

    thinking = translator_cfg.get("thinking", {}) or {}
    caching = translator_cfg.get("caching", {}) or {}

    return {
        "endpoint_type": "anthropic",
        "model": translator_cfg.get("model", "deepseek-v4-pro"),
        "fallback_model": None,
        "api_key_env": translator_cfg.get("api_key_env", "DEEPSEEK_API_KEY"),
        "base_url": translator_cfg.get("endpoint", "https://api.deepseek.com/anthropic"),
        "http_timeout_seconds": translator_cfg.get("http_timeout_seconds", 600),
        "generation": translator_cfg.get("generation", {}) or {},
        "thinking_mode": {
            "enabled": thinking.get("enabled", True),
            "reasoning_effort": thinking.get("effort", "max"),
            "thinking_budget": thinking.get("budget_tokens", 32000),
            "reasoning_content": {"enabled": True},
        },
        "caching": {
            "enabled": caching.get("enabled", True),
            "ttl_minutes": 120,
        },
        "cache_monitor": caching.get("cache_monitor", {}) or {},
        "thinking_analytics": caching.get("thinking_analytics", {}) or {},
        "context_window": 1_000_000,
    }


def get_conversation_config() -> Dict[str, Any]:
    """The `translation.translator.conversation` block, verbatim."""
    translator_cfg = get_config_section("translation").get("translator", {}) or {}
    return translator_cfg.get("conversation", {}) or {}


def get_optimizations_config() -> Dict[str, Any]:
    """The `translation.translator.optimizations` block (DRDI/DOVB/CCT toggles)."""
    translator_cfg = get_config_section("translation").get("translator", {}) or {}
    return translator_cfg.get("optimizations", {}) or {}


def get_post_processing_config() -> Dict[str, Any]:
    """The `translation.translator.post_processing` block."""
    translator_cfg = get_config_section("translation").get("translator", {}) or {}
    return translator_cfg.get("post_processing", {}) or {}
