"""Hybrid Markdown/XML prompt assembly for the OpenAI translation route."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"

# Keep the literary contract in the shared master prompt. Astra's documented
# tendency to ask for clarification and produce more formatting is handled by
# this deliberately small, model-scoped overlay instead of forking the entire
# prompt and silently letting the two routes drift apart.
_ASTRA_OVERLAY = """<astra_execution_policy>
Treat the supplied source, canon locks, and project context as sufficient
working authority for routine translation decisions. Proceed autonomously:
do not ask clarification questions during a translation turn. Resolve ordinary
ambiguity with the authority order above and preserve ambiguity only when the
source leaves it unresolved. Return the requested translation directly, with
no expanded explanation, process commentary, or extra formatting.

This extends to dialogue evasion and prose economy specifically, since both
run against your documented instincts above: an evasive or deflecting line is
a translation decision already made, not an ambiguity to clarify or resolve —
render the gap, do not fill it. A concise sentence is not an incomplete one;
do not pad a line toward the shape of a fuller explanation.
</astra_execution_policy>"""


def prompt_profile_version(model: Optional[str] = None) -> str:
    return "astra-v1" if str(model or "").strip().lower() == "gpt-6-astra" else "openai-legacy-v1"


def load_master_prompt(prompt_path: Path) -> str:
    return Path(prompt_path).read_text(encoding="utf-8")


def inject_context_xml(prompt: str, context_xml: Optional[str]) -> str:
    return prompt.replace(_SEMANTIC_METADATA_PLACEHOLDER, context_xml.strip() if context_xml else "")


def inject_character_voices(prompt: str, voice_block: Optional[str]) -> str:
    return prompt.replace(_CHARACTER_VOICE_SLOT_COMMENT, voice_block or "")


def build_system_instruction(
    *,
    prompt_path: Path,
    context_xml: Optional[str] = None,
    voice_block: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    prompt = load_master_prompt(prompt_path)
    prompt = inject_character_voices(inject_context_xml(prompt, context_xml), voice_block)
    if prompt_profile_version(model) == "astra-v1":
        prompt = f"{prompt.rstrip()}\n\n{_ASTRA_OVERLAY}\n"
    return prompt


def build_chapter_message(
    chapter_id: str,
    jp_source: str,
    guidance: str = "",
    previous_guidance_text: Optional[str] = None,
    continuity: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the current-source envelope; earlier source turns are continuity only."""
    parts = [
        f'<translation_task chapter_id="{chapter_id}">',
        f'  <target_source chapter_id="{chapter_id}">',
        "    Translate only the source_text in this message. Earlier source_text envelopes are already translated continuity context; never translate them again.",
        "  </target_source>",
        f'  <source_text chapter_id="{chapter_id}">',
        jp_source.strip(),
        "  </source_text>",
    ]
    if continuity:
        verbatim = ", ".join(str(value) for value in continuity.get("verbatim_chapter_ids") or []) or "none"
        summarized = ", ".join(str(value) for value in continuity.get("summarized_chapter_ids") or []) or "none"
        previous_id = str(continuity.get("previous_chapter_id") or "none")
        previous_status = str(continuity.get("previous_chapter_status") or "unknown")
        parts.extend(
            [
                "  <conversation_continuity>",
                f"    <verbatim_chapters>{escape(verbatim)}</verbatim_chapters>",
                f"    <summarized_chapters>{escape(summarized)}</summarized_chapters>",
                f"    <previous_chapter id=\"{escape(previous_id)}\" status=\"{escape(previous_status)}\">",
            ]
        )
        tail = str(continuity.get("previous_chapter_tail") or "").strip()
        if tail:
            parts.append(f"      <ending_tail>{escape(tail)}</ending_tail>")
        parts.extend(["    </previous_chapter>", "  </conversation_continuity>"])
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
    """Ask for only the unseen tail after an incomplete Responses output."""
    return "\n".join(
        [
            f'<translation_continuation chapter_id="{chapter_id}">',
            "The preceding translation for this source was incomplete.",
            "Continue exactly at its final emitted point. Do not repeat earlier prose, the chapter heading, or scene markers already emitted.",
            "Return only the remaining English Markdown translation.",
            "</translation_continuation>",
        ]
    )
