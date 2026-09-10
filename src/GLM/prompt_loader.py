"""GLM prompt assembly with a byte-stable system/user split."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"
_LITERACY_ANCHOR_SLOT_COMMENT = "<!-- LITERACY_ANCHOR_SLOT -->"


def load_master_prompt(prompt_path: Path) -> str:
    return Path(prompt_path).read_text(encoding="utf-8")


def inject_context_xml(prompt: str, context_xml: Optional[str]) -> str:
    replacement = context_xml.strip() if context_xml else ""
    slot = prompt.rfind(_SEMANTIC_METADATA_PLACEHOLDER)
    if slot < 0:
        return prompt
    return prompt[:slot] + replacement + prompt[slot + len(_SEMANTIC_METADATA_PLACEHOLDER):]


def inject_character_voices(prompt: str, voice_block: Optional[str]) -> str:
    return prompt.replace(_CHARACTER_VOICE_SLOT_COMMENT, voice_block or "")


def build_system_instruction(*, prompt_path: Path, context_xml: Optional[str] = None, voice_block: Optional[str] = None) -> str:
    prompt = inject_context_xml(load_master_prompt(prompt_path), context_xml)
    prompt = inject_character_voices(prompt, voice_block)
    return prompt.replace(_LITERACY_ANCHOR_SLOT_COMMENT, "")


def build_chapter_message(chapter_id: str, jp_source: str, guidance: str = "", strategy_directive: str = "", previous_guidance_text: Optional[str] = None, previous_strategy_directive: Optional[str] = None) -> str:
    parts = [
        f'<GLM_TARGET_SOURCE chapter_id="{chapter_id}">',
        "Translate ONLY the source_text envelope in THIS message. Earlier source envelopes in the conversation history are already-translated chapters — never re-translate them.",
        "</GLM_TARGET_SOURCE>",
        f'<source_text chapter_id="{chapter_id}">', jp_source.strip(), "</source_text>",
    ]
    directive = strategy_directive.strip()
    if directive:
        parts.extend(["<GLM_STRATEGY_DIRECTIVE>", "CONTINUE — same EPS strategy as the preceding chapter." if previous_strategy_directive and directive == previous_strategy_directive.strip() else directive, "</GLM_STRATEGY_DIRECTIVE>"])
    if guidance:
        guidance_text = guidance.strip()
        if previous_guidance_text and guidance_text == previous_guidance_text.strip():
            guidance_text = "CONTINUE — same EPS band and active character set as the preceding chapter."
        parts.extend(["<GLM_CHAPTER_EXECUTION_GUIDANCE>", guidance_text, "</GLM_CHAPTER_EXECUTION_GUIDANCE>"])
    return "\n".join(parts)


def build_continuation_message(chapter_id: str) -> str:
    return "\n".join([
        f'<GLM_CONTINUATION chapter_id="{chapter_id}">',
        "The previous response reached the output limit. Continue exactly from its final emitted point; do not repeat any prose, heading, or scene marker.",
        "Return only the remaining English Markdown translation.",
        "</GLM_CONTINUATION>",
    ])
