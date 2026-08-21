"""Phase 2 provider selector with isolated provider implementations."""

from typing import Any


def get_translator_class(provider: str | None = None) -> Any:
    from src.Deepseek.common.config import get_config_section

    selected = str(provider or get_config_section("translation").get("provider", "deepseek")).strip().lower()
    if selected == "qwen":
        from src.Qwen.agent import QwenTranslator

        return QwenTranslator
    if selected == "openai":
        from src.OpenAI.agent import OpenAITranslator

        return OpenAITranslator
    if selected == "anthropic":
        from src.Anthropic.agent import AnthropicTranslator

        return AnthropicTranslator
    if selected == "deepseek":
        from src.Deepseek.translator.agent import DeepSeekTranslator

        return DeepSeekTranslator
    raise ValueError(f"Unsupported translation provider: {selected!r}; expected deepseek, qwen, openai, or anthropic")


def get_volume_translator(provider: str | None = None) -> Any:
    selected = str(provider or "").strip().lower()
    if selected == "qwen":
        from src.Qwen.agent import translate_volume

        return translate_volume
    if selected == "openai":
        from src.OpenAI.agent import translate_volume

        return translate_volume
    if selected == "anthropic":
        from src.Anthropic.agent import translate_volume

        return translate_volume
    if selected == "deepseek":
        from src.Deepseek.translator.agent import translate_volume

        return translate_volume
    from src.Deepseek.common.config import get_config_section

    return get_volume_translator(str(get_config_section("translation").get("provider", "deepseek")))
