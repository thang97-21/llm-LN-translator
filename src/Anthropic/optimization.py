"""Anthropic-specific chapter guidance for the hybrid literary prompt."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.Deepseek.common.chapter_signals import HIGH_RISK_SIGNALS

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


def build_advisor_guidance(
    eps_band: str,
    record: Optional[Dict[str, Any]],
    previous_chapter_id: Optional[str] = None,
) -> str:
    """Layer 2 reinforcement for the proofreading advisor (spec §5), plus the
    deterministic previous-chapter review request.

    Two independent parts, either of which may be empty:

    1. **Uncertainty nudge** — "" when the chapter has neither a WARM/HOT
       eps_band nor a chapter_signals entry drawn from HIGH_RISK_SIGNALS.
       Reinforcement layered onto Layer 1's escalation block, not the trigger.

    2. **Review request** — emitted whenever ``previous_chapter_id`` is given.
       This is the fix for the clause that failed validation on volume c2cbb4
       (plan §6.4): `ESCALATION_BLOCK` asked the *executor* to request a review
       of the previous chapter's finished English, and across five chapters the
       executor never once carried that ask into a consult, because it frames
       every call around the chapter in front of it. The advisor answers only
       what it can see, so an instruction the executor may decline to relay is
       not an instruction at all. Putting the request here makes it part of the
       per-chapter envelope the executor is handed rather than prose it has to
       choose to act on.

       Deliberately NOT gated on eps_band or signals: whether the *previous*
       chapter deserves a second read has nothing to do with how emotionally
       intense *this* one is. It costs nothing on a chapter whose consult never
       happens, since the tool is attached separately by ``_tools_qualify``.

    ``record`` is the same ``{"signals": [...], "speakers": [...]}`` shape
    ``build_chapter_signal_guidance`` already consumes (``self._chapter_signals
    .get(chapter_id)`` in agent.py) — not a bare list of signal dicts.
    """
    parts: List[str] = []

    signals = (record or {}).get("signals") or []
    high_risk = sorted({
        str(signal.get("name") or "")
        for signal in signals
        if isinstance(signal, dict) and signal.get("name") in HIGH_RISK_SIGNALS
    })
    band = str(eps_band or "").upper()
    if band in ("WARM", "HOT") or high_risk:
        reasons = []
        if band in ("WARM", "HOT"):
            reasons.append(f"eps_band={band}")
        if high_risk:
            reasons.append(f"flagged signals: {', '.join(high_risk)}")
        parts.append(
            "Project context flags this chapter (" + "; ".join(reasons) + "). Before consulting "
            "the advisor, identify in your own reasoning the specific point in this chapter's "
            "rendering you are least certain of. Consult with that concern in mind, not as a "
            "general check-in."
        )

    if previous_chapter_id:
        parts.append(
            f"{previous_chapter_id}'s finished English is in this conversation. If you consult the "
            "advisor for this chapter, your consult MUST carry this as a separate, explicit second "
            f"question, whatever else you ask: \"Report any defect in {previous_chapter_id}'s "
            "finished English — a seam, an unintended repetition across a paragraph break, a "
            "register slip against that chapter's own voice profiles, or a device that died in the "
            "crossing. Findings only, each naming its line; say plainly if there are none.\" Ask it "
            "even when this chapter raises no question of its own, and ask it even if you believe "
            f"{previous_chapter_id} was clean — a defect nobody looks for is a defect that ships. "
            "Apply any finding to your own prose in your own words, or record why you declined."
        )

    return "\n\n".join(parts)


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
