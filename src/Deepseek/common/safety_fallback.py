"""Provider → DeepSeek safety-refusal fallback with decision inheritance.

Default behavior for every non-DeepSeek Phase 2 provider: when the provider
refuses a chapter on content-safety grounds, the refusal is deterministic —
retrying the same payload against the same gate/model fails identically. So
the route does NOT retry. Instead it hands the chapter to the DeepSeek route
— but not cold. Before the DeepSeek payload is built:

1. The exact refusal code is captured from the exception.
2. An inheritance agent (one DeepSeek call running on the PREP config from
   config.yaml — model/endpoint/api_key) reads every EN chapter the source
   provider has already produced, summarizes the translation decisions it
   established (names, honorifics, voice, terminology, style), and returns
   a summary.
3. That summary is injected into the ``<translation_inheritance>`` block of
   context.xml with a marker stating that this run succeeds from a safety
   refusal and all translation decisions must inherit from the source
   provider.
4. The chapter is then translated via the DeepSeek route (which embeds
   context.xml verbatim in its system prompt, so the injected block reaches
   the model without any prompt surgery).

The ``<translation_inheritance>`` block is seeded ``<pending/>`` by the
Librarian shell and left pending by prep; it is populated only at runtime,
when a safety refusal fires.

Provider contract: every non-DeepSeek translator (Qwen, Anthropic, OpenAI,
and any future provider) is responsible for catching its own refusal signal
— an API-level moderation exception (Qwen) or a normalized
``LLMTermination.REFUSED`` response (Anthropic, OpenAI) — and, before giving
up on the chapter, calling ``fallback_translate_chapter()`` here with its own
``source_provider`` display name (e.g. ``"Qwen"``, ``"Anthropic"``,
``"OpenAI"``). Wiring this fallback in is part of what it means to satisfy
that contract, on the same footing as matching ``translate_volume``'s call
signature — a new provider is not done until it does.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section
from src.utility.prep.agent import _call_deepseek_prep

logger = logging.getLogger(__name__)

_BLOCK_NAME = "translation_inheritance"
_BLOCK_OWNER = "safety_fallback"

# Every provider's AnthropicAPIError-style base class stamps one of these as
# its generic, un-specific default `code` when no real code was extracted.
# grab_refusal_code() must not report one of these as if it were meaningful.
_GENERIC_PROVIDER_ERROR_CODES = {"qwen_api_error", "anthropic_api_error", "openai_api_error"}

# QC audit artifact written to WORK/<vol_id>/QC/ on every fallback firing.
_QC_ARTIFACT_NAME = "inheritance_translator.json"


def _default_marker(source_provider: str) -> str:
    return (
        f"This run succeeds from {source_provider}'s safety refusal. "
        f"All translation decisions must inherit from {source_provider}."
    )


def _qc_instruction(source_provider: str) -> str:
    return (
        f"This volume contains chapters translated by DeepSeek after a {source_provider} "
        f"content-safety refusal. {source_provider} established the translation decisions for "
        "every chapter before the refused chapter; the chapters after it were produced by "
        "DeepSeek and must be verified consistent with those decisions. mtl-qc MUST run a "
        "consistency copypass IN ADDITION to the standard QC steps: cross-check every "
        "post-refusal chapter against the recorded decisions in this file (names, honorifics, "
        f"landmarks, epithets, voice, terminology) and against the pre-refusal {source_provider} "
        "chapters, and report any cross-provider drift as a finding."
    )


# ══════════════════════════════════════════════════════════════════════════
# Refusal code capture
# ══════════════════════════════════════════════════════════════════════════

def grab_refusal_code(exc: BaseException, *, default: str = "content_refusal") -> str:
    """Return the exact refusal code for the audit trail.

    Prefers the provider's ``code`` (e.g. ``InvalidParameter``), falling back
    to ``default`` — the caller's best label for its own refusal shape (Qwen's
    real moderation-gate default is ``"data_inspection_failed"``; a provider
    that only synthesizes a refusal from ``LLMTermination.REFUSED``, with no
    distinct API-level code, should pass something like ``"model_refusal"``).
    A provider's own generic ``*_api_error`` default `code` is NOT a refusal
    code, so it is treated as absent.
    """
    code = getattr(exc, "code", None)
    if code and str(code) not in _GENERIC_PROVIDER_ERROR_CODES:
        return str(code)
    return default


# ══════════════════════════════════════════════════════════════════════════
# Inheritance agent — summarize the source provider's established decisions
# ══════════════════════════════════════════════════════════════════════════

def _load_inherit_prompt() -> Tuple[str, str]:
    """Load the inheritance-agent prompt XML → (system, user template).

    The prompt file uses the same wrapper shape as the master prompts:
    a <system> block (templated with ``{source_provider}``) and a <user>
    template with ``{prior_chapters}`` and ``{context_xml}`` placeholders.
    """
    cfg = get_config_section("translation").get("safety_fallback", {}) or {}
    prompt_rel = str((cfg.get("inherit_agent") or {}).get(
        "prompt", "src/Qwen/prompts/inherit_decisions_prompt.xml"
    ))
    prompt_path = PIPELINE_ROOT / prompt_rel
    root = ET.parse(prompt_path).getroot()
    sys_el = root.find("system")
    user_el = root.find("user")
    if sys_el is None or user_el is None:
        raise ValueError(f"Malformed inherit prompt {prompt_path}: missing <system>/<user>")
    return (sys_el.text or "").strip(), (user_el.text or "").strip()


def _chapter_sort_key(chapter_id: str) -> Tuple[int, str]:
    """Natural sort key so CHAPTER_10 sorts after CHAPTER_09, not after 01."""
    digits = "".join(ch for ch in chapter_id if ch.isdigit())
    return (int(digits) if digits else 0, chapter_id)


def _prior_completed_chapters(work_dir: Path, exclude_chapter_id: str) -> List[str]:
    """Sorted ids of EN chapters already on disk, excluding the refused one."""
    en_dir = Path(work_dir) / "EN"
    if not en_dir.is_dir():
        return []
    ids = [
        path.stem.replace("_EN", "")
        for path in en_dir.glob("CHAPTER_*_EN.md")
        if path.stem.replace("_EN", "") != exclude_chapter_id
    ]
    return sorted(ids, key=_chapter_sort_key)


def _collect_prior_translated_chapters(work_dir: Path, exclude_chapter_id: str) -> str:
    """Concatenate every EN chapter already on disk, newest first, minus the
    refused chapter itself. These are the decisions the source provider
    actually made."""
    en_dir = Path(work_dir) / "EN"
    if not en_dir.is_dir():
        return "(no prior EN output found)"
    chunks: List[str] = []
    for chapter_id in reversed(_prior_completed_chapters(work_dir, exclude_chapter_id)):
        text = (en_dir / f"{chapter_id}_EN.md").read_text(encoding="utf-8")
        chunks.append(f"===== {chapter_id} =====\n{text}")
    return "\n\n".join(chunks) if chunks else "(no prior EN output found)"


def run_inheritance_agent(
    work_dir: Path,
    volume_id: str,
    chapter_id: str,
    refusal_code: str,
    source_provider: str,
) -> str:
    """One DeepSeek call on the PREP config: summarize the source provider's decisions.

    Reads the prior EN chapters + context.xml, prompts the agent, and
    returns the decision summary. Uses ``_call_deepseek_prep`` so the call
    inherits the prep section's model/endpoint/api_key exactly as configured.
    """
    system_instruction, user_template = _load_inherit_prompt()
    system_instruction = system_instruction.format(source_provider=source_provider)
    prior = _collect_prior_translated_chapters(work_dir, chapter_id)
    context_path = Path(work_dir) / "context.xml"
    context_xml = context_path.read_text(encoding="utf-8") if context_path.exists() else "(no context.xml)"
    user_message = user_template.format(prior_chapters=prior, context_xml=context_xml)
    logger.info(
        "[SAFETY-FALLBACK] %s — running inheritance agent (source=%s, refusal_code=%s)",
        chapter_id, source_provider, refusal_code,
    )
    return _call_deepseek_prep(system_instruction, user_message, volume_id=volume_id)


def _write_inheritance_translator_artifact(
    work_dir: Path,
    *,
    volume_id: str,
    chapter_id: str,
    refusal_code: str,
    refusal_message: str,
    status_code: Optional[int],
    inherit_config: Dict[str, Any],
    source_provider: str = "Qwen",
) -> Path:
    """Write the QC audit artifact for a safety-refusal handoff.

    Recorded in ``WORK/<vol_id>/QC/inheritance_translator.json``: which
    provider triggered the fallback, the exact refusal (code + message +
    status), every chapter completed before the refusal, and the inheritance
    config governing the DeepSeek run of the rest of the volume. Carries a
    ``qc_instruction`` that later mtl-qc runs must honor — if this file
    exists, the QC orchestrator runs a consistency copypass alongside the
    standard steps.
    """
    qc_dir = Path(work_dir) / "QC"
    qc_dir.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "artifact": "inheritance_translator",
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "volume_id": volume_id,
        "source_provider": source_provider,
        "refused_chapter": chapter_id,
        "refusal": {
            "error_code": refusal_code,
            "refusal_message": refusal_message,
            "status_code": status_code,
        },
        "completed_chapters_before_refusal": _prior_completed_chapters(work_dir, chapter_id),
        "deepseek_inheritance_config": inherit_config or {},
        "qc_instruction": _qc_instruction(source_provider),
    }
    path = qc_dir / _QC_ARTIFACT_NAME
    atomic_write_json(path, payload)
    return path


# ══════════════════════════════════════════════════════════════════════════
# Block build + injection
# ══════════════════════════════════════════════════════════════════════════

def build_inheritance_block(
    *,
    refusal_code: str,
    marker: str,
    summary: str,
    source_provider: str = "qwen",
    generated_at: Optional[str] = None,
) -> str:
    """Build the ``<translation_inheritance>`` block XML fragment."""
    ts = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f'<{_BLOCK_NAME} status="completed" owner="{_BLOCK_OWNER}" '
        f'source="{_xml_escape(source_provider.lower())}" target="deepseek" '
        f'refusal_code="{_xml_escape(refusal_code)}" generated_at="{ts}">\n'
        f"  <marker>{_xml_escape(marker)}</marker>\n"
        f"  <decision_summary>\n{_xml_escape(summary)}\n  </decision_summary>\n"
        f"</{_BLOCK_NAME}>"
    )


def inject_inheritance_block(context_path: Path, block_xml: str) -> None:
    """Upsert the ``<translation_inheritance>`` block into context.xml.

    Replaces an existing block of the same name (idempotent), preserving every
    other block and the root attributes. Writes atomically.
    """
    context_path = Path(context_path)
    if not context_path.exists():
        raise FileNotFoundError(f"context.xml not found at {context_path}")
    root = ET.parse(context_path).getroot()
    # Remove existing block of the same name (idempotent re-injection).
    for existing in root.findall(_BLOCK_NAME):
        root.remove(existing)
    new_block = ET.fromstring(block_xml)
    # Keep the block adjacent to the other runtime-owned blocks.
    anchor = root.find("character_attribute_anchors")
    if anchor is None:
        anchor = root.find("voice_fingerprints")
    if anchor is not None:
        idx = list(root).index(anchor)
        root.insert(idx + 1, new_block)
    else:
        root.append(new_block)
    ET.indent(root, space="  ")
    final_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    atomic_write_text(context_path, final_xml)


# ══════════════════════════════════════════════════════════════════════════
# Orchestration — the fallback entry point
# ══════════════════════════════════════════════════════════════════════════

def fallback_translate_chapter(
    *,
    work_dir: Path,
    volume_id: str,
    chapter_path: Path,
    chapter_id: str,
    refusal: BaseException,
    source_provider: str = "Qwen",
    refusal_code_default: str = "content_refusal",
    dry_run: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Translate one source-provider-refused chapter via DeepSeek with decision inheritance.

    Steps: capture refusal code → run inheritance agent → inject
    ``<translation_inheritance>`` into context.xml → build DeepSeekTranslator →
    translate the chapter → return the EN text.

    Returns the EN markdown text (the caller persists it via the normal
    translate_and_persist_chapter path). Raises the original refusal error
    if the fallback is disabled or cannot complete, so the run fails loudly
    rather than silently shipping a cold translation.
    """
    from src.Deepseek.translator.agent import DeepSeekTranslator  # lazy — avoids import cycle

    cfg = config or (get_config_section("translation").get("safety_fallback", {}) or {})
    if not cfg.get("enabled", True):
        raise refusal

    marker = str(cfg.get("marker") or _default_marker(source_provider))
    refusal_code = grab_refusal_code(refusal, default=refusal_code_default)

    try:
        summary = run_inheritance_agent(work_dir, volume_id, chapter_id, refusal_code, source_provider)
        logger.info(
            "[SAFETY-FALLBACK] %s — inheritance summary %d chars", chapter_id, len(summary)
        )
        block_xml = build_inheritance_block(
            refusal_code=refusal_code,
            marker=marker,
            summary=summary.strip() or "(inheritance agent returned no summary)",
            source_provider=source_provider,
        )
        inject_inheritance_block(Path(work_dir) / "context.xml", block_xml)
    except Exception as exc:  # noqa: BLE001 - a failed inheritance step must not silently cold-translate
        logger.error(
            "[SAFETY-FALLBACK] %s — inheritance/injection failed (%s); re-raising original "
            "refusal error so the run fails loudly.",
            chapter_id, exc,
        )
        raise refusal from exc

    try:
        _write_inheritance_translator_artifact(
            work_dir,
            volume_id=volume_id,
            chapter_id=chapter_id,
            refusal_code=refusal_code,
            refusal_message=str(refusal),
            status_code=getattr(refusal, "status_code", None),
            inherit_config=cfg,
            source_provider=source_provider,
        )
        logger.info(
            "[SAFETY-FALLBACK] %s — QC artifact written to QC/%s",
            chapter_id, _QC_ARTIFACT_NAME,
        )
    except Exception as exc:  # noqa: BLE001 - an audit artifact must never block the fallback
        logger.warning(
            "[SAFETY-FALLBACK] %s — QC artifact write failed (%s); continuing",
            chapter_id, exc,
        )

    logger.info(
        "[SAFETY-FALLBACK] %s — switching to DeepSeek (source=%s, inheritance block injected, "
        "refusal_code=%s)",
        chapter_id, source_provider, refusal_code,
    )
    translator = DeepSeekTranslator(
        work_dir=work_dir,
        volume_id=volume_id,
        dry_run=dry_run,
    )
    return translator.translate_chapter(chapter_path, {"chapter_id": chapter_id})


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
