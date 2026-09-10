"""
Markdown to XHTML Converter - Industry Standard OEBPS Format.

Converts translated markdown content to XHTML paragraphs with proper
XML escaping, illustration handling, and formatting.

Path convention: Uses OEBPS standard paths (../Images/)
"""

import re
from typing import Dict, List, Optional, Tuple
from html import escape

try:
    from smartypants import smartypants, Attr
    SMARTYPANTS_AVAILABLE = True
except ImportError:
    SMARTYPANTS_AVAILABLE = False
    Attr = None

from src.Deepseek.common.config import SCENE_BREAK_MARKER, ILLUSTRATION_PLACEHOLDER_PATTERN, MARKDOWN_IMAGE_PATTERN
from .config import COLLAPSE_BLANK_LINES, BLANK_LINE_FREQUENCY, get_epub_version

# The EPUB semantic carried by a verse container. Presentation is deliberately
# NOT part of this: the CSS hook stays class="poem" in every volume, because a
# song and a poem are set on the page identically. Only the machine-readable
# type differs, and a volume of lyrics should not announce itself to a reading
# system as a book of poems. The builder resolves this from the volume's
# <translation_policy> before conversion; anything unrecognised is refused
# rather than written into the markup.
VERSE_EPUB_TYPES = ("z3998:poem", "z3998:song", "z3998:verse")
_DEFAULT_VERSE_EPUB_TYPE = "z3998:poem"
_verse_epub_type = _DEFAULT_VERSE_EPUB_TYPE


def set_verse_epub_type(value: Optional[str]) -> str:
    """Set the verse container semantic. Returns the value actually in force."""
    global _verse_epub_type
    candidate = (value or "").strip()
    _verse_epub_type = (
        candidate if candidate in VERSE_EPUB_TYPES else _DEFAULT_VERSE_EPUB_TYPE
    )
    return _verse_epub_type


def get_verse_epub_type() -> str:
    return _verse_epub_type


# Industry-standard image path (OEBPS format)
IMAGES_PATH = "../Images"

# Footnote patterns
CUSTOM_FOOTNOTE_MARKER_RE = re.compile(r"\(\*\)")
MARKDOWN_FOOTNOTE_MARKER_RE = re.compile(r"\[\^([^\]]+)\]")
MARKDOWN_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.+)$")
CUSTOM_FOOTNOTE_DEF_COLON_RE = re.compile(r"^\*\*\*(.+?):\*\*\s*(.+?)\*$")
CUSTOM_FOOTNOTE_DEF_SEP_RE = re.compile(r"^\*\*\*(.+?)\*\*\s*(?:[:：]|[-–—])\s*(.+?)\*$")
NOTEREF_PLACEHOLDER_RE = re.compile(r"__NOTEREF_(\d+)__")
STANDALONE_BR_RE = re.compile(r"^<br\s*/?>$", re.IGNORECASE)

# Structured-block markers. A paragraph that is nothing but an HTML comment
# is a production note and must never reach the reader as text; a run of
# "- " or "1. " lines is a real list.
HTML_COMMENT_ONLY_RE = re.compile(r"^<!--(.*?)-->$", re.DOTALL)
BULLET_ITEM_RE = re.compile(r"^[-\u2022]\s+")
NUMBERED_ITEM_RE = re.compile(r"^\d+[.)]\s+")

# A body-level ATX sub-heading (## through ######) \u2014 e.g. a scene/episode
# title embedded partway through a chapter. The chapter's own H1 is handled
# separately upstream and never reaches this converter; anything matching
# here is real structural content and must become a semantic heading, not
# silently vanish or get flattened into an ordinary <p>.
ATX_HEADING_RE = re.compile(r"^(#{2,6})\s+(.+)$")


class MarkdownToXHTML:
    """Converts markdown content to XHTML paragraph format."""

    @staticmethod
    def convert_paragraphs(paragraphs: List[str]) -> List[str]:
        """
        Convert a list of paragraphs to XHTML <p> tags.

        Args:
            paragraphs: List of paragraph strings (including "<blank>" markers)

        Returns:
            List of XHTML paragraph strings
        """
        xhtml_paragraphs = []

        for para in paragraphs:
            xhtml_para = MarkdownToXHTML._convert_single_paragraph(para)
            if xhtml_para:
                xhtml_paragraphs.append(xhtml_para)

        return xhtml_paragraphs

    @staticmethod
    def _convert_single_paragraph(para: str, skip_illustrations: bool = False) -> str:
        """
        Convert a single paragraph to XHTML.

        Args:
            para: Single paragraph string
            skip_illustrations: If True, skip illustration placeholders

        Returns:
            XHTML paragraph tag or empty string
        """
        if para == "<blank>":
            return '<p><br/></p>'

        # Check for standalone illustration placeholder (block-level)
        stripped = para.strip()
        if STANDALONE_BR_RE.match(stripped):
            return '<p class="lyric-break"><br/></p>'

        if MarkdownToXHTML._is_illustration_placeholder(stripped):
            if skip_illustrations:
                return ""
            else:
                return MarkdownToXHTML._convert_illustration_placeholder(stripped)

        # Check for scene break marker
        if stripped == SCENE_BREAK_MARKER:
            return '<p class="section-break">◆</p>'

        # Markdown blockquote support:
        # - Lyric blocks in memoir chapters use: > *line*
        # - Other blockquotes are rendered as quoted prose lines.
        # ">>" opens a message-board post reference (">>8"), not a nested
        # quotation. Routing it to the blockquote path would set a reply as a
        # quote; nothing in this pipeline nests blockquotes.
        if stripped.startswith(">") and not stripped.startswith(">>"):
            return MarkdownToXHTML._convert_blockquote_block(para)

        # A paragraph that is only an HTML comment is an editorial or
        # production note. Escaping it prints "<!-- illustration: ... -->" to
        # the reader, so keep it as a real comment: preserved in the file,
        # invisible in the book.
        comment = HTML_COMMENT_ONLY_RE.match(stripped)
        if comment:
            return "<!--%s-->" % comment.group(1).replace("--", "-")

        # A body-level ATX sub-heading (## and deeper) — always single-line,
        # so this must be checked before the multi-line structured-block path.
        heading = ATX_HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            heading_text = MarkdownToXHTML._render_inline_markdown(heading.group(2).strip())
            return f'<h{level}>{heading_text}</h{level}>'

        # Multi-line paragraphs carry deliberate structure (see below).
        block_lines = [ln for ln in para.split("\n") if ln.strip()]
        if len(block_lines) > 1 and "<img" not in para:
            return MarkdownToXHTML._convert_structured_block(block_lines)

        # Check if paragraph contains inline image tags (normalize and preserve them)
        if '<img' in para and 'src=' in para:
            para = MarkdownToXHTML._normalize_inline_images(para)
            # Wrap in paragraph if not already wrapped
            if para.startswith('<img'):
                return f'<p class="illustration">{para}</p>'
            return para

        escaped_content = MarkdownToXHTML._render_inline_markdown(para)

        return f'<p>{escaped_content}</p>'

    @staticmethod
    def _convert_blockquote_line(stripped: str, epub3: bool = False) -> str:
        """
        Convert markdown blockquote lines.
        - `> *...*` => lyric line
        - `>`      => stanza/quote break
        - other    => quoted prose line
        """
        block = stripped[1:].strip()
        if not block:
            return '<p class="lyric-break"><br/></p>'

        rendered = MarkdownToXHTML._render_inline_markdown(block)

        # Lyric lines in memoir chapters are consistently italicized in blockquotes.
        if block.startswith("*") and block.endswith("*"):
            verse_type = ' epub:type="z3998:verse"' if epub3 else ""
            return f'<p class="lyric"{verse_type}>{rendered}</p>'

        return f'<p class="blockquote">{rendered}</p>'

    @staticmethod
    def _convert_blockquote_block(block: str) -> str:
        """
        Convert one paragraph that may contain multiple markdown blockquote lines.

        This preserves lyric stanza formatting when a stanza is serialized as:
        > *line 1*
        >
        > *line 2*

        Verse is wrapped in a <div class="poem"> container. Sibling <p> lines on
        their own leave nothing for `break-inside: avoid` to hold, so a fixed-form
        poem can be split across a page turn, and a runover line has no container
        to hang its indent from — on a narrow screen the wrapped remainder starts
        flush left and reads as one more line of verse. Where the line count is
        part of the text rather than its presentation, that is a correctness
        problem and not a cosmetic one. Quoted prose (`> text`, unemphasised) is
        left unwrapped: it is a blockquote, not a poem.

        epub:type is emitted for EPUB3 only. _build_epub2_chapter does not declare
        the epub namespace, so an unconditional attribute would make every EPUB2
        chapter invalid XHTML.
        """
        epub3 = get_epub_version() == "EPUB3"
        lines = []
        has_verse = False
        for raw in block.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith(">"):
                converted = MarkdownToXHTML._convert_blockquote_line(stripped, epub3)
                if 'class="lyric"' in converted:
                    has_verse = True
                lines.append(converted)
            else:
                # Mixed-content fallback: preserve non-blockquote lines as normal prose.
                rendered = MarkdownToXHTML._render_inline_markdown(stripped)
                lines.append(f'<p>{rendered}</p>')

        if not lines:
            return ""
        if not has_verse:
            return "\n".join(lines)

        poem_type = ' epub:type="%s"' % get_verse_epub_type() if epub3 else ""
        body = "\n".join("  " + line for line in lines)
        return f'<div class="poem"{poem_type}>\n{body}\n</div>'

    @staticmethod
    def _convert_structured_block(lines: List[str]) -> str:
        """
        Render a multi-line paragraph as the structure it actually is.

        Prose in this pipeline arrives one paragraph per line, so a paragraph
        that still contains newlines is never soft-wrapped text. Across the
        whole library every such block is deliberate: an in-world notebook, a
        message-board post and its header line, a crafting inventory, a cast
        roster, a letter's dateline and signature, a word-game grid whose
        columns line up. An unadorned <p> collapses all of them into one
        running line, which destroys the only thing that made them readable.

        Lists become real lists. Everything else keeps its line breaks, and
        leading spaces are preserved as non-breaking spaces because in an
        aligned block the indentation IS the content.
        """
        bullets = all(BULLET_ITEM_RE.match(ln.strip()) for ln in lines)
        numbered = all(NUMBERED_ITEM_RE.match(ln.strip()) for ln in lines)

        if bullets or numbered:
            pattern = BULLET_ITEM_RE if bullets else NUMBERED_ITEM_RE
            tag = "ul" if bullets else "ol"
            items = [
                "  <li>%s</li>"
                % MarkdownToXHTML._render_inline_markdown(pattern.sub("", ln.strip(), count=1))
                for ln in lines
            ]
            return '<%s class="doc-list">\n%s\n</%s>' % (tag, "\n".join(items), tag)

        rendered = []
        for ln in lines:
            indent = len(ln) - len(ln.lstrip(" "))
            body = MarkdownToXHTML._render_inline_markdown(ln.strip())
            rendered.append(("&#160;" * indent) + body)
        return '<p class="note">%s</p>' % "<br/>\n".join(rendered)

    @staticmethod
    def _render_inline_markdown(content: str) -> str:
        """Render one inline markdown fragment to escaped XHTML-safe HTML."""
        if re.search(r'!\[gaiji\]\([^)]+\)', content):
            content = MarkdownToXHTML._convert_inline_gaiji(content)

        if SMARTYPANTS_AVAILABLE:
            stacked_quotes = []
            stacked_pattern = re.compile(r'"{3,}([^"]+)"{3,}')

            def protect_stacked(match):
                stacked_quotes.append(match.group(0))
                return f"__STACKED_{len(stacked_quotes)-1}__"

            content = stacked_pattern.sub(protect_stacked, content)
            content = smartypants(content, Attr.q | Attr.D | Attr.e)
            for idx, original in enumerate(stacked_quotes):
                content = content.replace(f"__STACKED_{idx}__", original)

        escaped_content = content.replace('&', '&amp;')
        escaped_content = escaped_content.replace('&amp;#', '&#')
        escaped_content = escaped_content.replace('<', '&lt;')
        escaped_content = escaped_content.replace('>', '&gt;')

        return MarkdownToXHTML._convert_markdown_formatting(escaped_content)

    @staticmethod
    def _normalize_inline_images(para: str) -> str:
        """
        Normalize existing img tags to OEBPS standard paths.
        
        Converts EN translation inline images to proper EPUB format:
        - Updates path from ../image/ to ../Images/
        - Updates class from 'fit' to 'insert'
        
        Args:
            para: Paragraph text containing img tags
            
        Returns:
            Normalized paragraph text
        """
        # Update path from ../image/ to ../Images/
        para = re.sub(
            r'src="\.\./image/([^"]+)"',
            r'src="../Images/\1"',
            para
        )
        # Update class from 'fit' to 'insert'
        para = re.sub(
            r'class="fit"',
            r'class="insert"',
            para
        )
        return para

    @staticmethod
    def _convert_inline_gaiji(text: str) -> str:
        """Convert inline `![gaiji](filename)` markdown to XHTML gaiji image tags."""
        return re.sub(
            r'!\[gaiji\]\(([^)]+)\)',
            lambda m: f'<img class="gaiji" src="{IMAGES_PATH}/{m.group(1)}" alt=""/>',
            text
        )

    @staticmethod
    def _convert_markdown_formatting(text: str) -> str:
        """
        Convert markdown formatting to HTML tags.

        Converts:
        - **bold** to <strong>bold</strong>
        - *italic* to <em>italic</em>

        Args:
            text: Text with markdown formatting

        Returns:
            Text with HTML tags
        """
        # Convert **bold** to <strong>bold</strong>
        text = re.sub(r'\*\*([^*]+?)\*\*', r'<strong>\1</strong>', text)

        # Convert *italic* to <em>italic</em>
        text = re.sub(r'(?<!\*)\*(?!\*)([^*]+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)

        return text

    @staticmethod
    def _is_illustration_placeholder(text: str) -> bool:
        """Check if text contains an illustration placeholder (legacy or markdown format)."""
        # Legacy format: [ILLUSTRATION: filename]
        if re.search(ILLUSTRATION_PLACEHOLDER_PATTERN, text):
            return True
        # Markdown format: ![illustration](filename) or ![gaiji](filename)
        if re.search(MARKDOWN_IMAGE_PATTERN, text):
            return True
        return False

    @staticmethod
    def _convert_illustration_placeholder(text: str) -> str:
        """Convert illustration placeholder to XHTML img tag."""
        # Try legacy format first: [ILLUSTRATION: filename]
        match = re.search(ILLUSTRATION_PLACEHOLDER_PATTERN, text)
        if match:
            filename = match.group(1)
            return f'<p class="illustration"><img class="insert" src="{IMAGES_PATH}/{filename}" alt=""/></p>'
        
        # Try markdown format: ![alt](filename)
        match = re.search(MARKDOWN_IMAGE_PATTERN, text)
        if match:
            alt_type = match.group(1)  # 'illustration', 'gaiji', or ''
            filename = match.group(2)
            
            if alt_type == 'gaiji':
                # Gaiji: inline character - no wrapper paragraph, just the img
                # Will be embedded inline within surrounding text
                return f'<img class="gaiji" src="{IMAGES_PATH}/{filename}" alt=""/>'
            else:
                # Regular illustration - full block with wrapper
                return f'<p class="illustration"><img class="insert" src="{IMAGES_PATH}/{filename}" alt=""/></p>'
        
        return ""

    @staticmethod
    def convert_to_xhtml_string(paragraphs: List[str]) -> str:
        """
        Convert paragraphs to a single XHTML content string.

        Args:
            paragraphs: List of paragraph strings

        Returns:
            Concatenated XHTML paragraphs as string
        """
        filtered_paragraphs = MarkdownToXHTML._collapse_blank_lines(paragraphs)
        processed_paragraphs, footnotes = MarkdownToXHTML._prepare_footnotes(filtered_paragraphs)

        xhtml_paragraphs = MarkdownToXHTML.convert_paragraphs(processed_paragraphs)
        xhtml_content = '\n      '.join(xhtml_paragraphs)
        if footnotes:
            xhtml_content = MarkdownToXHTML._inject_noteref_links(xhtml_content, footnotes)
            footnotes_html = MarkdownToXHTML._build_footnotes_section(footnotes)
            if xhtml_content:
                return f"{xhtml_content}\n      {footnotes_html}"
            return footnotes_html

        return xhtml_content

    @staticmethod
    def _prepare_footnotes(paragraphs: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
        """Parse footnote definitions and inject noteref placeholders."""
        custom_marker_total = 0
        markdown_ref_ids = set()

        for para in paragraphs:
            if para == "<blank>":
                continue
            if MARKDOWN_FOOTNOTE_DEF_RE.match(para.strip()):
                continue
            custom_marker_total += len(CUSTOM_FOOTNOTE_MARKER_RE.findall(para))
            markdown_ref_ids.update(MARKDOWN_FOOTNOTE_MARKER_RE.findall(para))

        custom_defs: List[str] = []
        markdown_defs: Dict[str, str] = {}
        kept: List[str] = []

        prev_had_custom_marker = False
        for para in paragraphs:
            if para == "<blank>":
                kept.append(para)
                continue

            stripped = para.strip()
            md_def = MARKDOWN_FOOTNOTE_DEF_RE.match(stripped)
            if md_def:
                note_id = md_def.group(1).strip()
                if note_id in markdown_ref_ids:
                    markdown_defs[note_id] = md_def.group(2).strip()
                    prev_had_custom_marker = False
                    continue

            custom_def = CUSTOM_FOOTNOTE_DEF_COLON_RE.match(stripped) or CUSTOM_FOOTNOTE_DEF_SEP_RE.match(stripped)
            # Only collect a custom definition if the immediately preceding non-blank
            # paragraph contained a (*) marker — prevents orphan definitions (e.g. a
            # bold note without a marker) from shifting all subsequent footnotes.
            if custom_def and prev_had_custom_marker and len(custom_defs) < custom_marker_total:
                label = custom_def.group(1).strip().rstrip(":")
                body = custom_def.group(2).strip()
                custom_defs.append(f"{label}: {body}")
                prev_had_custom_marker = False
                continue

            prev_had_custom_marker = bool(CUSTOM_FOOTNOTE_MARKER_RE.search(stripped))
            kept.append(para)

        notes: List[Dict[str, str]] = []
        custom_index = 0
        output_paragraphs: List[str] = []

        for para in kept:
            if para == "<blank>":
                output_paragraphs.append(para)
                continue

            def markdown_ref_repl(match: re.Match) -> str:
                note_id = match.group(1).strip()
                note_text = markdown_defs.get(note_id)
                if not note_text:
                    return match.group(0)
                idx = len(notes) + 1
                notes.append({"number": str(idx), "text": note_text})
                return f"__NOTEREF_{idx}__"

            rendered = MARKDOWN_FOOTNOTE_MARKER_RE.sub(markdown_ref_repl, para)

            def custom_ref_repl(match: re.Match) -> str:
                nonlocal custom_index
                if custom_index >= len(custom_defs):
                    return match.group(0)
                idx = len(notes) + 1
                note_text = custom_defs[custom_index]
                custom_index += 1
                notes.append({"number": str(idx), "text": note_text})
                return f"__NOTEREF_{idx}__"

            rendered = CUSTOM_FOOTNOTE_MARKER_RE.sub(custom_ref_repl, rendered)
            output_paragraphs.append(rendered)

        return output_paragraphs, notes

    @staticmethod
    def _inject_noteref_links(xhtml_content: str, footnotes: List[Dict[str, str]]) -> str:
        """Replace `__NOTEREF_n__` placeholders with XHTML note reference links."""
        epub3 = get_epub_version() == "EPUB3"

        def repl(match: re.Match) -> str:
            num = int(match.group(1))
            if num < 1 or num > len(footnotes):
                return match.group(0)
            note_id = f"fn-{num}"
            ref_id = f"fnref-{num}"
            if epub3:
                return (
                    f'<a id="{ref_id}" class="noteref" href="#{note_id}" epub:type="noteref">[{num}]</a>'
                )
            return f'<a id="{ref_id}" class="noteref" href="#{note_id}">[{num}]</a>'

        return NOTEREF_PLACEHOLDER_RE.sub(repl, xhtml_content)

    @staticmethod
    def _build_footnotes_section(footnotes: List[Dict[str, str]]) -> str:
        """Build XHTML footnote section appended to the chapter body."""
        epub3 = get_epub_version() == "EPUB3"
        section_open = '<section class="footnotes" epub:type="footnotes">' if epub3 else '<section class="footnotes">'
        lines = [section_open, '        <h2>Notes</h2>', '        <ol class="footnote-list">']

        for item in footnotes:
            num = int(item["number"])
            note_id = f"fn-{num}"
            ref_id = f"fnref-{num}"
            note_html = MarkdownToXHTML._render_inline_markdown(item["text"])
            if epub3:
                lines.append(
                    '          '
                    + f'<li id="{note_id}" class="footnote-item" epub:type="footnote">'
                    + f'<p>{note_html} <a class="footnote-backref" href="#{ref_id}" aria-label="Back to text">↩</a></p>'
                    + '</li>'
                )
            else:
                lines.append(
                    '          '
                    + f'<li id="{note_id}" class="footnote-item">'
                    + f'<p>{note_html} <a class="footnote-backref" href="#{ref_id}" aria-label="Back to text">↩</a></p>'
                    + '</li>'
                )

        lines.extend(['        </ol>', '      </section>'])
        return "\n".join(lines)

    @staticmethod
    def _collapse_blank_lines(paragraphs: List[str]) -> List[str]:
        """
        Collapse consecutive <blank> markers to reduce visual breaks.
        Swallows the first blank line (standard paragraph separator) 
        but keeps subsequent ones as explicit breaks.

        Args:
            paragraphs: List of paragraph strings

        Returns:
            Filtered list with fewer <blank> markers
        """
        if not COLLAPSE_BLANK_LINES:
            return paragraphs

        filtered = []
        blank_run = 0

        for para in paragraphs:
            if para == "<blank>":
                blank_run += 1
                # Swallow the first blank line in any run
                # Keep any subsequent ones
                if blank_run > 1:
                    filtered.append(para)
            else:
                blank_run = 0
                filtered.append(para)

        return filtered

    @staticmethod
    def escape_xml_content(text: str) -> str:
        """Escape XML special characters in text content."""
        return escape(text)


def convert_paragraphs_to_xhtml(paragraphs: List[str], skip_illustrations: bool = False) -> str:
    """
    Main function to convert markdown paragraphs to XHTML.

    Args:
        paragraphs: List of paragraph strings
        skip_illustrations: If True, skip illustration placeholders

    Returns:
        XHTML content string
    """
    if skip_illustrations:
        xhtml_paragraphs = []
        for para in paragraphs:
            if para == "<blank>":
                xhtml_paragraphs.append('<p><br/></p>')
            elif not MarkdownToXHTML._is_illustration_placeholder(para):
                xhtml_para = MarkdownToXHTML._convert_single_paragraph(para, skip_illustrations=True)
                if xhtml_para:
                    xhtml_paragraphs.append(xhtml_para)
        return '\n      '.join(xhtml_paragraphs)
    else:
        return MarkdownToXHTML.convert_to_xhtml_string(paragraphs)


def extract_illustrations_from_paragraphs(paragraphs: List[str]) -> List[str]:
    """
    Extract illustration filenames from paragraph list.

    Args:
        paragraphs: List of paragraph strings

    Returns:
        List of illustration filenames
    """
    illustrations = []

    for para in paragraphs:
        match = re.search(ILLUSTRATION_PLACEHOLDER_PATTERN, para)
        if match:
            filename = match.group(1)
            illustrations.append(filename)

    return illustrations
