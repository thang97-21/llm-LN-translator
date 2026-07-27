"""
Lightweight QC — filesystem-only sanity gate, zero API calls, <5s per volume.

Replaces the entire mtl-quality-evaluator skill for this client (see
PLANNING.md Step 6.5). Deliberately NOT the full thing: no 3-model fan-out,
no LCI/SSP scoring, no Koji Fox voice fidelity, no ECR/VREC/ADN checks, no
interactive review. This catches the 95th-percentile failure modes —
missing output, truncation, token-count outliers, name drift — with pure
filesystem reads and string analysis. The full evaluator remains available
in the main pipeline for production-grade quality assessment.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any, Dict, List, Set
from xml.etree import ElementTree as ET

from src.common.config import WORK_DIR

# Tries a real tokenizer for the token-sanity check; falls back to a
# character-count heuristic. No API dependency either way — this must run
# without a DEEPSEEK_API_KEY, unlike prep/translate.
_tokenizer = None
_tokenizer_tried = False


def _count_tokens(text: str) -> int:
    global _tokenizer, _tokenizer_tried
    if not text:
        return 0
    if not _tokenizer_tried:
        _tokenizer_tried = True
        try:
            import tiktoken
            _tokenizer = tiktoken.get_encoding("o200k_base")
        except Exception:
            _tokenizer = None
    if _tokenizer is not None:
        try:
            return len(_tokenizer.encode(text))
        except Exception:
            pass
    return len(text) // 4


_SENTENCE_END_RE = re.compile(r'[.!?…」』"\'\)]\s*$')
_SCENE_BREAK_RE = re.compile(r'^\s*(\*\s*\*\s*\*|[◆◇★☆▼▽●○※§]{1,5})\s*$', re.MULTILINE)
_HEADING_RE = re.compile(r'^#\s+\S', re.MULTILINE)
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z']{2,}\b")


def _locked_names(context_xml_path: Path) -> Set[str]:
    """Pull canonical EN names out of context.xml's name_map + character_roster."""
    if not context_xml_path.exists():
        return set()
    try:
        root = ET.fromstring(context_xml_path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return set()

    names: Set[str] = set()

    name_map = root.find("name_map")
    if name_map is not None:
        for entry in name_map.findall("entry"):
            en = entry.get("en", "").strip()
            if en:
                names.add(en)

    roster = root.find("character_roster")
    if roster is not None:
        for character in roster.findall("character"):
            canonical = character.get("canonical_name", "").strip()
            if canonical:
                names.add(canonical)

    return names


def _name_drift(locked_names: Set[str], combined_en_text: str) -> List[Dict[str, str]]:
    """Fuzzy-match locked name tokens against capitalized tokens actually used in
    the EN output. A close-but-not-exact, frequently-used match is a drift candidate
    (e.g. context.xml locks "Haruto" but the model wrote "Haruoto" repeatedly)."""
    if not locked_names or not combined_en_text:
        return []

    locked_tokens: Set[str] = set()
    for name in locked_names:
        locked_tokens.update(name.split())

    used_tokens: Dict[str, int] = {}
    for match in _PROPER_NOUN_RE.findall(combined_en_text):
        used_tokens[match] = used_tokens.get(match, 0) + 1

    drifts: List[Dict[str, str]] = []
    for locked in sorted(locked_tokens):
        candidates = [
            tok for tok in used_tokens
            if tok != locked and used_tokens[tok] >= 2
            and difflib.SequenceMatcher(None, tok, locked).ratio() >= 0.75
        ]
        for candidate in candidates:
            drifts.append({"locked": locked, "drift": candidate, "occurrences": used_tokens[candidate]})
    return drifts


def run_qc(volume_id: str) -> Dict[str, Any]:
    """Post-translation quality gate. See module docstring."""
    work_dir = WORK_DIR / volume_id
    jp_dir = work_dir / "JP"
    en_dir = work_dir / "EN"
    context_xml_path = work_dir / "context.xml"

    jp_files = sorted(jp_dir.glob("*.md")) if jp_dir.is_dir() else []

    completeness_missing: List[str] = []
    truncation_flags: List[str] = []
    token_outliers: List[Dict[str, Any]] = []
    structural_flags: List[str] = []
    en_texts: List[str] = []

    for jp_path in jp_files:
        chapter_id = jp_path.stem
        en_path = en_dir / f"{chapter_id}_EN.md"
        if not en_path.exists():
            completeness_missing.append(chapter_id)
            continue

        en_text = en_path.read_text(encoding="utf-8")
        en_texts.append(en_text)
        jp_text = jp_path.read_text(encoding="utf-8")

        stripped = en_text.rstrip()
        if stripped and not _SENTENCE_END_RE.search(stripped):
            truncation_flags.append(chapter_id)

        jp_tokens = _count_tokens(jp_text)
        en_tokens = _count_tokens(en_text)
        if jp_tokens > 0:
            ratio = en_tokens / jp_tokens
            if ratio < 0.8 or ratio > 1.5:
                token_outliers.append({
                    "chapter_id": chapter_id, "jp_tokens": jp_tokens,
                    "en_tokens": en_tokens, "ratio": round(ratio, 3),
                })

        if not _HEADING_RE.search(en_text):
            structural_flags.append(f"{chapter_id}: no top-level heading")
        jp_breaks = len(_SCENE_BREAK_RE.findall(jp_text))
        en_breaks = len(_SCENE_BREAK_RE.findall(en_text))
        if jp_breaks > 0 and en_breaks == 0:
            structural_flags.append(f"{chapter_id}: {jp_breaks} JP scene break(s), 0 in EN")

    locked_names = _locked_names(context_xml_path)
    name_drifts = _name_drift(locked_names, "\n".join(en_texts))

    passed = not (completeness_missing or truncation_flags or name_drifts)

    recommendations: List[str] = []
    if completeness_missing:
        recommendations.append(f"Translate missing chapters: {', '.join(completeness_missing)}")
    if truncation_flags:
        recommendations.append(f"Re-check possible truncation in: {', '.join(truncation_flags)}")
    if token_outliers:
        recommendations.append(f"{len(token_outliers)} chapter(s) have an unusual JP:EN length ratio — spot-check them")
    if name_drifts:
        recommendations.append(f"{len(name_drifts)} possible name-spelling drift(s) — see name_drifts")
    if structural_flags:
        recommendations.append(f"{len(structural_flags)} structural issue(s) — see structural_flags")

    return {
        "schema": "QCReport",
        "volume_id": volume_id,
        "passed": passed,
        "completeness": {
            "jp_chapter_count": len(jp_files),
            "en_chapter_count": len(jp_files) - len(completeness_missing),
            "missing": completeness_missing,
        },
        "truncation_flags": truncation_flags,
        "token_outliers": token_outliers,
        "name_drifts": name_drifts,
        "structural_flags": structural_flags,
        "recommendations": recommendations,
    }
