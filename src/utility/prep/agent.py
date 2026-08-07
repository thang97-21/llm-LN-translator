"""
DeepSeekPrepAgent — unified single-DeepSeek-API-call context.xml builder.

Fills every <pending> block in the Librarian's barebone context.xml (see
src/librarian/agent.py::_write_context_placeholder) in one shot: no Gemini,
no ChromaDB, no metadata-phase pipeline, no main-pipeline subprocess. One
DeepSeek V4 Pro call reads the full JP volume text (plus a prior-volume
series bible, if this is a sequel) and returns the fully populated document.

This is deliberately NOT built on top of DeepSeekClient (src/translator/
deepseek_client.py) — that class is tuned for the chaptered, conversation-
aware translation loop (prefix cache monitor, persistent conversation
manager, per-chapter cost aggregation). Prep is one stateless call; a
purpose-built minimal client keeps prep's behavior from silently drifting
whenever someone tunes the translator's config.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from src.Deepseek.common.atomic_io import atomic_write_json, atomic_write_text
from src.Deepseek.common.config import WORK_DIR, get_config_section

logger = logging.getLogger(__name__)

_XML_FENCE_RE = re.compile(r"^```(?:xml)?\s*|\s*```$", re.MULTILINE)


class PrepError(RuntimeError):
    """Raised when prep cannot proceed (missing context.xml, malformed model output, ...)."""


# ══════════════════════════════════════════════════════════════════════════
# Minimal one-shot DeepSeek call (not DeepSeekClient — see module docstring)
# ══════════════════════════════════════════════════════════════════════════

def _call_deepseek_prep(system_instruction: str, user_message: str, *, volume_id: str) -> str:
    try:
        import anthropic as _anthropic_mod
    except ImportError as exc:
        raise PrepError(
            "Prep requires the Anthropic SDK (pip install anthropic) — "
            "DeepSeek uses an Anthropic-compatible API format."
        ) from exc

    cfg = get_config_section("prep")
    api_key_env = str(cfg.get("api_key_env", "DEEPSEEK_API_KEY"))
    api_key = os.getenv(api_key_env)
    if not api_key:
        raise PrepError(f"{api_key_env} not set — cannot run prep.")

    base_url = str(cfg.get("endpoint", "https://api.deepseek.com/anthropic")).rstrip("/")
    model = str(cfg.get("model", "deepseek-v4-pro"))
    max_output_tokens = int(cfg.get("max_output_tokens", 128000))
    thinking_budget = int(cfg.get("thinking_budget", 48000))
    effort = str(cfg.get("effort", "max"))
    timeout_seconds = float(cfg.get("http_timeout_seconds", 900))

    # Same env-var conflict avoidance as DeepSeekClient.__init__: the Anthropic
    # SDK auto-discovers ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY, which may hold
    # Claude Code / platform keys that 401 against the DeepSeek endpoint.
    saved_auth_token = os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    saved_api_key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ["ANTHROPIC_API_KEY"] = api_key
        client = _anthropic_mod.Anthropic(
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
    finally:
        if saved_auth_token is not None:
            os.environ["ANTHROPIC_AUTH_TOKEN"] = saved_auth_token
        if saved_api_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_api_key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    response = client.messages.create(
        model=model,
        max_tokens=max_output_tokens,
        system=system_instruction,
        messages=[{"role": "user", "content": user_message}],
        thinking={"type": "enabled", "budget_tokens": thinking_budget},
        output_config={"effort": effort},
    )

    text_parts: List[str] = []
    thinking_parts: List[str] = []
    for block in getattr(response, "content", []):
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif block_type == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")

    from src.Deepseek.common.token_telemetry import count_tokens, log_call
    usage = getattr(response, "usage", None)
    cache_hit_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage is not None else 0
    total_input_tokens = count_tokens(system_instruction + "\n\n" + user_message, model)
    output_tokens = count_tokens("".join(text_parts) + "".join(thinking_parts), model)
    log_call(
        phase="prep",
        volume_id=volume_id,
        call_label="unified",
        model=model,
        cache_hit_tokens=cache_hit_tokens,
        fresh_tokens=max(0, total_input_tokens - cache_hit_tokens),
        output_tokens=output_tokens,
    )

    return "".join(text_parts).strip()


# ══════════════════════════════════════════════════════════════════════════
# Input assembly
# ══════════════════════════════════════════════════════════════════════════

def _read_jp_chapters(work_dir: Path) -> List[Tuple[str, str]]:
    jp_dir = work_dir / "JP"
    if not jp_dir.is_dir():
        raise PrepError(f"No JP/ directory for this volume at {jp_dir} — run extract first.")
    chapters = []
    for path in sorted(jp_dir.glob("*.md")):
        chapters.append((path.stem, path.read_text(encoding="utf-8")))
    if not chapters:
        raise PrepError(f"JP/ directory at {jp_dir} has no chapter files.")
    return chapters


def _wrap_jp_chapters(chapters: List[Tuple[str, str]]) -> str:
    parts = ["<jp_chapters>"]
    for chapter_id, text in chapters:
        parts.append(f'  <chapter id="{chapter_id}">')
        parts.append(text.strip())
        parts.append("  </chapter>")
    parts.append("</jp_chapters>")
    return "\n".join(parts)


def _extract_opf_title_jp(context_xml_text: str) -> str:
    try:
        root = ET.fromstring(context_xml_text)
    except ET.ParseError:
        return ""
    opf = root.find("opf_metadata")
    if opf is None or not opf.text:
        return ""
    try:
        opf_data = json.loads(opf.text)
    except (json.JSONDecodeError, TypeError):
        return ""
    for key in ("dc_title_jp", "title", "dc:title"):
        value = opf_data.get(key)
        if value:
            return str(value).strip()
    return ""


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def discover_series_bible(
    context_xml_text: str,
    bible_dir: Path,
    explicit_series_id: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Best-effort sequel detection: match this volume's JP title against every
    bibles/<series_id>/series_pack.json's series_title_jp. Simplified sibling
    of the main pipeline's build_context_xml.py::_discover_series_id — good
    enough for "did we already translate volume 1 of this," not a general
    fuzzy-matching engine.
    """
    if not bible_dir.exists():
        return None

    if explicit_series_id:
        candidate = bible_dir / explicit_series_id
        if candidate.is_dir():
            return explicit_series_id, _load_bible_json(candidate)
        return None

    title_jp = _extract_opf_title_jp(context_xml_text)
    if not title_jp:
        return None
    base_title = re.sub(r"[【［][^】］]*[】］]", "", title_jp).strip()

    for folder in sorted(bible_dir.iterdir()):
        if not folder.is_dir():
            continue
        pack_path = folder / "series_pack.json"
        if not pack_path.exists():
            continue
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        series_title_jp = str(pack.get("series_title_jp", "")).strip()
        if series_title_jp and (series_title_jp in base_title or base_title in series_title_jp):
            return folder.name, _load_bible_json(folder)

    return None


def _load_bible_json(bible_folder: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for name in ("term_lock.json", "verbatim_anchors.json", "series_pack.json"):
        path = bible_folder / name
        if path.exists():
            try:
                result[name.replace(".json", "")] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    return result


# ══════════════════════════════════════════════════════════════════════════
# Output parsing
# ══════════════════════════════════════════════════════════════════════════

def _strip_fences(text: str) -> str:
    return _XML_FENCE_RE.sub("", text).strip()


def _block_text(root: ET.Element, tag: str) -> str:
    el = root.find(tag)
    if el is None:
        return ""
    return (el.text or "").strip()


def _is_pending(root: ET.Element, tag: str) -> bool:
    el = root.find(tag)
    if el is None:
        return True
    return el.find("pending") is not None


def _build_manifest_metadata(root: ET.Element) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Flat v3.0-style metadata/metadata_en pair — the fields Builder's
    _resolve_book_title() checks first, before any legacy nested-schema branch."""
    identity = root.find("volume_identity")
    if identity is None:
        return {}, {}

    def field(name: str) -> str:
        el = identity.find(name)
        return (el.text or "").strip() if el is not None else ""

    metadata = {
        "title": field("title_jp"),
        "author": field("author_jp"),
        "publisher": field("publisher"),
        "series": field("series_en"),
        "series_index": field("volume_number"),
    }
    metadata_en = {
        "title_en": field("title_en"),
        "author_en": field("author_en"),
    }
    return metadata, metadata_en


def _extract_translation_brief(root: ET.Element) -> str:
    brief = root.find("translation_brief")
    if brief is None:
        return ""
    raw = brief.find("raw")
    if raw is not None and raw.text:
        return raw.text.strip()
    return "".join(brief.itertext()).strip()


def _count_characters(root: ET.Element) -> int:
    roster = root.find("character_roster")
    if roster is None:
        return 0
    return len(roster.findall("character"))


_BLOCK_NAMES = (
    "validation_audit", "volume_identity", "world_setting", "character_roster",
    "name_map", "relationship_graph", "verbatim_anchors", "character_attribute_anchors",
    "voice_fingerprints", "cultural_glossary", "eps_arc_tracker", "scene_plans",
    "eps_signals", "illustration_context", "translation_brief",
)


# ══════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════

def _finalize_and_write(
    work_dir: Path,
    context_path: Path,
    root: ET.Element,
    *,
    resolved_series_id: Optional[str],
    chapters: List[Tuple[str, str]],
    bible_match: Optional[Tuple[str, Dict[str, Any]]],
    generated_by: str,
) -> Dict[str, Any]:
    """Shared tail of both prep paths: stamp root attrs, write context.xml,
    update manifest.json, extract the translation brief, build the receipt.
    Split out so the parallel cache-warmed path (parallel_agent.py) doesn't
    reimplement it — same output contract, different way of filling blocks."""
    root.set("generated_by", generated_by)
    root.set("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    if resolved_series_id:
        root.set("series_id", resolved_series_id)

    ET.indent(root, space="  ")
    final_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    atomic_write_text(context_path, final_xml)

    metadata, metadata_en = _build_manifest_metadata(root)
    manifest_path = work_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest["metadata"] = metadata
    manifest["metadata_en"] = metadata_en
    blocks_populated_count = sum(1 for name in _BLOCK_NAMES if not _is_pending(root, name))
    manifest.setdefault("pipeline_state", {})["prep"] = {
        "status": "completed",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "blocks_populated": blocks_populated_count,
        "blocks_total": len(_BLOCK_NAMES),
    }
    atomic_write_json(manifest_path, manifest)

    brief_text = _extract_translation_brief(root)
    if brief_text:
        atomic_write_text(work_dir / "TRANSLATION_BRIEF.md", brief_text)

    blocks_populated = [name for name in _BLOCK_NAMES if not _is_pending(root, name)]
    return {
        "volume_id": work_dir.name,
        "chapter_count": len(chapters),
        "character_count": _count_characters(root),
        "series_id": resolved_series_id or "",
        "is_sequel": bool(bible_match),
        "bible_used": bible_match[0] if bible_match else "",
        "blocks_populated": blocks_populated,
        "blocks_pending": [n for n in _BLOCK_NAMES if n not in blocks_populated],
    }


def run_prep(volume_id: str, series_id: Optional[str] = None) -> Dict[str, Any]:
    """Full pre-translation prep for one volume. See module docstring.

    Dispatches to one of two alternative paths before falling through to the
    unified single-call path below, each gated by its own config.yaml flag
    and each with its own fallback_to_unified safety net on failure:

      1. prep.multi_turn.enabled — the persisted, sequential DeepSeek-
         conversation path (multiturn_agent.py). Checked FIRST: do not
         enable this alongside prep.parallel at the same time.
      2. prep.parallel.enabled — the cache-warmed concurrent fan-out path
         (parallel_agent.py). See PARALLEL_PREP_GUIDE.md.
    """
    prep_cfg = get_config_section("prep")

    multi_turn_cfg = prep_cfg.get("multi_turn", {}) or {}
    if multi_turn_cfg.get("enabled", False):
        from src.utility.prep.multiturn_agent import run_multiturn_prep

        try:
            return run_multiturn_prep(volume_id, series_id=series_id)
        except Exception as exc:
            if not multi_turn_cfg.get("fallback_to_unified", True):
                raise
            logger.warning(
                "[PREP] %s — multi-turn path failed (%s); falling back to unified Pro call.",
                volume_id, exc,
            )

    parallel_cfg = prep_cfg.get("parallel", {}) or {}
    if parallel_cfg.get("enabled", False):
        from src.utility.prep.parallel_agent import run_parallel_prep

        try:
            return run_parallel_prep(volume_id, series_id=series_id)
        except Exception as exc:
            if not parallel_cfg.get("fallback_to_unified", True):
                raise
            logger.warning(
                "[PREP] %s — parallel path failed (%s); falling back to unified Pro call.",
                volume_id, exc,
            )

    work_dir = WORK_DIR / volume_id
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise PrepError(f"No context.xml at {context_path} — run extract first.")

    existing_context_xml = context_path.read_text(encoding="utf-8")
    chapters = _read_jp_chapters(work_dir)

    bible_dir = Path(prep_cfg.get("bible_dir", "bibles/"))
    if not bible_dir.is_absolute():
        from src.Deepseek.common.config import PIPELINE_ROOT
        bible_dir = PIPELINE_ROOT / bible_dir

    bible_match = discover_series_bible(existing_context_xml, bible_dir, series_id)
    resolved_series_id = bible_match[0] if bible_match else series_id

    user_parts = [f"<existing_context_xml>\n{existing_context_xml}\n</existing_context_xml>"]
    if bible_match:
        _, bible_data = bible_match
        user_parts.append(
            "<bible_context>\n" + json.dumps(bible_data, ensure_ascii=False, indent=2) + "\n</bible_context>"
        )
    user_parts.append(_wrap_jp_chapters(chapters))
    user_message = "\n\n".join(user_parts)

    prep_prompt_path = Path(prep_cfg.get("prompt", "src/Deepseek/prompt/prep_prompt_deepseek_en.xml"))
    if not prep_prompt_path.is_absolute():
        from src.Deepseek.common.config import PIPELINE_ROOT
        prep_prompt_path = PIPELINE_ROOT / prep_prompt_path
    system_instruction = prep_prompt_path.read_text(encoding="utf-8")

    logger.info("[PREP] %s — sending %d chapters to DeepSeek (sequel=%s)",
                volume_id, len(chapters), bool(bible_match))
    raw_response = _call_deepseek_prep(system_instruction, user_message, volume_id=volume_id)
    populated_xml = _strip_fences(raw_response)

    try:
        root = ET.fromstring(populated_xml)
    except ET.ParseError as exc:
        raise PrepError(
            f"DeepSeek returned malformed context.xml ({exc}) — refusing to overwrite "
            f"the existing file. Raw response saved for inspection."
        ) from exc

    if root.tag != "mtls_project_context":
        raise PrepError(
            f"DeepSeek response root is <{root.tag}>, expected <mtls_project_context> — refusing to write."
        )

    receipt = _finalize_and_write(
        work_dir, context_path, root,
        resolved_series_id=resolved_series_id, chapters=chapters,
        bible_match=bible_match, generated_by="deepseek_prep",
    )
    logger.info("[PREP] %s — done. %d/%d blocks populated.",
                volume_id, len(receipt["blocks_populated"]), len(_BLOCK_NAMES))
    return receipt
