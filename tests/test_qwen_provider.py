"""Focused, credential-free tests for the Qwen Phase 2 provider."""

from src.Qwen.client import QwenClient
from src.Qwen.config import get_qwen_config
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
    # Assert the payload carries the CONFIGURED model, not a hardcoded version
    # string. Frontier-class Qwen model names roll (3.7-max, 3.8-max, whatever
    # is next); pinning one here only guarantees this test fails the next time
    # someone legitimately changes config.yaml. What matters is that the client
    # actually forwards what it was configured with.
    assert payload["model"] == get_qwen_config()["model"]
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


def test_qwen_dry_run_drops_conversation_history(tmp_path):
    """A dry-run payload must be single-turn even when a conversation ledger
    with real history is attached.

    This is the regression guard for a leak found by reading actual DRY_RUN
    output: the agent assembled the recent-verbatim window BEFORE the client
    ever saw dry_run, so every chapter's preview carried the same frozen pair
    of chapters left behind by the last real run — truthful for at most one
    chapter in the volume and fiction for the rest. The guard now lives in
    QwenClient.generate(), so passing history in explicitly must not defeat it.
    """
    manager = QwenConversationManager(
        tmp_path,
        "VOL",
        "qwen3.8-max",
        "https://dashscope-intl.aliyuncs.com/apps/anthropic",
        {
            "enabled": True,
            "recent_verbatim_chapters": 2,
            "context_window": 1_000_000,
            "persistence_file": ".context/qwen_conversation.json",
        },
    )
    for index in (6, 7):
        manager.commit(
            f"CHAPTER_0{index}",
            [{"role": "user", "content": f"JP source of chapter {index}"}],
            f"EN output of chapter {index}",
            tmp_path / f"CHAPTER_0{index}_EN.md",
            [{"type": "text", "text": f"EN output of chapter {index}"}],
        )
    history = manager.messages("system", "translate chapter 1")
    assert len(history) > 1, "precondition: the ledger really does carry history"

    client = QwenClient(dry_run=True)
    response = client.generate(
        prompt="translate chapter 1",
        system_instruction="system",
        messages=history,          # deliberately handed the assembled window
        dry_run=True,
    )
    payload = response.provider_metadata["payload"]
    assert len(payload["messages"]) == 1, payload["messages"]
    assert payload["messages"][0]["role"] == "user"
    rendered = "".join(
        block.get("text", "") for block in payload["messages"][0]["content"]
    )
    assert "chapter 6" not in rendered and "chapter 7" not in rendered


def test_dry_run_renderer_handles_block_shaped_system(tmp_path):
    """The shared renderer must not str() a list-shaped system block.

    Qwen sends `system` as a list of cache_control-bearing content blocks;
    DeepSeek sends a plain string. str() on the list produced a Python repr —
    one enormous escaped line — which is why every Qwen dry-run file was
    hundreds of KB and unreadable.
    """
    from src.Deepseek.translator.dry_run import write_dry_run_prompt

    path = write_dry_run_prompt(
        work_dir=tmp_path,
        volume_id="VOL",
        chapter_id="CHAPTER_01",
        payload={
            "model": "qwen3.7-max",
            "system": [
                {"type": "text", "text": "SYSTEM_MARKER", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "USER_MARKER"}]}],
            "thinking": {"type": "enabled", "budget_tokens": 100},
            "max_tokens": 200,
        },
        provider="qwen",
    )
    text = path.read_text(encoding="utf-8")
    assert "SYSTEM_MARKER" in text
    assert "USER_MARKER" in text
    assert "'type': 'text'" not in text, "system rendered as a Python repr"
    assert "cache_control" not in text
    assert "QwenClient.generate()" in text, "header must name the actual client"
    assert "DeepSeekClient" not in text


def test_safety_fallback_block_build_and_inject(tmp_path):
    from src.Qwen.safety_fallback import build_inheritance_block, inject_inheritance_block

    marker = "This run succeeds from Qwen's safety refusal. All translation decisions must inherit from Qwen."
    block_xml = build_inheritance_block(
        refusal_code="InvalidParameter",
        marker=marker,
        summary="Alfiria Glastonbury; アルフィリアお嬢様 → Lady Alfiria (LOCKED); Shinya's ore-casual deadpan.",
    )

    # Build a minimal context.xml shell with a couple of real blocks.
    from xml.etree import ElementTree as ET

    root = ET.Element("mtls_project_context", {"schema_version": "1.0", "volume_id": "V"})
    ET.SubElement(root, "volume_identity", {"status": "completed"})
    ET.SubElement(root, "character_attribute_anchors", {"status": "pending"})
    ET.SubElement(root, "voice_fingerprints", {"status": "completed"})
    ET.indent(root, space="  ")
    context_path = tmp_path / "context.xml"
    context_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
    )

    inject_inheritance_block(context_path, block_xml)
    parsed = ET.parse(context_path).getroot()
    block = parsed.find("translation_inheritance")
    assert block is not None
    assert block.get("status") == "completed"
    assert block.get("owner") == "safety_fallback"
    assert block.get("source") == "qwen"
    assert block.get("target") == "deepseek"
    assert block.get("refusal_code") == "InvalidParameter"
    assert block.findtext("marker") == marker
    assert "Lady Alfiria" in block.findtext("decision_summary")

    # Idempotent re-injection: still exactly one block, others preserved.
    inject_inheritance_block(context_path, block_xml)
    parsed2 = ET.parse(context_path).getroot()
    assert len(parsed2.findall("translation_inheritance")) == 1
    assert parsed2.find("volume_identity") is not None
    assert parsed2.find("character_attribute_anchors") is not None
    assert parsed2.find("voice_fingerprints") is not None


def test_safety_fallback_grab_refusal_code():
    from src.Qwen.errors import QwenModerationError
    from src.Qwen.safety_fallback import grab_refusal_code

    err = QwenModerationError("inappropriate content", status_code=400, code="InvalidParameter")
    assert grab_refusal_code(err) == "InvalidParameter"

    bare = QwenModerationError("inappropriate content", status_code=400)
    assert grab_refusal_code(bare) == "data_inspection_failed"


def test_safety_fallback_disabled_rejects(tmp_path):
    from src.Qwen.safety_fallback import fallback_translate_chapter
    from src.Qwen.errors import QwenModerationError

    err = QwenModerationError("inappropriate content", status_code=400, code="InvalidParameter")
    try:
        fallback_translate_chapter(
            work_dir=tmp_path,
            volume_id="V",
            chapter_path=tmp_path / "JP" / "CHAPTER_01.md",
            chapter_id="CHAPTER_01",
            refusal=err,
            config={"enabled": False},
        )
        assert False, "disabled fallback must re-raise the moderation error"
    except QwenModerationError as raised:
        assert raised is err


def test_inherit_prompt_covers_landmarks_epithets_honorific_practice():
    """The inheritance prompt must extract the decisions that actually drift in a
    fallback run: the OBSERVED honorific practice (not just the stated policy,
    including retained -san as a comedic beat), epithet word order, and recurring
    landmarks — with prior Qwen output winning over metadata files. If any of
    these clauses is trimmed, the next DeepSeek fallback run silently loses the
    locks that ch8-11 needed."""
    from src.Qwen.safety_fallback import _load_inherit_prompt

    system, user = _load_inherit_prompt()
    assert "{prior_chapters}" in user and "{context_xml}" in user
    for marker in (
        "Observed honorific practice",
        "comedic beat",
        "word order",
        "Rainbow-Flame Witch",
        "Landmarks",
        "PRIOR OUTPUT WINS",
        "Aurora Academy of Magic",
    ):
        assert marker in system, f"inherit prompt missing clause: {marker}"


def test_safety_fallback_writes_qc_inheritance_artifact(tmp_path):
    """The fallback must leave a QC audit trail: exact refusal details, every
    chapter completed before the refusal (naturally sorted), the inheritance
    config that governs the rest of the volume, and a binding instruction that
    mtl-qc runs a consistency copypass when the file exists."""
    from src.Qwen.safety_fallback import (
        _prior_completed_chapters,
        _write_inheritance_translator_artifact,
    )

    en_dir = tmp_path / "EN"
    en_dir.mkdir()
    for i in range(1, 11):
        (en_dir / f"CHAPTER_{i:02d}_EN.md").write_text(f"# {i}", encoding="utf-8")

    # Natural sort: CHAPTER_10 must follow CHAPTER_09, not sort before 02.
    assert _prior_completed_chapters(tmp_path, "CHAPTER_08") == [
        "CHAPTER_01", "CHAPTER_02", "CHAPTER_03", "CHAPTER_04", "CHAPTER_05",
        "CHAPTER_06", "CHAPTER_07", "CHAPTER_09", "CHAPTER_10",
    ]

    cfg = {
        "enabled": True,
        "fallback_provider": "deepseek",
        "block_name": "translation_inheritance",
        "marker": "marker",
        "inherit_agent": {
            "enabled": True,
            "prompt": "src/Qwen/prompts/inherit_decisions_prompt.xml",
        },
    }
    path = _write_inheritance_translator_artifact(
        tmp_path,
        volume_id="V",
        chapter_id="CHAPTER_08",
        refusal_code="InvalidParameter",
        refusal_message="inappropriate content",
        status_code=400,
        inherit_config=cfg,
    )
    assert path == tmp_path / "QC" / "inheritance_translator.json"

    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["artifact"] == "inheritance_translator"
    assert data["schema_version"] == 1
    assert data["refused_chapter"] == "CHAPTER_08"
    assert data["refusal"] == {
        "error_code": "InvalidParameter",
        "refusal_message": "inappropriate content",
        "status_code": 400,
    }
    assert data["completed_chapters_before_refusal"][0] == "CHAPTER_01"
    assert data["completed_chapters_before_refusal"][-1] == "CHAPTER_10"
    assert "CHAPTER_08" not in data["completed_chapters_before_refusal"]
    assert data["deepseek_inheritance_config"]["fallback_provider"] == "deepseek"
    assert "consistency copypass" in data["qc_instruction"]


def test_dry_run_translate_volume_bypasses_manifest(tmp_path):
    """Dry-run must ignore manifest completion state and assemble payloads for
    every requested chapter (it is a developer inspection mode); a non-dry run
    must still filter completed chapters."""
    import json

    import src.Deepseek.translator.agent as da
    import src.Qwen.agent as qa

    fake_work = tmp_path / "work"
    root = fake_work / "V"  # WORK_DIR/<volume_id>/
    jp = root / "JP"
    jp.mkdir(parents=True)
    for i in range(1, 5):
        (jp / f"CHAPTER_{i:02d}.md").write_text(f"# {i}", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps({
            "chapters": [
                {"id": "01", "source_file": "CHAPTER_01.md", "translation_status": "completed"},
                {"id": "02", "source_file": "CHAPTER_02.md", "translation_status": "completed"},
                {"id": "03", "source_file": "CHAPTER_03.md", "translation_status": "pending"},
                {"id": "04", "source_file": "CHAPTER_04.md", "translation_status": "pending"},
            ]
        }),
        encoding="utf-8",
    )
    (root / "context.xml").write_text(
        '<mtls_project_context schema_version="1.0" volume_id="V"/>', encoding="utf-8"
    )

    qa_work, da_work = qa.WORK_DIR, da.WORK_DIR
    try:
        qa.WORK_DIR = fake_work
        da.WORK_DIR = fake_work
        res_q = qa.translate_volume("V", dry_run=True)
        res_d = da.translate_volume("V", dry_run=True)
    finally:
        qa.WORK_DIR = qa_work
        da.WORK_DIR = da_work

    expected = {"CHAPTER_01", "CHAPTER_02", "CHAPTER_03", "CHAPTER_04"}
    assert set(res_q) == expected, f"Qwen dry-run must bypass manifest: {sorted(res_q)}"
    assert set(res_d) == expected, f"DeepSeek dry-run must bypass manifest: {sorted(res_d)}"

    # The non-dry-run path still respects the manifest completion filter.
    kept = da._filter_completed_chapters(root, sorted(jp.glob("CHAPTER_*.md")))
    assert {p.stem for p in kept} == {"CHAPTER_03", "CHAPTER_04"}