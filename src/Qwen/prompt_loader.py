"""Qwen prompt loader — mirrors the DeepSeek slot-injection mechanism.

Loads the Qwen master prompt, injects context.xml into PRIMARY_CONTEXT_XML via
the SEMANTIC_METADATA_PLACEHOLDER, injects a whole-cast voice anchor into
CHARACTER_VOICE_SLOT, drops the LITERACY_ANCHOR_SLOT comment (no lightweight
producer), and wraps the JP source + per-chapter guidance into the user-turn
envelope. The slot mechanism is identical to translator/prompt_loader.py's —
the prompts are provider-owned, the injection contract is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_SEMANTIC_METADATA_PLACEHOLDER = "SEMANTIC_METADATA_PLACEHOLDER"
_CHARACTER_VOICE_SLOT_COMMENT = "<!-- CHARACTER_VOICE_SLOT -->"
_LITERACY_ANCHOR_SLOT_COMMENT = "<!-- LITERACY_ANCHOR_SLOT -->"


def load_master_prompt(prompt_path: Path) -> str:
    """Read the Qwen master prompt XML file, verbatim."""
    return Path(prompt_path).read_text(encoding="utf-8")


def inject_context_xml(prompt: str, context_xml: Optional[str]) -> str:
    """Replace the PRIMARY_CONTEXT_XML placeholder with context.xml content.

    When no context.xml is available the placeholder is dropped — the master
    prompt carries enough inline guidance to translate without it, matching
    the DeepSeek loader's degrade-gracefully contract.
    """
    replacement = context_xml.strip() if context_xml else ""
    return prompt.replace(_SEMANTIC_METADATA_PLACEHOLDER, replacement)


def inject_character_voices(prompt: str, voice_block: Optional[str]) -> str:
    """Replace CHARACTER_VOICE_SLOT with a volume-stable whole-cast voice block.

    This is the cached system channel, distinct from the per-chapter voice
    narrowing that goes into the user turn's guidance envelope.
    """
    return prompt.replace(_CHARACTER_VOICE_SLOT_COMMENT, voice_block or "")


def build_system_instruction(
    *,
    prompt_path: Path,
    context_xml: Optional[str] = None,
    voice_block: Optional[str] = None,
) -> str:
    """Assemble the final system prompt: master prompt + context.xml + voices."""
    prompt = load_master_prompt(prompt_path)
    prompt = inject_context_xml(prompt, context_xml)
    prompt = inject_character_voices(prompt, voice_block)
    # LITERACY_ANCHOR_SLOT has no lightweight-client producer — drop the marker
    # so it doesn't leak into the API payload as a no-op HTML comment.
    prompt = prompt.replace(_LITERACY_ANCHOR_SLOT_COMMENT, "")
    return prompt


def build_chapter_message(
    chapter_id: str,
    jp_source: str,
    guidance: str = "",
    previous_guidance_text: Optional[str] = None,
) -> str:
    """Wrap the JP source in the per-turn user envelope with chapter guidance.

    The envelope becomes ``<source_text chapter_id="...">`` with a target-source
    disambiguation guard — in a multi-turn conversation the history contains
    earlier ``<source_text>`` blocks, and without the attribute the model has no
    explicit key for which chapter it is translating now.

    When *previous_guidance_text* matches the current chapter's guidance
    byte-for-byte, the full block is replaced with a single CONTINUE directive —
    consecutive chapters sharing EPS band and active cast skip the repeated
    payload, improving context-cache hit ratio.
    """
    parts = []
    if chapter_id:
        parts.append(
            f'<QWEN_TARGET_SOURCE chapter_id="{chapter_id}">'
            "Translate ONLY the source_text envelope in THIS message. Earlier "
            "source envelopes in the conversation history are already-"
            "translated chapters — never re-translate them."
            "</QWEN_TARGET_SOURCE>"
        )
    source_tag = f'<source_text chapter_id="{chapter_id}">' if chapter_id else "<source_text>"
    parts.extend([source_tag, jp_source.strip(), "</source_text>"])
    guidance_text = guidance.strip()
    if guidance_text:
        if previous_guidance_text and guidance_text == previous_guidance_text:
            parts.append("\n<QWEN_CHAPTER_EXECUTION_GUIDANCE>")
            parts.append(
                "CONTINUE — same EPS band and active character set "
                "as previous chapter. Canon landscape unchanged. "
                "Proceed directly to scene analysis."
            )
            parts.append("</QWEN_CHAPTER_EXECUTION_GUIDANCE>")
        else:
            parts.append("\n<QWEN_CHAPTER_EXECUTION_GUIDANCE>")
            parts.append(guidance_text)
            parts.append("</QWEN_CHAPTER_EXECUTION_GUIDANCE>")
    return "\n".join(parts)


def build_continuation_messages(messages, assistant_blocks, chapter_id: str):
    """Build messages for a continuation call after the output cap cut the turn.

    Qwen's Anthropic route does not support partial-prefix continuation while
    thinking is enabled, so the truncated assistant turn is committed verbatim
    (including thinking blocks, which the route requires back) and a fresh user
    instruction asks for only the remainder.
    """
    return [
        *messages,
        {"role": "assistant", "content": assistant_blocks},
        {
            "role": "user",
            "content": (
                f"<QWEN_CONTINUATION chapter_id=\"{chapter_id}\">"
                "Your previous response for this chapter was cut off by the output "
                "token limit before the translation was complete. "
                "Continue the translation EXACTLY from where your previous output "
                "stopped. Do NOT re-translate any content already emitted, and do "
                "NOT repeat the chapter heading or any scene markers that already "
                "appeared. Output ONLY the continuation in English Markdown — no "
                "thinking block, no preamble, no summary."
                "</QWEN_CONTINUATION>"
            ),
        },
    ]