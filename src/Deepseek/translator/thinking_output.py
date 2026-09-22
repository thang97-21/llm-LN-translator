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

# The same H1, welded onto the end of the preceding sentence. Measured on
# a6cbaa CHAPTER_07_EN.md, where the leaked prelude ended
# "...without added translator commentary.# Afterword" with no newline, so the
# line-anchored pattern above matched nothing and the entire prelude shipped.
# A '#' directly following a non-space character is itself an artifact
# signature -- real prose does not weld a heading onto a full stop -- and this
# is consulted ONLY when no line-start H1 exists, so behaviour on well-formed
# output is unchanged. The prelude must still match an analysis or advisor
# marker before anything is cut.
_GLUED_H1_RE = re.compile(r"(?m)(?:^|(?<=\S))#[ \t]+\S[^\n]*$")

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


# Tool-call XML that reached the content channel. A chapter body never
# legitimately contains an invoke/function_calls envelope, so these are removed
# wherever they appear rather than only ahead of the heading. Both the paired
# form and a bare unpaired tag are matched: the leak measured on volume a6cbaa
# (CHAPTER_07_EN.md) carried an EMPTY <invoke name="advisor"></invoke> pair
# with nothing between the tags, which a body-requiring pattern would miss.
_TOOL_CALL_TAGS = r"(?:antml:)?(?:invoke|function_calls|function_results|tool_use|parameter)"
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<\s*" + _TOOL_CALL_TAGS + r"\b[^>]*>[\s\S]*?<\s*/\s*" + _TOOL_CALL_TAGS + r"\s*>"
    r"|<\s*/?\s*" + _TOOL_CALL_TAGS + r"\b[^>]*>",
    re.IGNORECASE,
)

# Advisor-era scaffolding (Proofreading Mode). Every phrase here was observed in
# SHIPPED output, not imagined: a6cbaa/EN/CHAPTER_07_EN.md opened with ~1.1KB of
# planning prose, an empty advisor invoke envelope, and the consult-budget
# system message -- all of it above the "# Afterword" heading, all of it read by
# whoever opened the file. These extend _ANALYSIS_PRELUDE_RE rather than
# replacing it; that pattern still covers the older untagged-analysis shapes.
_ADVISOR_PRELUDE_RE = re.compile(
    r"(?:"
    r"\bConsult\s+budget\s+exhausted\b|"
    r"\bSkip\s+the\s+consult\b|"
    r"\bReason\s+silently\b|"
    r"\bThe\s+task\s+is\s+to\s+translate\b|"
    r"\bcontinuity_state\b|"
    r"\bPer\s+`?<\w+_policy>`?|"
    r"<\s*/?\s*(?:antml:)?(?:invoke|function_calls|function_results|tool_use)\b"
    r")",
    re.IGNORECASE,
)


def strip_tool_call_blocks(text: str) -> Tuple[str, List[str]]:
    """Return (body_without_tool_call_xml, removed_blocks).

    Unconditional, unlike the prelude split: a translated chapter never contains
    a tool-call envelope, so there is no heading precondition to satisfy and no
    position in the body where one would be legitimate.

    What is removed is handed back rather than dropped, so it lands in the
    thinking log. A filter that silently swallows a leak is how the a6cbaa one
    survived a full run -- the operator needs to see that it fired.
    """
    raw = text or ""
    blocks = [m.group(0).strip() for m in _TOOL_CALL_BLOCK_RE.finditer(raw)]
    blocks = [block for block in blocks if block]
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", raw)
    return cleaned.strip(), blocks


def sanitize_chapter_output(text: str) -> Tuple[str, List[str]]:
    """Deterministic receive-side filter for one chapter's content channel.

    Three guards in dependency order: tagged ``<thinking>`` first, then tool-call
    XML, then the untagged analysis/advisor prelude. The prelude test runs LAST
    on purpose -- removing an invoke envelope changes what sits above the first
    H1, and the heading precondition has to be judged against text the earlier
    passes have already cleaned, not against the raw response.

    Every removed fragment comes back in the second return value for the
    thinking log. No rule here consults model judgment: each is a literal
    pattern measured from output that actually shipped.
    """
    body, thinking_blocks = split_thinking_from_output(text)
    body, tool_blocks = strip_tool_call_blocks(body)
    body, prelude_blocks = split_analysis_prelude_from_output(body)
    return body, [*thinking_blocks, *tool_blocks, *prelude_blocks]

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
    h1_match = _TOP_LEVEL_H1_RE.search(raw) or _GLUED_H1_RE.search(raw)
    if not h1_match:
        return raw.strip(), []

    prelude = raw[: h1_match.start()].strip()
    if not prelude:
        return raw.strip(), []

    if not (_ANALYSIS_PRELUDE_RE.search(prelude) or _ADVISOR_PRELUDE_RE.search(prelude)):
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
