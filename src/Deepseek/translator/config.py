"""
DeepSeek Translator Configuration — the single control knob.

Everything DeepSeekClient needs comes out of config.yaml's
`translation.deepseek` block through get_deepseek_config(), remapped to
the field names the (verbatim-copied) client code already expects.
"""

from pathlib import Path
from typing import Any, Dict

from src.Deepseek.common.config import load_config, get_config_section


def get_master_prompt_path() -> Path:
    """Path to the active DeepSeek master prompt XML, from config.yaml."""
    from src.Deepseek.common.config import PIPELINE_ROOT

    translation = get_config_section("translation")
    relative = translation.get("master_prompt", "src/Deepseek/prompt/master_prompt_deepseek_en.xml")
    return PIPELINE_ROOT / relative


def get_deepseek_config() -> Dict[str, Any]:
    """
    Return the DeepSeek client config, remapped from config.yaml's
    `translation.deepseek` block into the shape deepseek_client.py expects
    (endpoint_type/base_url/thinking_mode/caching/cache_monitor/...).
    """
    translator_cfg = get_config_section("translation").get("deepseek", {}) or {}

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
    """The `translation.deepseek.conversation` block, verbatim."""
    translator_cfg = get_config_section("translation").get("deepseek", {}) or {}
    return translator_cfg.get("conversation", {}) or {}


def get_optimizations_config() -> Dict[str, Any]:
    """The `translation.deepseek.optimizations` block (DRDI/DOVB/CCT toggles)."""
    translator_cfg = get_config_section("translation").get("deepseek", {}) or {}
    return translator_cfg.get("optimizations", {}) or {}


def get_post_processing_config() -> Dict[str, Any]:
    """The `translation.deepseek.post_processing` block."""
    translator_cfg = get_config_section("translation").get("deepseek", {}) or {}
    return translator_cfg.get("post_processing", {}) or {}


def get_continuation_config() -> Dict[str, Any]:
    """The `translation.deepseek.continuation` block (output-cap continuation).

    When a chapter's response is cut off by the per-turn output cap
    (``stop_reason == max_tokens`` — reasoning tokens count against the same
    budget as the translation text on this endpoint), the translator issues
    follow-up calls that continue from where the previous output stopped.
    ``enabled`` gates the behavior; ``max_continuations`` caps how many
    follow-up calls one chapter may take before the partial result is kept.
    """
    translator_cfg = get_config_section("translation").get("deepseek", {}) or {}
    return translator_cfg.get("continuation", {}) or {}


def get_thinking_log_config() -> Dict[str, Any]:
    """The `translation.thinking_log` block — general, sibling of the
    `deepseek:` / `qwen:` provider menus, since it's a translate-time behavior
    toggle rather than a provider client parameter."""
    translation_cfg = get_config_section("translation")
    return translation_cfg.get("thinking_log", {}) or {}
