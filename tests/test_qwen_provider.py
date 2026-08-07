"""Focused, credential-free tests for the Qwen Phase 2 provider."""

from src.Qwen.client import QwenClient
from src.Qwen.conversation import QwenConversationManager
from src.Qwen.errors import (
    QwenModerationError,
    QwenRateLimitError,
    classify_exception,
    is_retryable,
)
from src.Qwen.optimization import supports_partial_prefix
from src.Deepseek.common.llm_types import LLMTermination
from src.Deepseek.common.token_telemetry import cost_breakdown_usd
from src.Deepseek.translator.provider import get_translator_class


def test_default_and_explicit_provider_selection():
    assert get_translator_class("deepseek").__name__ == "DeepSeekTranslator"
    assert get_translator_class("qwen").__name__ == "QwenTranslator"


def test_thinking_disables_partial_prefix():
    assert not supports_partial_prefix(True, {"partial_prefix": True})
    assert supports_partial_prefix(False, {"partial_prefix": True})


def test_qwen_dry_run_has_anthropic_payload_without_credentials():
    client = QwenClient(dry_run=True)
    response = client.generate(prompt="translate", system_instruction="system", dry_run=True)
    payload = response.provider_metadata["payload"]
    assert response.provider == "qwen"
    assert payload["model"] == "qwen3.8-max"
    assert payload["thinking"]["type"] == "enabled"
    assert payload["stream"] is True
    assert payload["system"][0]["cache_control"]["type"] == "ephemeral"
    assert payload["messages"][-1]["content"][0]["cache_control"]["type"] == "ephemeral"


def test_qwen_partial_mode_disables_thinking_in_payload():
    client = QwenClient(dry_run=True)
    response = client.generate(
        prompt="continue",
        system_instruction="system",
        messages=[
            {"role": "user", "content": "source"},
            {"role": "assistant", "content": "partial text"},
        ],
        dry_run=True,
        partial=True,
    )
    payload = response.provider_metadata["payload"]
    assert payload["thinking"]["type"] == "disabled"
    assert payload["messages"][-1]["partial"] is True


def test_qwen_dict_response_normalizes_content_thinking_usage():
    client = QwenClient(dry_run=True)

    class Messages:
        def create(self, **_request):
            return {
                "id": "msg-test",
                "content": [
                    {"type": "thinking", "thinking": "reason"},
                    {"type": "text", "text": "translated"},
                ],
                "stop_reason": "max_tokens",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 6,
                },
            }

    class FakeAPI:
        messages = Messages()

    client._client = FakeAPI()
    response = client.generate(prompt="translate", system_instruction="system", stream=False)
    assert response.content == "translated"
    assert response.thinking_content == "reason"
    assert response.termination == LLMTermination.MAX_OUTPUT
    assert response.cached_tokens == 4
    assert response.cache_creation_tokens == 6
    assert response.total_cost_usd > 0
    assert response.provider_metadata["raw_content_blocks"][0]["type"] == "thinking"


def test_moderation_and_rate_limit_classification():
    class FakeHTTPError(Exception):
        def __init__(self, status_code: int, message: str):
            super().__init__(message)
            self.status_code = status_code

    moderated = classify_exception(FakeHTTPError(400, "data_inspection_failed: inappropriate content"))
    limited = classify_exception(FakeHTTPError(429, "rate_limit_error"))
    assert isinstance(moderated, QwenModerationError)
    assert isinstance(limited, QwenRateLimitError)
    assert not is_retryable(moderated)
    assert is_retryable(limited)


def test_qwen_pricing_table_present():
    costs = cost_breakdown_usd(
        model_name="qwen3.8-max",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_creation_tokens=100,
    )
    assert costs["cache_creation_cost_usd"] > 0
    assert costs["total_cost_usd"] > 0


def test_conversation_compaction(tmp_path):
    manager = QwenConversationManager(
        tmp_path,
        "VOL",
        "qwen3.8-max",
        "https://dashscope-intl.aliyuncs.com/apps/anthropic",
        {
            "enabled": True,
            "recent_verbatim_chapters": 1,
            "context_window": 200,
            "soft_notice_ratio": 0.1,
            "compact_ratio": 0.2,
            "hard_trim_ratio": 0.3,
            "persistence_file": ".context/qwen_conversation.json",
        },
    )
    for index in range(4):
        manager.commit(
            f"CHAPTER_0{index}",
            [{"role": "user", "content": "x" * 80}],
            "y" * 80,
            tmp_path / f"CHAPTER_0{index}_EN.md",
            [{"type": "text", "text": "y" * 80}],
        )
    assert len(manager.turns) <= 1
    assert manager.state.get("summary")