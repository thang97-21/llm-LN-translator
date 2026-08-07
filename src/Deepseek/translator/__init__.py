"""Phase 2 translation provider boundary."""

from src.Deepseek.translator.provider import get_translator_class, get_volume_translator


def translate_volume(*args, **kwargs):
	return get_volume_translator()(*args, **kwargs)


__all__ = ["translate_volume", "get_translator_class", "get_volume_translator"]
