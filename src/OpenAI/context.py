"""OpenAI-owned context.xml extraction for chapter-local guidance."""

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
            {
                "name": character.get("name"),
                "eps": character.get("eps"),
                "band": (character.get("band") or "").upper() or None,
            }
            for character in chapter.findall("character")
            if character.get("name")
        ]
    return chapters


def derive_chapter_eps_band(characters: List[Dict[str, Any]], default: str = "NEUTRAL") -> str:
    known = [str(item.get("band") or "").upper() for item in characters]
    known = [band for band in known if band in _EPS_INTENSITY]
    return max(known, key=_EPS_INTENSITY.__getitem__) if known else default


def parse_character_roster_handles(context_xml: Optional[str]) -> Dict[str, str]:
    root = _parse(context_xml)
    if root is None:
        return {}
    jp_to_en: Dict[str, str] = {}
    name_map = root.find("name_map")
    if name_map is not None:
        for entry in name_map.findall("entry"):
            jp, en = (entry.get("jp") or "").strip(), (entry.get("en") or "").strip()
            if jp and en:
                jp_to_en[jp] = en
    roster = root.find("character_roster")
    if roster is None:
        return {}
    handles: Dict[str, str] = {}
    for character in roster.findall("character"):
        canonical = (character.get("canonical_name") or "").strip()
        if not canonical:
            continue
        jp_name = ""
        identity = character.find("identity")
        if identity is not None:
            jp_name_element = identity.find("jp_name")
            if jp_name_element is not None and jp_name_element.text:
                jp_name = jp_name_element.text.strip().split("/")[0].strip()
        handles[canonical] = jp_to_en.get(jp_name, canonical)
    return handles


def resolve_voice_aliases(voice_profiles: Dict[str, Dict[str, Any]], roster_handles: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    resolved = dict(voice_profiles)
    for canonical_name, en_handle in roster_handles.items():
        if en_handle in voice_profiles and canonical_name not in resolved:
            resolved[canonical_name] = voice_profiles[en_handle]
    return resolved


def parse_volume_type(context_xml: Optional[str]) -> str:
    root = _parse(context_xml)
    volume_identity = root.find("volume_identity") if root is not None else None
    volume_type = volume_identity.find("volume_type") if volume_identity is not None else None
    return volume_type.text.strip() if volume_type is not None and volume_type.text else ""


def _parse(context_xml: Optional[str]) -> Optional[ET.Element]:
    if not context_xml:
        return None
    try:
        return ET.fromstring(context_xml)
    except ET.ParseError as exc:
        logger.warning("[OPENAI-CONTEXT] context.xml parse failed: %s", exc)
        return None
