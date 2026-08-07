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
from xml.etree import ElementTree as ET
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
        "Maximum analytical rigor. Per sentence: examine subtext, voice fidelity, "
        "emotional resonance, literary devices. Walk reasoning step by step: "
        "JP subtext → optimal EN register → character voice alignment → technique "
        "fidelity → emotional resonance. No sentence passes without ≥2 checks. "
        "HOT scene — confession, combat, trauma, or peak emotional content demands "
        "full reasoning capacity."
    ),
    "WARM": (
        "Balanced analytical rigor. Prioritize character voice consistency and "
        "emotional escalation tracking. For key dialogue exchanges: verify speech "
        "register against voice fingerprints. For exposition: ensure smooth, "
        "progressive emotional arc. Focus reasoning where character dynamics demand "
        "it; maintain momentum through connective tissue."
    ),
    "NEUTRAL": (
        "Efficient analytical rigor. Primary task: accurate, natural prose preserving "
        "voice continuity and narrative coherence. Spot-check dialogue for fingerprint "
        "compliance. Verify transitions preserve rhythm. Do not overanalyze — "
        "correctness and readability are paramount."
    ),
    "COOL": (
        "Light analytical rigor — casual dialogue, exposition, or low-stakes "
        "interaction. Prioritize natural, conversational output. Verify contraction "
        "patterns and voice basics. Do not linger on subtext unless the JP source "
        "clearly signals it. Be accurate, consistent, efficient."
    ),
    "COLD": (
        "Minimal analytical rigor — throughput mode. Low-intensity setup or "
        "procedural narration. Accuracy and momentum first. Check names and "
        "honorifics. Do not think more than 5 sentences ahead. Trust training "
        "data for standard phrasing. Speed and precision override literary craft."
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
    volume_type: str = "",
) -> List[Dict[str, Any]]:
    """
    Build the chapter-varying DeepSeek guidance blocks, in order:

      0. Volume type tag (spinoff/mainline — controls TRAINING_KNOWLEDGE_AUTHORITY)
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
        volume_type: From context.xml volume_identity/volume_type (mainline|spinoff).

    Returns:
        List of content blocks (text dicts) for the caller to place.
    """
    blocks: List[Dict[str, Any]] = []

    # Block: Volume type tag — controls TRAINING_KNOWLEDGE_AUTHORITY posture
    if volume_type:
        blocks.append(
            {
                "type": "text",
                "text": f"<deepseek_volume_type>{volume_type}</deepseek_volume_type>",
            }
        )

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


# ══════════════════════════════════════════════════════════════════════════════
# 5. CONTEXT.XML CHAPTER-METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════
#
# The translator historically defaulted every chapter to eps_band="NEUTRAL" and
# active_characters=None, because the caller (translate_all) never supplied real
# per-chapter metadata. That had a second, subtler consequence: it made the
# CONTINUE optimization in prompt_loader.build_user_message fire unconditionally
# (every chapter's guidance was byte-identical), so "same EPS band as previous
# chapter" was asserted without ever being checked. These helpers read the
# per-chapter ground truth out of the already-loaded context.xml so the band and
# the voice narrowing reflect the actual scene — and CONTINUE only fires when
# consecutive chapters genuinely share a canon landscape.

# EPS band intensity ladder, used to derive a chapter-level band from its
# per-character signals. The scene's reasoning effort should track the most
# emotionally demanding beat in it, not the average — a chapter containing one
# HOT confession amid NEUTRAL banter still needs HOT-tier rigor at that beat.
_EPS_BAND_INTENSITY: Dict[str, int] = {
    "COLD": 0,
    "COOL": 1,
    "NEUTRAL": 2,
    "WARM": 3,
    "HOT": 4,
}
_EPS_INTENSITY_TO_BAND: Dict[int, str] = {v: k for k, v in _EPS_BAND_INTENSITY.items()}


def parse_voice_fingerprints(context_xml: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """
    Extract per-character voice data from context.xml's <voice_fingerprints>.

    Returns a mapping of character name → profile dict with keys drawn from the
    fingerprint element: ``archetype``, ``register`` (mapped to the DOVB field
    ``speech_register``), ``contraction_rate``, and ``voice_pattern`` (mapped to
    ``speech_tics``). These are exactly the fields
    :func:`build_deepseek_voice_block` consumes. Missing or unparseable input
    yields an empty dict — never raises.
    """
    profiles: Dict[str, Dict[str, Any]] = {}
    if not context_xml:
        return profiles
    try:
        root = ET.fromstring(context_xml)
    except ET.ParseError as exc:
        logger.warning("[DEEPSEEK-CTX] context.xml parse failed (voice_fingerprints): %s", exc)
        return profiles

    block = root.find("voice_fingerprints")
    if block is None:
        return profiles

    for fp in block.findall("fingerprint"):
        name = fp.get("character")
        if not name:
            continue
        profile: Dict[str, Any] = {}
        if fp.get("archetype"):
            profile["archetype"] = fp.get("archetype")
        register = fp.get("register")
        if register:
            profile["speech_register"] = register
        contraction_el = fp.find("contraction_rate")
        if contraction_el is not None and contraction_el.get("value") is not None:
            profile["contraction_rate"] = contraction_el.get("value")
        pattern_el = fp.find("voice_pattern")
        if pattern_el is not None and pattern_el.text and pattern_el.text.strip():
            # The narrative voice pattern is the closest thing this lightweight
            # client has to per-character speech guidance; surface it as the
            # DOVB speech-tics line so it reaches the model verbatim.
            profile["speech_tics"] = [pattern_el.text.strip()]
        if profile:
            profiles[name] = profile
    return profiles


def parse_eps_signals(context_xml: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Extract per-chapter active-character signals from context.xml's <eps_signals>.

    Returns a mapping of chapter id → list of ``{"name", "eps", "band"}`` dicts,
    in document order. Chapters with no <character> children (e.g. an afterword)
    map to an empty list, which the caller treats as "no voice narrowing this
    chapter." Missing or unparseable input yields an empty dict — never raises.
    """
    chapters: Dict[str, List[Dict[str, Any]]] = {}
    if not context_xml:
        return chapters
    try:
        root = ET.fromstring(context_xml)
    except ET.ParseError as exc:
        logger.warning("[DEEPSEEK-CTX] context.xml parse failed (eps_signals): %s", exc)
        return chapters

    block = root.find("eps_signals")
    if block is None:
        return chapters

    for chapter_el in block.findall("chapter"):
        chapter_id = chapter_el.get("id")
        if not chapter_id:
            continue
        characters: List[Dict[str, Any]] = []
        for char_el in chapter_el.findall("character"):
            name = char_el.get("name")
            if not name:
                continue
            characters.append(
                {
                    "name": name,
                    "eps": char_el.get("eps"),
                    "band": (char_el.get("band") or "").upper() or None,
                }
            )
        chapters[chapter_id] = characters
    return chapters


def derive_chapter_eps_band(
    characters: List[Dict[str, Any]],
    default: str = "NEUTRAL",
) -> str:
    """
    Reduce a chapter's per-character EPS signals to a single chapter-level band.

    Uses the maximum (most intense) band present: a chapter's reasoning budget
    must cover its single most demanding beat, and a lower aggregate would
    under-allocate effort exactly where the scene peaks. Unknown/absent bands
    are ignored; if nothing usable remains, *default* is returned.
    """
    best: Optional[int] = None
    for char in characters:
        band = char.get("band")
        intensity = _EPS_BAND_INTENSITY.get(str(band).upper()) if band else None
        if intensity is None:
            continue
        if best is None or intensity > best:
            best = intensity
    if best is None:
        return default
    return _EPS_INTENSITY_TO_BAND[best]


def parse_volume_type(context_xml: Optional[str]) -> str:
    """Extract volume_type from context.xml's <volume_identity> block.

    Returns "mainline" or "spinoff" — empty string if missing/unparseable.
    """
    if not context_xml:
        return ""
    try:
        root = ET.fromstring(context_xml)
    except ET.ParseError:
        return ""
    vi = root.find("volume_identity")
    if vi is None:
        return ""
    vt = vi.find("volume_type")
    if vt is not None and vt.text:
        return vt.text.strip()
    return ""
