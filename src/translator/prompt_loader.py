"""
Minimal DeepSeek prompt loader.

Stripped from the ~3500-line main-pipeline prompt_loader.py down to exactly
what the bare client needs: load the master prompt XML, inject context.xml
into PRIMARY_CONTEXT_XML, inject a whole-cast voice anchor block into
CHARACTER_VOICE_SLOT, and wrap the JP source + DRDI directive into the
per-turn user envelope. No RAG modules, no grammar RAG, no EPS routing
beyond the DRDI directive already produced by deepseek_optimization.py, no
tool definitions, no validation_audit blocks, no Fable 5 variants, no
literacy anchors, no PTW/FCCE/reference-deobfuscation machinery, no Active
Translation Memory.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from src.translator.config import get_master_prompt_path

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"
_LITERACY_ANCHOR_SLOT_COMMENT = "<!-- LITERACY_ANCHOR_SLOT -->"


def load_master_prompt(prompt_path: Optional[Path] = None) -> str:
    """Read the master prompt XML file, verbatim."""
    path = prompt_path or get_master_prompt_path()
    return Path(path).read_text(encoding="utf-8")


def inject_context_xml(prompt: str, context_xml: Optional[str]) -> str:
    """
    Replace the PRIMARY_CONTEXT_XML placeholder with the user-supplied
    context.xml content. When no context.xml is available, the placeholder
    is simply dropped — the master prompt has enough inline guidance to
    translate without it (PLANNING.md "Further Considerations" #1), but the
    caller should have already warned the operator loudly by this point.
    """
    replacement = context_xml.strip() if context_xml else ""
    return prompt.replace(_SEMANTIC_METADATA_PLACEHOLDER, replacement)


def inject_character_voices(prompt: str, voice_block: Optional[str]) -> str:
    """
    Replace the CHARACTER_VOICE_SLOT comment with a DOVB-style voice block
    (see deepseek_optimization.build_deepseek_voice_collective_block). This
    is the volume-stable channel — whole-cast anchors that stay in the
    cached system prefix, distinct from the per-chapter DOVB narrowing that
    goes into the user turn.
    """
    return prompt.replace(_CHARACTER_VOICE_SLOT_COMMENT, voice_block or "")


def build_system_instruction(
    *,
    context_xml: Optional[str] = None,
    voice_block: Optional[str] = None,
    prompt_path: Optional[Path] = None,
) -> str:
    """Assemble the final system prompt: master prompt + context.xml + voices."""
    prompt = load_master_prompt(prompt_path)
    prompt = inject_context_xml(prompt, context_xml)
    prompt = inject_character_voices(prompt, voice_block)
    # LITERACY_ANCHOR_SLOT has no lightweight-client producer (no literacy
    # anchor extractor was copied) — drop the comment marker so it doesn't
    # leak into the API payload as a no-op HTML comment.
    prompt = prompt.replace(_LITERACY_ANCHOR_SLOT_COMMENT, "")
    return prompt


def build_user_message(
    *,
    jp_source: str,
    chapter_guidance_blocks: Optional[list] = None,
) -> str:
    """
    Wrap the JP source in the per-turn user envelope, followed by the
    chapter-varying DRDI/DOVB guidance blocks (produced by
    deepseek_optimization.assemble_deepseek_chapter_blocks). Placing the
    guidance AFTER the source keeps the source text itself byte-stable
    across retries.
    """
    parts = [
        "<source_text>",
        jp_source.strip(),
        "</source_text>",
    ]
    if chapter_guidance_blocks:
        guidance_text = "\n\n".join(
            block.get("text", "")
            for block in chapter_guidance_blocks
            if isinstance(block, dict) and block.get("text")
        )
        if guidance_text:
            parts.append("\n<DEEPSEEK_CHAPTER_EXECUTION_GUIDANCE>")
            parts.append(guidance_text)
            parts.append("</DEEPSEEK_CHAPTER_EXECUTION_GUIDANCE>")
    return "\n".join(parts)
