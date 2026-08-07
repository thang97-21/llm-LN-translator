"""Phase 2 provider selector. DeepSeek implementation remains isolated."""

from typing import Any


def get_translator_class(provider: str | None = None) -> Any:
    from src.Deepseek.common.config import get_config_section
    selected = str(provider or get_config_section("translation").get("provider", "deepseek")).strip().lower()
    if selected == "qwen":
        from src.Qwen.agent import QwenTranslator
        return QwenTranslator
    if selected == "deepseek":
        from src.Deepseek.translator.agent import DeepSeekTranslator
        return DeepSeekTranslator
    raise ValueError(f"Unsupported translation provider: {selected!r}; expected deepseek or qwen")


def get_volume_translator(provider: str | None = None) -> Any:
    selected = str(provider or "").strip().lower()
    if selected == "qwen":
        from src.Qwen.agent import translate_volume
        return translate_volume
    if selected == "deepseek":
        from src.Deepseek.translator.agent import translate_volume
        return translate_volume
    from src.Deepseek.common.config import get_config_section
    return get_volume_translator(str(get_config_section("translation").get("provider", "deepseek")))
