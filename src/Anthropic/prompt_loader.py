"""Hybrid Markdown/XML prompt assembly for the Anthropic translation route."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"
_TRANSLATION_POLICY_SLOT_COMMENT = "<!-- TRANSLATION_POLICY_SLOT -->"
_PROJECT_CONTEXT_HEADING = "## Project Context"


def load_master_prompt(prompt_path: Path) -> str:
    return Path(prompt_path).read_text(encoding="utf-8")


def inject_context_xml(prompt: str, context_xml: Optional[str]) -> str:
    return prompt.replace(_SEMANTIC_METADATA_PLACEHOLDER, context_xml.strip() if context_xml else "")


def inject_character_voices(prompt: str, voice_block: Optional[str]) -> str:
    return prompt.replace(_CHARACTER_VOICE_SLOT_COMMENT, voice_block or "")


_TRANSLATION_POLICY_RE = re.compile(
    r"^[ \t]*<translation_policy\b.*?</translation_policy>[ \t]*\r?\n?",
    re.DOTALL | re.MULTILINE,
)


def split_translation_policy(context_xml: Optional[str]) -> Tuple[Optional[str], str]:
    """Lift this volume's <translation_policy> out of the context document.

    Returns ``(remaining_context_xml, policy_block)``. The policy is a
    constraint, not metadata, so it is injected at its own slot ahead of the
    project-context data rather than left buried among fifteen sibling blocks
    a model reads as reference material. It is *removed* from the data dump on
    the way past: leaving a copy behind spends the tokens twice and invites the
    model to treat the second occurrence as a separate, possibly conflicting
    rule set. Both halves stay inside the volume cache segment, so the static
    craft policy above ``## Project Context`` is untouched by a volume change.
    """
    if not context_xml:
        return context_xml, ""
    match = _TRANSLATION_POLICY_RE.search(context_xml)
    if match is None:
        return context_xml, ""
    remaining = context_xml[: match.start()] + context_xml[match.end() :]
    return remaining, match.group(0).strip()


def inject_translation_policy(prompt: str, policy_block: Optional[str]) -> str:
    """Fill the policy slot, or erase it cleanly when the volume declares none."""
    block = (policy_block or "").strip()
    if not block:
        return prompt.replace(_TRANSLATION_POLICY_SLOT_COMMENT + "\n", "").replace(
            _TRANSLATION_POLICY_SLOT_COMMENT, ""
        )
    return prompt.replace(_TRANSLATION_POLICY_SLOT_COMMENT, block + "\n")


def build_system_instruction(*, prompt_path: Path, context_xml: Optional[str] = None, voice_block: Optional[str] = None) -> str:
    prompt = load_master_prompt(prompt_path)
    context_xml, policy_block = split_translation_policy(context_xml)
    prompt = inject_translation_policy(prompt, policy_block)
    return inject_character_voices(inject_context_xml(prompt, context_xml), voice_block)


def build_system_segments(
    *, prompt_path: Path, context_xml: Optional[str] = None, voice_block: Optional[str] = None
) -> List[str]:
    """Split the assembled system prompt into its two cache layers.

    Segment 1 is the static craft policy — byte-identical for every volume and
    every chapter, so one cache entry stays warm across the whole library.
    Segment 2 is this volume's project context, stable for the length of the
    volume. Sent as one concatenated block (the previous behaviour) the two
    share a single cache key, so starting a new volume discards the craft
    policy along with the old context; split, a volume change rewrites only
    the second entry. The master prompt keeps every static section ahead of
    its ``## Project Context`` heading precisely so this is a clean tail cut.

    Falls back to a single segment when the heading is absent, so a prompt
    file without it still renders exactly as before.
    """
    prompt = build_system_instruction(
        prompt_path=prompt_path, context_xml=context_xml, voice_block=voice_block
    )
    index = prompt.find(_PROJECT_CONTEXT_HEADING)
    if index <= 0:
        return [prompt]
    static_policy = prompt[:index].rstrip()
    project_context = prompt[index:].strip()
    if not static_policy or not project_context:
        return [prompt]
    return [static_policy, project_context]


def _format_chapter_ids(chapter_ids: Any) -> str:
    """Render a chapter-id list for the envelope, or the word ``none``.

    ``none`` is deliberate and load-bearing: an empty element would read as an
    unfilled slot, and the whole point of this block is that the model can
    distinguish "nothing is here" from "I was not told".
    """
    ids = [str(chapter_id).strip() for chapter_id in (chapter_ids or []) if str(chapter_id).strip()]
    return ", ".join(ids) if ids else "none"


def build_continuity_block(chapter_id: str, continuity: Dict[str, Any]) -> List[str]:
    """Declare, by chapter id, what continuity THIS request carries.

    The envelope used to assert unconditionally that "earlier source_text
    envelopes are already translated continuity context". On the batch path
    that assertion outruns the payload: every request in a wave is built
    against one frozen ledger, so chapter N is told its predecessors are
    present while the messages array stops at the previous wave. Fable 5.1
    noticed and said so on Vol.4 - "depends on wording I don't have access
    to" (THINKING/CHAPTER_03_THINKING.md) - and then guessed at a callback it
    could not see.

    A guess is the failure mode worth removing. Naming the gap converts it
    into a stated constraint the model can actually honour: fall back to the
    locked glossary rather than coining a second rendering for a term that
    already has one.
    """
    verbatim = continuity.get("verbatim_chapter_ids") or []
    summarized = continuity.get("summarized_chapter_ids") or []
    previous_id = str(continuity.get("previous_chapter_id") or "").strip()
    previous_status = str(continuity.get("previous_chapter_status") or "").strip() or "unknown"
    previous_tail = str(continuity.get("previous_chapter_tail") or "").strip()

    lines = [
        f'  <continuity_state chapter_id="{chapter_id}">',
        "    This block describes the continuity present in THIS request, and nothing beyond it.",
        f"    <translated_in_full>{_format_chapter_ids(verbatim)}</translated_in_full>",
        f"    <translated_summarized_only>{_format_chapter_ids(summarized)}</translated_summarized_only>",
    ]
    if previous_id:
        lines.append(
            f'    <immediately_preceding chapter_id="{previous_id}" status="{previous_status}" />'
        )
    if previous_tail:
        lines.extend(
            [
                f'    <preceding_chapter_ending chapter_id="{previous_id}">',
                "      The closing passage of the preceding chapter, for callbacks and open scenes.",
                previous_tail,
                "    </preceding_chapter_ending>",
            ]
        )
    if previous_id and previous_status == "absent":
        lines.extend(
            [
                "    <instruction>",
                f"      {previous_id} is NOT in this request. THE SOURCE TEXT IS AUTHORITATIVE:",
                "      translate what this chapter's source says, and let no reconstruction of an",
                "      unseen chapter override it. Do not invent a rendering for any locked term,",
                "      name, honorific, or signature phrase in order to bridge the gap — resolve",
                "      those from the anchor_locks below and from the glossary, name_map, and",
                "      locks in the project context, which are authoritative. Where a callback",
                "      depends on exact wording you cannot see, phrase it so it stays true",
                "      whichever wording was used, and never assert a detail the source does not",
                "      carry in order to make the callback land.",
                "    </instruction>",
            ]
        )
    lines.append("  </continuity_state>")
    return lines


def build_anchor_block(chapter_id: str, anchor_rows: List[Dict[str, Any]]) -> List[str]:
    """The locked phrases this chapter's source actually contains.

    These already live in context.xml's <verbatim_anchors>, inside the cached
    system prefix — so why repeat them? Two reasons, and only the second is
    about attention.

    First, context.xml states what a surface SHOULD be; it cannot state what
    earlier chapters DID. That is harvested from EN/ and appears here as
    established_in, with the quotation that proves it. A chapter whose
    predecessor is in flight beside it in the same batch wave has no other
    route to that fact.

    Second, salience. Fifteen anchors sit among eighteen reference blocks in a
    long system prompt. Naming the two or three that this chapter will collide
    with, beside the source that collides with them, is a different act from
    having them somewhere in the prefix.

    Measured on Vol.4: 重畳 is anchored and held its surface across 45
    occurrences in eight chapters. こほろん is not anchored and shipped as
    "kohoron", "Kohon", and "Ahem" — across CH18 and CH19, which were in the
    same wave.
    """
    if not anchor_rows:
        return []
    lines = [
        f'  <anchor_locks chapter_id="{chapter_id}">',
        "    Locked renderings for phrases in this chapter's source. These are decided for the",
        "    whole series: use the locked English surface, do not coin a variant, and do not",
        "    substitute a synonym even for variety.",
    ]
    for row in anchor_rows:
        attrs = [
            f'id="{row.get("anchor_id", "")}"',
            f'occurrences_in_this_source="{row.get("source_occurrences", 0)}"',
        ]
        lines.append(f"    <lock {' '.join(attrs)}>")
        lines.append(f'      <jp>{row.get("jp", "")}</jp>')
        lines.append(f'      <en>{row.get("en", "")}</en>')
        forbidden = row.get("forbidden_synonyms") or []
        if forbidden:
            lines.append(f'      <never_render_as>{", ".join(forbidden)}</never_render_as>')
        established = row.get("established_in_chapter")
        if established:
            lines.append(
                f'      <as_used_in chapter_id="{established}">{row.get("citation", "")}</as_used_in>'
            )
        else:
            lines.append(
                "      <as_used_in status=\"no_prior_use\">No earlier chapter in this volume has "
                "used this surface yet. You are establishing it; use the locked form exactly.</as_used_in>"
            )
        lines.append("    </lock>")
    lines.append("  </anchor_locks>")
    return lines


def build_chapter_message(
    chapter_id: str,
    jp_source: str,
    guidance: str = "",
    previous_guidance_text: Optional[str] = None,
    continuity: Optional[Dict[str, Any]] = None,
    anchor_rows: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the current-source envelope; earlier source turns are continuity only.

    ``continuity`` (from AnthropicConversationManager.continuity_state, plus
    the agent's preceding-chapter resolution) turns the blanket claim about
    "earlier source_text envelopes" into a declaration naming what is here.
    Omitted, the envelope renders exactly as it always did.
    """
    if continuity:
        target_note = (
            "    Translate only the source_text in this message. Any earlier source_text "
            "envelope in this conversation is already-translated continuity context; never "
            "translate one again. The continuity_state block below lists exactly which "
            "chapters are present - do not assume any chapter it omits."
        )
    else:
        target_note = (
            "    Translate only the source_text in this message. Earlier source_text envelopes "
            "are already translated continuity context; never translate them again."
        )
    parts = [
        f'<translation_task chapter_id="{chapter_id}">',
        f'  <target_source chapter_id="{chapter_id}">',
        target_note,
        "  </target_source>",
    ]
    if continuity:
        parts.extend(build_continuity_block(chapter_id, continuity))
    if anchor_rows:
        parts.extend(build_anchor_block(chapter_id, anchor_rows))
    parts.extend(
        [
            f'  <source_text chapter_id="{chapter_id}">',
            jp_source.strip(),
            "  </source_text>",
        ]
    )
    guidance_text = guidance.strip()
    if guidance_text:
        if previous_guidance_text and guidance_text == previous_guidance_text:
            parts.extend(
                [
                    "  <chapter_guidance>",
                    "    Continue with the same emotional band and active voice set as the preceding chapter.",
                    "  </chapter_guidance>",
                ]
            )
        else:
            parts.extend(["  <chapter_guidance>", guidance_text, "  </chapter_guidance>"])
    parts.extend(
        [
            "  <chapter_output>",
            "    Return only the finished English literary translation in Markdown. Do not add notes, process commentary, or a preamble.",
            "  </chapter_output>",
            "</translation_task>",
        ]
    )
    return "\n".join(parts)


def build_continuation_message(chapter_id: str) -> str:
    """Ask for only the unseen tail after an incomplete Messages output."""
    return "\n".join(
        [
            f'<translation_continuation chapter_id="{chapter_id}">',
            "The preceding translation for this source was incomplete.",
            "Continue exactly at its final emitted point. Do not repeat earlier prose, the chapter heading, or scene markers already emitted.",
            "Return only the remaining English Markdown translation.",
            "</translation_continuation>",
        ]
    )
