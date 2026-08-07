"""
Thinking-log text utilities — split leaked <thinking> XML out of chapter
output, and merge API reasoning_content into a THINKING/<chapter_id>_THINKING.md
log entry.

This is a deliberately narrow slice of the main pipeline's thinking_output.py
(~360 lines): that module also has retroactive salvage-a-whole-volume CLI
tooling and a provider-config lookup (`is_thinking_salvage_enabled`) that
imports `pipeline.translator.config`/`pipeline.config` — main-pipeline-only
modules that don't exist in this lightweight client. Copying it verbatim
would have shipped broken imports for functionality nothing here calls.
Only the pure text-transform functions the translator's per-chapter
thinking log actually needs are here.

`split_analysis_prelude_from_output` was previously duplicated as a private
inline copy inside deepseek_client.py (needed there for the empty-content
reasoning-channel salvage path). It now lives here as the one canonical
copy; deepseek_client.py imports it instead of redefining it.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_THINKING_BLOCK_RE = re.compile(
    r"<thinking\b[^>]*>([\s\S]*?)</thinking>",
    re.IGNORECASE,
)

_SALVAGED_MARKER = "## Salvaged XML thinking"
_SALVAGED_PRELUDE_MARKER = "## Salvaged untagged analysis prelude"

# A top-level Markdown H1 (the chapter heading) — the credibility signal that
# text before it is a leaked analysis prelude and not legitimate narrative.
_TOP_LEVEL_H1_RE = re.compile(r"(?m)^#\s+.+$")

_ANALYSIS_PRELUDE_RE = re.compile(
    r"(?:"
    r"\bI['’]ll\s+work\s+through\b|"
    r"\bMETADATA\s+CONFLICT\b|"
    r"\bBIBLE\s+AUDIT\b|"
    r"\bSCENE\s+ANALYSIS\b|"
    r"\bTRANSLATION\s+DECISIONS\b|"
    r"\bKOJI\s+FOX\b|"
    r"\bSELF[-\s]?CRITIQUE\b|"
    r"\bPhase\s+0\b|"
    r"\*\*[^*\n]+(?:SCAN|AUDIT|ANALYSIS|DECISIONS|CHECK)\*\*"
    r")",
    re.IGNORECASE,
)


def split_thinking_from_output(text: str) -> Tuple[str, List[str]]:
    """
    Return (chapter_body_without_thinking_blocks, list_of_thinking_inner_texts).

    Catches the case DeepSeekClient's reasoning-channel salvage doesn't: the
    content channel is non-empty (a real chapter came back), but a
    `<thinking>...</thinking>` block still leaked in alongside it — a
    different failure mode from the empty-content case that salvage handles.
    """
    raw = text or ""
    blocks = [m.group(1).strip() for m in _THINKING_BLOCK_RE.finditer(raw)]
    blocks = [b for b in blocks if b]
    cleaned = _THINKING_BLOCK_RE.sub("", raw)
    return cleaned.strip(), blocks


def split_analysis_prelude_from_output(text: str) -> Tuple[str, List[str]]:
    """Return (chapter_body_without_analysis_prelude, prelude_blocks).

    Some models ignore the required <thinking> wrapper and emit a short
    translator-analysis prelude before the first top-level chapter heading.
    Only strip it when a real H1 follows and the prelude matches analysis
    markers, so narrative text that legitimately starts before a heading is
    left untouched.
    """
    raw = text or ""
    h1_match = _TOP_LEVEL_H1_RE.search(raw)
    if not h1_match:
        return raw.strip(), []

    prelude = raw[: h1_match.start()].strip()
    if not prelude:
        return raw.strip(), []

    if not _ANALYSIS_PRELUDE_RE.search(prelude):
        return raw.strip(), []

    return raw[h1_match.start():].strip(), [prelude]


def merge_thinking_log(
    api_thinking: Optional[str],
    xml_blocks: List[str],
    *,
    chapter_id: str = "",
    untagged_blocks: Optional[List[str]] = None,
) -> str:
    """Combine API reasoning_content with salvaged XML thinking blocks."""
    parts: List[str] = []
    api = (api_thinking or "").strip()
    if api:
        parts.append("## API reasoning (reasoning_content)\n\n" + api)
    if xml_blocks:
        header = _SALVAGED_MARKER + " (leaked into content"
        if chapter_id:
            header += f" — {chapter_id}"
        header += ")\n\n"
        parts.append(header + "\n\n---\n\n".join(xml_blocks))
    if untagged_blocks:
        header = _SALVAGED_PRELUDE_MARKER + " (leaked before chapter heading"
        if chapter_id:
            header += f" — {chapter_id}"
        header += ")\n\n"
        parts.append(header + "\n\n---\n\n".join(untagged_blocks))
    return "\n\n".join(parts).strip()
