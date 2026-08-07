"""Qwen-owned context.xml extraction for chapter guidance."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

_EPS_INTENSITY = {"COLD": 0, "COOL": 1, "NEUTRAL": 2, "WARM": 3, "HOT": 4}


def parse_voice_fingerprints(context_xml: Optional[str]) -> Dict[str, Dict[str, Any]]:
    profiles: Dict[str, Dict[str, Any]] = {}
    root = _parse(context_xml)
    block = root.find("voice_fingerprints") if root is not None else None
    if block is None:
        return profiles
    for fingerprint in block.findall("fingerprint"):
        name = fingerprint.get("character")
        if not name:
            continue
        profile: Dict[str, Any] = {}
        for source, target in (("archetype", "archetype"), ("register", "speech_register")):
            if fingerprint.get(source):
                profile[target] = fingerprint.get(source)
        contraction = fingerprint.find("contraction_rate")
        if contraction is not None and contraction.get("value") is not None:
            profile["contraction_rate"] = contraction.get("value")
        pattern = fingerprint.find("voice_pattern")
        if pattern is not None and pattern.text and pattern.text.strip():
            profile["speech_tics"] = [pattern.text.strip()]
        if profile:
            profiles[name] = profile
    return profiles


def parse_eps_signals(context_xml: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    chapters: Dict[str, List[Dict[str, Any]]] = {}
    root = _parse(context_xml)
    block = root.find("eps_signals") if root is not None else None
    if block is None:
        return chapters
    for chapter in block.findall("chapter"):
        chapter_id = chapter.get("id")
        if not chapter_id:
            continue
        chapters[chapter_id] = [
            {"name": character.get("name"), "eps": character.get("eps"), "band": (character.get("band") or "").upper() or None}
            for character in chapter.findall("character")
            if character.get("name")
        ]
    return chapters


def derive_chapter_eps_band(characters: List[Dict[str, Any]], default: str = "NEUTRAL") -> str:
    known = [str(item.get("band") or "").upper() for item in characters]
    known = [band for band in known if band in _EPS_INTENSITY]
    return max(known, key=_EPS_INTENSITY.__getitem__) if known else default


def parse_name_map(context_xml: Optional[str]) -> Dict[str, str]:
    """Build a bidirectional JP↔EN lookup from context.xml's <name_map> block.

    Returns a dict with both JP→EN and EN→JP entries — a flat bidirectional
    glossary.  Each JP form maps to exactly one EN form and vice versa.
    """
    root = _parse(context_xml)
    block = root.find("name_map") if root is not None else None
    if block is None:
        return {}
    lookup: Dict[str, str] = {}
    for entry in block.findall("entry"):
        jp = (entry.get("jp") or "").strip()
        en = (entry.get("en") or "").strip()
        if jp and en:
            lookup[jp] = en
            lookup[en] = jp
    return lookup


def parse_character_roster_handles(context_xml: Optional[str]) -> Dict[str, str]:
    """Build canonical_name → EN handle mapping from <character_roster>.

    The roster links canonical names (used by eps_signals) to JP handle names
    (the first name in <jp_name>, e.g. 'キリト' before the /).  Looks up the
    JP handle through the name_map to get the EN handle that voice_fingerprints
    keys on (e.g. 'Kirito').

    Returns {canonical_name: en_handle}.
    """
    root = _parse(context_xml)
    if root is None:
        return {}
    # Build a JP→EN mapping from the name_map block (raw, non-bidirectional).
    jp_to_en: Dict[str, str] = {}
    nm_block = root.find("name_map")
    if nm_block is not None:
        for entry in nm_block.findall("entry"):
            jp = (entry.get("jp") or "").strip()
            en = (entry.get("en") or "").strip()
            if jp and en:
                jp_to_en[jp] = en
    # Parse character roster.
    block = root.find("character_roster")
    if block is None:
        return {}
    mapping: Dict[str, str] = {}
    for character in block.findall("character"):
        canonical = (character.get("canonical_name") or "").strip()
        if not canonical:
            continue
        identity = character.find("identity")
        jp_name = ""
        if identity is not None:
            jp_name_el = identity.find("jp_name")
            if jp_name_el is not None and jp_name_el.text:
                # Extract handle: first name before '/' or ' / '
                jp_name = jp_name_el.text.strip().split("/")[0].strip()
        en_handle = canonical  # fall back to canonical name
        if jp_name:
            en_handle = jp_to_en.get(jp_name, canonical)
        mapping[canonical] = en_handle
    return mapping


def resolve_voice_aliases(
    voice_profiles: Dict[str, Dict[str, Any]],
    roster_handles: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """Expand voice_profiles so canonical names also key to the same profile.

    voice_profiles is keyed by EN handle names (e.g. "Kirito", "Asuna").
    roster_handles maps canonical names (from eps_signals, e.g.
    "Kirigaya Kazuto") → EN handle names.  After this call, canonical-name
    lookups resolve to the same profile dict as the handle name.
    """
    if not roster_handles:
        return voice_profiles
    resolved = dict(voice_profiles)
    for canonical_name, en_handle in roster_handles.items():
        if en_handle in voice_profiles and canonical_name not in resolved:
            resolved[canonical_name] = voice_profiles[en_handle]
    return resolved


def _parse(context_xml: Optional[str]) -> Optional[ET.Element]:
    if not context_xml:
        return None
    try:
        return ET.fromstring(context_xml)
    except ET.ParseError as exc:
        logger.warning("[QWEN-CONTEXT] context.xml parse failed: %s", exc)
        return None


def parse_volume_type(context_xml: Optional[str]) -> str:
    """Extract volume_type from context.xml's <volume_identity> block.

    Returns "mainline" or "spinoff" — empty string if missing/unparseable.
    """
    root = _parse(context_xml)
    if root is None:
        return ""
    vi = root.find("volume_identity")
    if vi is None:
        return ""
    vt = vi.find("volume_type")
    if vt is not None and vt.text:
        return vt.text.strip()
    return ""