"""GLM EPS-to-reasoning policy and chapter guidance."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

_DIRECTIVES_PATH = Path(__file__).with_name("prompts") / "eps_directives_glm.xml"


def effort_for_band(eps_band: str, configured: str = "high") -> str:
    _, effort = directive_for_band(eps_band)
    return effort if effort in {"low", "high", "max"} else (configured if configured in {"low", "high", "max"} else "high")


def strategy_for_band(eps_band: str) -> str:
    directive, _ = directive_for_band(eps_band)
    return directive


def directive_for_band(eps_band: str) -> Tuple[str, str]:
    """Return (prompt directive, request effort) from Claude's data asset.

    Missing or malformed prompt data degrades to an empty directive and high
    effort; the client remains usable without silently inventing policy prose.
    """
    band = str(eps_band or "NEUTRAL").upper()
    try:
        root = ET.parse(_DIRECTIVES_PATH).getroot()
        for element in root.findall("directive"):
            if str(element.get("band") or "").upper() == band:
                return " ".join("".join(element.itertext()).split()), str(element.get("reasoning_effort") or "high").lower()
    except (OSError, ET.ParseError):
        pass
    return "", "high"


def build_voice_block(active_characters: Optional[List[Dict[str, Any]]], enabled: bool = True) -> Optional[str]:
    if not enabled or not active_characters:
        return None
    lines = ["<glm_voice_profiles>"]
    for character in active_characters:
        name = character.get("name") or character.get("character")
        profile = character.get("fingerprint") or character.get("voice_profile") or {}
        if not name or not profile:
            continue
        lines.append(f'  <character name="{name}">')
        for key in ("archetype", "speech_register", "contraction_rate", "speech_tics", "forbidden_vocabulary"):
            value = profile.get(key)
            if value not in (None, "", [], {}):
                lines.append(f"    <{key}>{value}</{key}>")
        lines.append("  </character>")
    lines.append("</glm_voice_profiles>")
    return "\n".join(lines) if len(lines) > 2 else None


def build_chapter_guidance(eps_band: str, active_characters: Optional[List[Dict[str, Any]]], cfg: Dict[str, Any], volume_type: str = "") -> str:
    band = str(eps_band or "NEUTRAL").upper()
    parts = [f'<glm_chapter_guidance eps_band="{band}">']
    if volume_type:
        parts.append(f"  <volume_type>{volume_type}</volume_type>")
    voice = build_voice_block(active_characters, cfg.get("voice_block", True))
    if voice:
        parts.append(voice)
    parts.append("</glm_chapter_guidance>")
    return "\n".join(parts)
