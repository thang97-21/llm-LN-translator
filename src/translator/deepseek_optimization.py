"""
DeepSeek V4 Pro Optimization Module.

Provides DeepSeek-specific prompt-level adaptations that compensate for
features absent in the DeepSeek API:

  1. Reasoning Directive Injection (DRDI)
     Since DeepSeek ignores the Anthropic `effort` parameter, EPS-band
     CoT scaffolding is injected into the system prompt to approximate
     the effort routing DeepSeek lacks at the API level.

  2. DeepSeek-Optimized Voice Block (DOVB)
     Structured, template-style voice representations tailored to
     DeepSeek's reasoning style — explicit contraction rewrite rules,
     "show don't tell" demonstration blocks, character archetype anchoring.

  3. Concurrent Chapter Translation (CCT)
     Async chapter processor for parallel translation when Anthropic
     Batch API is unavailable on the DeepSeek route.

All methods are pure functions — no side effects, no imports beyond stdlib.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 1. REASONING DIRECTIVE INJECTION (DRDI)
# ══════════════════════════════════════════════════════════════════════════════

# EPS-band → CoT directive map.
# These replace the Anthropic `effort` parameter that DeepSeek ignores.
# The directives are injected as a <deepseek_reasoning_directive> XML block
# in the system prompt, placed after the translation brief and before the
# source text block.

_EPS_BAND_REASONING_DIRECTIVES: Dict[str, str] = {
    "HOT": (
        "Apply maximum analytical rigor to this scene. For each sentence, examine "
        "subtext, cultural nuance, character voice fidelity, emotional resonance, "
        "and literary device preservation. Walk through your reasoning step by step: "
        "identify the JP subtext → select the optimal EN register → verify character "
        "voice alignment → check for literary technique fidelity → confirm emotional "
        "resonance. No sentence should pass without at least two of these checks. "
        "This is a HOT-tagged scene — confession, combat, trauma, or peak emotional "
        "content demands your full reasoning capacity."
    ),
    "WARM": (
        "Apply balanced analytical rigor to this scene. Prioritize character voice "
        "consistency, emotional escalation tracking, and natural narrative flow. "
        "For key dialogue exchanges, verify that each character's speech register "
        "matches their established voice fingerprint. For exposition transitions, "
        "check that the emotional arc is smooth and progressive. Let your reasoning "
        "focus where the character dynamics demand it, and maintain momentum through "
        "lower-stakes connective tissue."
    ),
    "NEUTRAL": (
        "Apply efficient analytical rigor to this scene. The primary task is accurate, "
        "natural-sounding prose that maintains character voice continuity and narrative "
        "coherence. Spot-check dialogue for voice fingerprint compliance and verify "
        "that transition sentences preserve scene rhythm. Do not overanalyze — this "
        "is general narrative prose where correctness and readability are paramount."
    ),
    "COOL": (
        "Apply light analytical rigor to this scene — casual dialogue, exposition, "
        "or low-stakes character interaction. Prioritize natural, conversational EN "
        "output. Verify contraction patterns and voice fingerprint basics. Don't "
        "linger on subtext analysis unless the JP source clearly signals subtext. "
        "Throughput matters here: be accurate, be consistent, be efficient."
    ),
    "COLD": (
        "Apply minimal analytical rigor — Oku Hanako throughput mode. This is "
        "low-intensity setup, character introduction, or procedural narration. "
        "Focus on accuracy and momentum. Check names and honorifics for consistency. "
        "Do not think more than 5 sentences ahead. Trust your training data for "
        "standard phrasing. Speed and precision override literary craft."
    ),
}


def get_reasoning_directive(
    eps_band: str,
    enabled: bool = True,
) -> Optional[str]:
    """
    Return the EPS-band reasoning directive XML block for DeepSeek.

    When *enabled* is False, returns None (directive injection skipped).

    Args:
        eps_band: EPS intensity label (HOT, WARM, NEUTRAL, COOL, COLD).
        enabled: Whether DRDI is active (from providers.yaml).

    Returns:
        XML-formatted directive string, or None if disabled/unknown band.
    """
    if not enabled:
        return None

    band = str(eps_band).strip().upper()
    directive = _EPS_BAND_REASONING_DIRECTIVES.get(band)

    if not directive:
        logger.debug(
            "[DEEPSEEK-DRDI] Unknown EPS band '%s' — no directive injected.", eps_band,
        )
        return None

    return (
        "<deepseek_reasoning_directive>\n"
        f"<!-- EPS band: {band} -->\n"
        f"{directive}\n"
        "</deepseek_reasoning_directive>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. DEEPSEEK-OPTIMIZED VOICE BLOCK (DOVB)
# ══════════════════════════════════════════════════════════════════════════════


def build_deepseek_voice_block(
    character_name: str,
    voice_profile: Dict[str, Any],
    chapter_eps_band: str = "NEUTRAL",
) -> Optional[str]:
    """
    Build a DeepSeek-optimized voice representation for a character.

    DeepSeek's reasoning style benefits from structured, template-style
    voice instructions rather than the narrative-style fingerprints
    designed for Claude's interpretive reasoning.

    The output is a compact <deepseek_voice_profile> XML block with:
      - Explicit contraction rewrite rules (e.g. "do not → don't")
      - Forbidden vocabulary (never-use list)
      - Speech register template (formal / casual / mixed)
      - Archetype anchor (e.g. tsundere, kuudere, genki)

    Args:
        character_name: Display name of the character.
        voice_profile: Voice fingerprint dict from metadata_en.json.
        chapter_eps_band: EPS band of the current chapter (for intensity).

    Returns:
        XML-formatted voice block, or None if profile is empty.
    """
    if not voice_profile or not character_name:
        return None

    # Extract voice profile fields
    contractions = voice_profile.get("contraction_rate", None)
    forbidden = voice_profile.get("forbidden_vocabulary", [])
    register = voice_profile.get("speech_register", voice_profile.get("register", ""))
    archetype = voice_profile.get("archetype", voice_profile.get("personality_type", ""))
    eps_band_char = voice_profile.get("eps_band", chapter_eps_band)
    speech_tics = voice_profile.get("speech_tics", voice_profile.get("verbal_tics", []))
    formality = voice_profile.get("formality_level", "")

    lines: List[str] = []
    lines.append(f"<deepseek_voice_profile character=\"{character_name}\">")

    # Archetype anchor — strongest signal for DeepSeek
    if archetype:
        lines.append(f"  <archetype>{archetype}</archetype>")

    # Speech register
    if register or formality:
        reg = register or formality
        lines.append(f"  <speech_register>{reg}</speech_register>")

    # Contraction rewrite rules — explicit templates
    if contractions is not None:
        try:
            cr = float(contractions)
            if cr <= 0.2:
                lines.append("  <contraction_policy>NEVER_CONTRACT</contraction_policy>")
                lines.append("  <contraction_rule>Rewrite all: do not, cannot, will not, I am → I am (no I'm)</contraction_rule>")
            elif cr <= 0.5:
                lines.append("  <contraction_policy>LIGHT_CONTRACT</contraction_policy>")
                lines.append("  <contraction_rule>Prefer expanded, contract only in fast/emotional dialogue</contraction_rule>")
            elif cr <= 0.8:
                lines.append("  <contraction_policy>STANDARD_CONTRACT</contraction_policy>")
                lines.append("  <contraction_rule>Contract naturally: I'm, don't, can't, won't, it's, that's</contraction_rule>")
            else:
                lines.append("  <contraction_policy>HEAVY_CONTRACT</contraction_policy>")
                lines.append("  <contraction_rule>Contract aggressively: all standard + ain't, gonna, wanna, gotta, lemme</contraction_rule>")
        except (ValueError, TypeError):
            pass

    # Forbidden vocabulary — explicit never-use list
    if forbidden and isinstance(forbidden, list):
        banned = ", ".join(str(w) for w in forbidden[:15])
        if banned:
            lines.append(f"  <forbidden_vocabulary>{banned}</forbidden_vocabulary>")

    # Speech tics
    if speech_tics and isinstance(speech_tics, list):
        tics_list = ", ".join(str(t) for t in speech_tics[:10])
        if tics_list:
            lines.append(f"  <speech_tics>{tics_list}</speech_tics>")

    # EPS band context
    if eps_band_char:
        lines.append(f"  <eps_band>{eps_band_char}</eps_band>")

    lines.append("</deepseek_voice_profile>")

    return "\n".join(lines)


def build_deepseek_voice_collective_block(
    active_characters: List[Dict[str, Any]],
    chapter_eps_band: str = "NEUTRAL",
) -> Optional[str]:
    """
    Build a collective voice block for all active characters in a chapter.

    Args:
        active_characters: List of dicts with 'name' and 'fingerprint' keys.
        chapter_eps_band: EPS band for the current chapter.

    Returns:
        XML-formatted block with all voice profiles, or None if empty.
    """
    if not active_characters:
        return None

    blocks = []
    for char in active_characters:
        name = char.get("name", "") or char.get("character", "")
        profile = char.get("fingerprint", {}) or char.get("voice_profile", {})
        if name and profile:
            block = build_deepseek_voice_block(name, profile, chapter_eps_band)
            if block:
                blocks.append(block)

    if not blocks:
        return None

    return (
        "<deepseek_voice_profiles>\n"
        + "\n".join(blocks)
        + "\n</deepseek_voice_profiles>"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. CONCURRENT CHAPTER TRANSLATION (CCT)
# ══════════════════════════════════════════════════════════════════════════════


async def translate_chapters_concurrent(
    chapters: List[Dict[str, Any]],
    processor_factory,
    max_concurrent: int = 3,
) -> List[Dict[str, Any]]:
    """
    Translate multiple chapters concurrently using asyncio semaphore.

    DeepSeek lacks Anthropic's Batch API. This closes the throughput gap
    by parallelizing independent chapter translations.

    Args:
        chapters: List of chapter dicts, each with at least 'chapter_id'
                  and any other keys needed by the processor.
        processor_factory: Callable that returns a chapter processor callable.
                           Signature: processor_factory() -> callable(chapter_dict) -> result_dict
        max_concurrent: Max concurrent chapter translations (default 3).

    Returns:
        List of result dicts in original chapter order.
    """
    if not chapters:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)

    async def _process_one(chapter: Dict[str, Any], index: int) -> Dict[str, Any]:
        async with semaphore:
            chapter_id = chapter.get("chapter_id", f"chapter_{index}")
            logger.info(
                "[DEEPSEEK-CCT] Starting chapter %s (slot %d/%d)",
                chapter_id, index + 1, len(chapters),
            )
            try:
                processor = processor_factory()
                result = await asyncio.to_thread(processor, chapter)
                logger.info("[DEEPSEEK-CCT] Completed chapter %s", chapter_id)
                return {"chapter_id": chapter_id, "index": index, "success": True, "result": result}
            except Exception as e:
                logger.error("[DEEPSEEK-CCT] Chapter %s failed: %s", chapter_id, e)
                return {"chapter_id": chapter_id, "index": index, "success": False, "error": str(e)}

    tasks = [_process_one(ch, i) for i, ch in enumerate(chapters)]
    results = await asyncio.gather(*tasks)

    # Sort back to original order
    results.sort(key=lambda r: r["index"])
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 4. PROMPT ASSEMBLY HELPER
# ══════════════════════════════════════════════════════════════════════════════


def assemble_deepseek_chapter_blocks(
    *,
    eps_band: str = "NEUTRAL",
    active_characters: Optional[List[Dict[str, Any]]] = None,
    reasoning_directive_enabled: bool = True,
    voice_block_enabled: bool = True,
) -> List[Dict[str, Any]]:
    """
    Build the chapter-varying DeepSeek guidance blocks, in order:

      1. DeepSeek reasoning directive (EPS-band CoT scaffolding)      [DRDI]
      2. DeepSeek voice collective block (structured voice profiles)  [DOVB]

    Both vary per chapter, so this function does NOT decide where they go —
    ``ChapterProcessor`` does, and that placement is the cache lever:

      * ``conversation_enabled=True`` (production default) — wrapped in a
        ``<DEEPSEEK_CHAPTER_EXECUTION_GUIDANCE>`` envelope and appended to the
        bottom of the USER turn, leaving the system prefix byte-stable across
        chapters (``chapter_processor.py:896``).
      * ``conversation_enabled=False`` — appended to the system instruction
        instead, which breaks DeepSeek's auto-prefix cache every chapter
        (``chapter_processor.py:845``).

    The volume-stable voice channel is separate and genuinely cached:
    ``PromptLoader`` substitutes whole-cast Fable anchors into
    CHARACTER_VOICE_SLOT once per volume (``prompt_loader.py:3331``).  These
    per-chapter profiles narrow that anchor set to the active speakers; they
    do not replace it.

    Args:
        eps_band: Chapter EPS band (HOT/WARM/NEUTRAL/COOL/COLD).
        active_characters: Active character list for voice block.
        reasoning_directive_enabled: Whether DRDI is active.
        voice_block_enabled: Whether DOVB is active.

    Returns:
        List of content blocks (text dicts) for the caller to place.
    """
    blocks: List[Dict[str, Any]] = []

    # Block: Reasoning directive
    if reasoning_directive_enabled:
        directive = get_reasoning_directive(eps_band, enabled=True)
        if directive:
            blocks.append({"type": "text", "text": directive})

    # Block: Voice collective
    if voice_block_enabled and active_characters:
        voice_block = build_deepseek_voice_collective_block(
            active_characters, eps_band,
        )
        if voice_block:
            blocks.append({"type": "text", "text": voice_block})

    return blocks
