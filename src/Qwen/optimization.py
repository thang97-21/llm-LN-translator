"""Qwen-specific prompt and request optimizations.

These are deliberately independent of DeepSeek DRDI/DOVB implementation.
"""

from typing import Any, Dict, List, Optional

_EPS_GUIDANCE = {
    "HOT": "Spend maximum attention on subtext, emotional reversals, voice, and literary technique.",
    "WARM": "Prioritize character voice, emotional escalation, and natural dialogue rhythm.",
    "NEUTRAL": "Preserve meaning, continuity, voice, and readable English without over-analysis.",
    "COOL": "Favor natural conversational English while checking names, register, and rhythm.",
    "COLD": "Favor accurate, efficient prose and verify names, terms, and structural fidelity.",
}

def build_eps_guidance(eps_band: str, enabled: bool = True) -> Optional[str]:
    if not enabled:
        return None
    band = str(eps_band or "NEUTRAL").upper()
    return f"<qwen_eps_guidance band=\"{band}\">{_EPS_GUIDANCE.get(band, _EPS_GUIDANCE['NEUTRAL'])}</qwen_eps_guidance>"


def build_voice_block(active_characters: Optional[List[Dict[str, Any]]], enabled: bool = True) -> Optional[str]:
    if not enabled or not active_characters:
        return None
    lines = ["<qwen_voice_profiles>"]
    for character in active_characters:
        name = character.get("name") or character.get("character")
        profile = character.get("fingerprint") or character.get("voice_profile") or {}
        if name and profile:
            lines.append(f"  <character name=\"{name}\">")
            for key in ("archetype", "speech_register", "contraction_rate", "speech_tics", "forbidden_vocabulary"):
                value = profile.get(key)
                if value not in (None, "", [], {}):
                    lines.append(f"    <{key}>{value}</{key}>")
            lines.append("  </character>")
    lines.append("</qwen_voice_profiles>")
    return "\n".join(lines) if len(lines) > 2 else None


def build_chapter_guidance(eps_band: str, active_characters: Optional[List[Dict[str, Any]]], cfg: Dict[str, Any], volume_type: str = "") -> str:
    parts = []
    if volume_type:
        parts.append(f"<qwen_volume_type>{volume_type}</qwen_volume_type>")
    if build_eps_guidance(eps_band, cfg.get("eps_guidance", True)):
        parts.append(build_eps_guidance(eps_band, cfg.get("eps_guidance", True)))
    if build_voice_block(active_characters, cfg.get("voice_block", True)):
        parts.append(build_voice_block(active_characters, cfg.get("voice_block", True)))
    return "\n\n".join(part for part in parts if part)


def supports_partial_prefix(thinking_enabled: bool, cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("partial_prefix", False) and not thinking_enabled)
