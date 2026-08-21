"""OpenAI-specific chapter guidance for the hybrid literary prompt."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_EPS_GUIDANCE = {
    "HOT": "Spend maximum attention on subtext, emotional reversals, distinct voice, and literary technique.",
    "WARM": "Prioritize character voice, emotional escalation, and natural dialogue rhythm.",
    "NEUTRAL": "Preserve meaning, continuity, voice, and readable English without over-analysis.",
    "COOL": "Favor natural conversational English while checking names, register, and rhythm.",
    "COLD": "Favor accurate, efficient prose and verify names, terms, and structural fidelity.",
}


def build_eps_guidance(eps_band: str, enabled: bool = True) -> Optional[str]:
    if not enabled:
        return None
    band = str(eps_band or "NEUTRAL").upper()
    guidance = _EPS_GUIDANCE.get(band, _EPS_GUIDANCE["NEUTRAL"])
    return f'<chapter_emotional_guidance band="{band}">{guidance}</chapter_emotional_guidance>'


def build_voice_block(active_characters: Optional[List[Dict[str, Any]]], enabled: bool = True) -> Optional[str]:
    if not enabled or not active_characters:
        return None
    lines = ["<active_voice_profiles>"]
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
    lines.append("</active_voice_profiles>")
    return "\n".join(lines) if len(lines) > 2 else None


def build_chapter_guidance(eps_band: str, active_characters: Optional[List[Dict[str, Any]]], cfg: Dict[str, Any], volume_type: str = "") -> str:
    parts = []
    if volume_type:
        parts.append(f"<volume_type>{volume_type}</volume_type>")
    eps = build_eps_guidance(eps_band, bool(cfg.get("eps_guidance", True)))
    voices = build_voice_block(active_characters, bool(cfg.get("voice_block", True)))
    if eps:
        parts.append(eps)
    if voices:
        parts.append(voices)
    return "\n\n".join(parts)
