"""
THINKING density telemetry — translator-only (see CLAUDE.md's "only translator
emit this": prep runs single-shot structured extraction calls, not the
Multi-round Conversation + prompt-caching loop this measures the shape of).

Measures how much of the injected canon graph a chapter's reasoning actually
engaged with — which context.xml blocks and named characters got referenced —
against how long that reasoning ran, then compiles both into one self-
contained HTML report per volume: WORK/<vol>/THINKING/density_map.html.

Grounded in real THINKING/*.md output, not the prompt's aspirational
[TAG]-bracket format from src/Deepseek/prompt/master_prompt_deepseek_en.xml's
INTERNAL_REASONING section, which the model does not reliably follow in
practice (confirmed against WORK/.../THINKING/CHAPTER_01_THINKING.md and
CHAPTER_05_THINKING.md for a real volume): reasoning comes back as prose with
bolded stage headers that vary in wording and grouping call to call, context.xml
block names appear inline ("eps_signals", "voice_fingerprints", ...) rather
than as machine-parseable tags, and characters get referenced by given name
("Yuuhi") far more often than by full canonical_name ("Narumiya Yuuhi").
Detection here is regex/keyword-based against that real shape, not strict
tag parsing.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

# ══════════════════════════════════════════════════════════════════════════
# Vocabulary — sourced live from each volume's own context.xml, never a
# hardcoded cast list (every volume has a different one).
# ══════════════════════════════════════════════════════════════════════════

# Every context.xml block name a chapter's reasoning could plausibly reference.
# character_attribute_anchors is included even though the lightweight prep
# client never fills it (src/utility/prep/agent.py) — if it's ever populated by a
# future prep revision or a hand-edited context.xml, it should still count.
CONTEXT_XML_BLOCKS: Tuple[str, ...] = (
    "volume_identity", "world_setting", "character_roster", "name_map",
    "relationship_graph", "verbatim_anchors", "character_attribute_anchors",
    "voice_fingerprints", "cultural_glossary", "eps_arc_tracker", "eps_signals",
    "scene_plans", "illustration_context", "translation_brief",
    "chapter_titles_en", "validation_audit",
)

# INTERNAL_REASONING's 7 stages (master_prompt_deepseek_en.xml lines 439-451),
# matched by keyword rather than exact header text — real output splits or
# renames them freely (DEVICE_LEDGER and TRANSLATION_MEMORY routinely get
# their own bolded header instead of staying folded into stage 3 as the
# prompt names it; numbering is sometimes present, sometimes dropped).
_STAGE_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("canon_activation", r"canon activation"),
    ("scene_analysis", r"scene analysis"),
    ("translation_decisions", r"translation decisions"),
    ("device_ledger", r"device[_ ]ledger"),
    ("translation_memory", r"translation memory"),
    ("critical_passage", r"critical[- ]passage"),
    ("voice_self_critique", r"voice\b.{0,25}(negative.space|self.critique)"),
    ("calibration", r"\bcalibration\b"),
    ("active_recall", r"active recall|verbatim recall"),
)

_WORD_RE = re.compile(r"\S+")
_CHAPTER_FROM_FILENAME_RE = re.compile(r"^(.*?)_THINKING$")


class ThinkingDensityError(RuntimeError):
    """Raised when the density report can't be built (bad context.xml, no
    THINKING logs yet, ...). Callers should treat this as skip-not-fatal."""


@dataclass
class ChapterDensity:
    chapter_id: str
    word_count: int
    blocks_activated: List[str] = field(default_factory=list)
    characters_mentioned: List[str] = field(default_factory=list)
    stages_found: List[str] = field(default_factory=list)
    blocks_total: int = 0


# ══════════════════════════════════════════════════════════════════════════
# Extraction
# ══════════════════════════════════════════════════════════════════════════

def _extract_reasoning_body(thinking_md_text: str) -> str:
    """THINKING/<chapter>_THINKING.md wraps the actual reasoning under
    "## API reasoning (reasoning_content)" (see thinking_output.py's
    merge_thinking_log / agent.py's _maybe_write_thinking_log). Falls back to
    the whole file if that marker is ever absent — never fail on format drift,
    a slightly-noisier word count beats a skipped chapter."""
    marker = "## API reasoning (reasoning_content)"
    idx = thinking_md_text.find(marker)
    if idx == -1:
        return thinking_md_text
    return thinking_md_text[idx + len(marker):]


def _load_vocabulary(context_xml_path: Path) -> Tuple[List[str], List[str]]:
    """(block_names_present, character_canonical_names) read live from this
    volume's own context.xml."""
    try:
        root = ET.fromstring(context_xml_path.read_text(encoding="utf-8"))
    except ET.ParseError as exc:
        raise ThinkingDensityError(f"context.xml at {context_xml_path} is malformed: {exc}") from exc
    blocks_present = [b for b in CONTEXT_XML_BLOCKS if root.find(b) is not None]
    characters = [
        c.get("canonical_name", "").strip()
        for c in root.findall("./character_roster/character")
        if c.get("canonical_name", "").strip()
    ]
    return blocks_present, characters


def _block_pattern(block_name: str) -> re.Pattern:
    # "eps_signals" and "eps signals" both count — real output uses either.
    spaced = block_name.replace("_", "[_ ]?")
    return re.compile(rf"\b{spaced}\b", re.IGNORECASE)


def _character_pattern(canonical_name: str) -> Optional[re.Pattern]:
    tokens = canonical_name.split()
    if not tokens:
        return None
    # Given-name-only reference is the common case in real reasoning prose
    # (surname-first canonical_name, but "Yuuhi" not "Narumiya Yuuhi" gets
    # used turn after turn) — match the full name OR its last token.
    candidates = {canonical_name, tokens[-1]}
    alt = "|".join(re.escape(c) for c in candidates if len(c) >= 2)
    if not alt:
        return None
    return re.compile(rf"\b({alt})\b")


def compute_chapter_density(
    reasoning_text: str, blocks_present: List[str], characters: List[str],
) -> Tuple[int, List[str], List[str], List[str]]:
    word_count = len(_WORD_RE.findall(reasoning_text))
    blocks_activated = [b for b in blocks_present if _block_pattern(b).search(reasoning_text)]
    characters_mentioned = []
    for name in characters:
        pattern = _character_pattern(name)
        if pattern is not None and pattern.search(reasoning_text):
            characters_mentioned.append(name)
    stages_found = [
        label for label, pattern in _STAGE_PATTERNS
        if re.search(pattern, reasoning_text, re.IGNORECASE)
    ]
    return word_count, blocks_activated, characters_mentioned, stages_found


def _chapter_sort_key(chapter_id: str) -> Tuple[int, str]:
    match = re.search(r"(\d+)", chapter_id)
    return (int(match.group(1)) if match else 0, chapter_id)


def collect_chapter_densities(work_dir: Path) -> List[ChapterDensity]:
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise ThinkingDensityError(f"No context.xml at {context_path}.")
    thinking_dir = work_dir / "THINKING"
    if not thinking_dir.is_dir():
        raise ThinkingDensityError(f"No THINKING/ directory at {thinking_dir} — nothing to analyze yet.")

    blocks_present, characters = _load_vocabulary(context_path)
    if not blocks_present:
        raise ThinkingDensityError(f"context.xml at {context_path} has no recognized blocks filled in yet.")

    results: List[ChapterDensity] = []
    for md_path in sorted(thinking_dir.glob("*_THINKING.md")):
        chapter_match = _CHAPTER_FROM_FILENAME_RE.match(md_path.stem)
        chapter_id = chapter_match.group(1) if chapter_match else md_path.stem
        reasoning_text = _extract_reasoning_body(md_path.read_text(encoding="utf-8"))
        word_count, blocks_activated, chars_mentioned, stages = compute_chapter_density(
            reasoning_text, blocks_present, characters,
        )
        results.append(ChapterDensity(
            chapter_id=chapter_id,
            word_count=word_count,
            blocks_activated=blocks_activated,
            characters_mentioned=chars_mentioned,
            stages_found=stages,
            blocks_total=len(blocks_present),
        ))
    results.sort(key=lambda d: _chapter_sort_key(d.chapter_id))
    return results


# ══════════════════════════════════════════════════════════════════════════
# Rendering — one self-contained HTML file, no CDN, light/dark via the
# validated reference palette (dataviz skill, references/palette.md):
# sequential blue for magnitude, chart chrome/ink tokens for both modes.
# Bubble map and heatmap share one x-scale (per-chapter column) so a reader
# can align "this bubble" to "this heatmap column" by eye — the two views
# are one coordinated map, not two unrelated charts.
# ══════════════════════════════════════════════════════════════════════════

_LEFT_MARGIN = 190
_COL_WIDTH = 34
_RIGHT_PAD = 24
_BUBBLE_CHART_HEIGHT = 240
_BUBBLE_TOP_PAD = 20
_BUBBLE_BOTTOM_PAD = 36
_HEATMAP_ROW_HEIGHT = 26
_HEATMAP_TOP_LABEL_HEIGHT = 110
_CELL_GAP = 2


def _short_label(chapter_id: str) -> str:
    return re.sub(r"^CHAPTER_", "", chapter_id)


def _x_for(index: int) -> int:
    return _LEFT_MARGIN + index * _COL_WIDTH + _COL_WIDTH // 2


def _render_bubble_chart(results: List[ChapterDensity]) -> str:
    width = _LEFT_MARGIN + len(results) * _COL_WIDTH + _RIGHT_PAD
    height = _BUBBLE_CHART_HEIGHT
    plot_top = _BUBBLE_TOP_PAD
    plot_bottom = height - _BUBBLE_BOTTOM_PAD
    max_words = max((r.word_count for r in results), default=1) or 1

    def y_for(word_count: int) -> float:
        frac = word_count / max_words
        return plot_bottom - frac * (plot_bottom - plot_top)

    # Gridlines at "nice" round word-count steps.
    step = max(250, round(max_words / 4 / 250) * 250)
    gridlines = []
    tick = 0
    while tick <= max_words:
        y = y_for(tick)
        gridlines.append(
            f'<line x1="{_LEFT_MARGIN - 8}" y1="{y:.1f}" x2="{width - _RIGHT_PAD}" y2="{y:.1f}" '
            f'class="gridline" />'
            f'<text x="{_LEFT_MARGIN - 14}" y="{y:.1f}" class="tick-label" text-anchor="end" '
            f'dominant-baseline="middle">{tick:,}</text>'
        )
        tick += step

    bubbles = []
    for i, r in enumerate(results):
        cx = _x_for(i)
        cy = y_for(r.word_count)
        breadth_frac = (len(r.blocks_activated) / r.blocks_total) if r.blocks_total else 0.0
        radius = 4 + breadth_frac * 10  # 8px-28px diameter, per mark spec floor
        title = (
            f"{html.escape(r.chapter_id)}: {r.word_count:,} words, "
            f"{len(r.blocks_activated)}/{r.blocks_total} blocks activated, "
            f"{len(r.characters_mentioned)} characters mentioned, "
            f"{len(r.stages_found)} reasoning stages detected"
        )
        zero_class = " bubble-zero" if not r.blocks_activated else ""
        bubbles.append(
            f'<circle cx="{cx}" cy="{cy:.1f}" r="{radius:.1f}" class="bubble{zero_class}">'
            f'<title>{html.escape(title)}</title></circle>'
        )

    x_labels = [
        f'<text x="{_x_for(i)}" y="{plot_bottom + 16}" class="axis-label" text-anchor="middle">'
        f'{html.escape(_short_label(r.chapter_id))}</text>'
        for i, r in enumerate(results)
    ]

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'class="viz-svg" role="img" aria-label="Reasoning length and block-activation breadth per chapter">'
        + "".join(gridlines)
        + f'<line x1="{_LEFT_MARGIN - 8}" y1="{plot_bottom}" x2="{width - _RIGHT_PAD}" y2="{plot_bottom}" class="axis-line" />'
        + "".join(bubbles)
        + "".join(x_labels)
        + "</svg>"
    )


def _render_heatmap(results: List[ChapterDensity], blocks_present: List[str]) -> str:
    width = _LEFT_MARGIN + len(results) * _COL_WIDTH + _RIGHT_PAD
    height = _HEATMAP_TOP_LABEL_HEIGHT + len(blocks_present) * _HEATMAP_ROW_HEIGHT + 10

    row_labels = [
        f'<text x="{_LEFT_MARGIN - 12}" y="{_HEATMAP_TOP_LABEL_HEIGHT + row * _HEATMAP_ROW_HEIGHT + _HEATMAP_ROW_HEIGHT / 2:.1f}" '
        f'class="row-label" text-anchor="end" dominant-baseline="middle">{html.escape(block)}</text>'
        for row, block in enumerate(blocks_present)
    ]
    col_labels = [
        f'<text x="0" y="0" class="axis-label heatmap-col-label" '
        f'transform="translate({_x_for(i)},{_HEATMAP_TOP_LABEL_HEIGHT - 8}) rotate(-45)" text-anchor="start">'
        f'{html.escape(_short_label(r.chapter_id))}</text>'
        for i, r in enumerate(results)
    ]

    cells = []
    for row, block in enumerate(blocks_present):
        for col, r in enumerate(results):
            activated = block in r.blocks_activated
            cx = _LEFT_MARGIN + col * _COL_WIDTH
            cy = _HEATMAP_TOP_LABEL_HEIGHT + row * _HEATMAP_ROW_HEIGHT
            cell_w = _COL_WIDTH - _CELL_GAP
            cell_h = _HEATMAP_ROW_HEIGHT - _CELL_GAP
            cls = "cell-active" if activated else "cell-inactive"
            title = f"{html.escape(block)} × {html.escape(r.chapter_id)}: {'activated' if activated else 'not referenced'}"
            cells.append(
                f'<rect x="{cx}" y="{cy}" width="{cell_w}" height="{cell_h}" rx="3" class="{cls}">'
                f'<title>{title}</title></rect>'
            )

    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'class="viz-svg" role="img" aria-label="Which context.xml blocks each chapter\'s reasoning referenced">'
        + "".join(row_labels)
        + "".join(col_labels)
        + "".join(cells)
        + "</svg>"
    )


def _render_table(results: List[ChapterDensity]) -> str:
    rows = []
    for r in results:
        rows.append(
            "<tr>"
            f"<td>{html.escape(r.chapter_id)}</td>"
            f'<td class="num">{r.word_count:,}</td>'
            f'<td class="num">{len(r.blocks_activated)}/{r.blocks_total}</td>'
            f"<td>{html.escape(', '.join(r.blocks_activated) or '—')}</td>"
            f"<td>{html.escape(', '.join(r.characters_mentioned) or '—')}</td>"
            f'<td class="num">{len(r.stages_found)}</td>'
            "</tr>"
        )
    return (
        '<table class="density-table">'
        "<thead><tr><th>Chapter</th><th>Words</th><th>Blocks</th>"
        "<th>Blocks activated</th><th>Characters mentioned</th><th>Stages</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _stat_tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="stat-note">{html.escape(note)}</div>' if note else ""
    return (
        '<div class="stat-tile">'
        f'<div class="stat-label">{html.escape(label)}</div>'
        f'<div class="stat-value">{html.escape(value)}</div>'
        f"{note_html}</div>"
    )


_CSS = """
.viz-root {
  color-scheme: light;
  --surface-1:      #fcfcfb;
  --page-plane:      #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --gridline:       #e1e0d9;
  --axis-line:      #c3c2b7;
  --series-1:       #2a78d6;
  --series-1-weak:  #cde2fb;
  --border:         rgba(11,11,11,0.10);
  --warn:           #fab219;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page-plane:      #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --gridline:       #2c2c2a;
    --axis-line:      #383835;
    --series-1:       #3987e5;
    --series-1-weak:  #184f95;
    --border:         rgba(255,255,255,0.10);
    --warn:           #fab219;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1:      #1a1a19;
  --page-plane:      #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --gridline:       #2c2c2a;
  --axis-line:      #383835;
  --series-1:       #3987e5;
  --series-1-weak:  #184f95;
  --border:         rgba(255,255,255,0.10);
  --warn:           #fab219;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px;
  background: var(--page-plane); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
.viz-root { max-width: 1000px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 4px; }
.subtitle { color: var(--text-secondary); margin: 0 0 24px; font-size: 0.95rem; }
.panel {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px; margin-bottom: 20px; overflow-x: auto;
}
.panel h2 { font-size: 1.05rem; margin: 0 0 4px; }
.panel .panel-note { color: var(--text-secondary); font-size: 0.85rem; margin: 0 0 16px; }
.stat-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }
.stat-tile {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px; min-width: 150px; flex: 1;
}
.stat-label { color: var(--text-secondary); font-size: 0.8rem; }
.stat-value { font-size: 1.6rem; font-weight: 600; margin-top: 2px; }
.stat-note { color: var(--warn); font-size: 0.78rem; margin-top: 2px; }
.viz-svg { display: block; }
.gridline { stroke: var(--gridline); stroke-width: 1; }
.axis-line { stroke: var(--axis-line); stroke-width: 1; }
.tick-label { fill: var(--text-muted); font-size: 10px; }
.axis-label { fill: var(--text-secondary); font-size: 10px; }
.row-label { fill: var(--text-secondary); font-size: 10px; font-family: ui-monospace, monospace; }
.bubble { fill: var(--series-1); fill-opacity: 0.75; stroke: var(--surface-1); stroke-width: 2; }
.bubble-zero { fill: var(--text-muted); fill-opacity: 0.5; }
.cell-active { fill: var(--series-1); }
.cell-inactive { fill: var(--gridline); }
.density-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.density-table th, .density-table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
.density-table td.num, .density-table th:nth-child(2), .density-table th:nth-child(3) { text-align: right; font-variant-numeric: tabular-nums; }
.density-table thead th { color: var(--text-secondary); font-weight: 600; font-size: 0.78rem; }
details summary { cursor: pointer; color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 8px; }
"""


def render_density_html(volume_id: str, results: List[ChapterDensity], blocks_present: List[str]) -> str:
    n = len(results)
    avg_words = round(sum(r.word_count for r in results) / n) if n else 0
    avg_blocks = round(sum(len(r.blocks_activated) for r in results) / n, 1) if n else 0.0
    zero_block_chapters = [r.chapter_id for r in results if not r.blocks_activated]

    stat_tiles = "".join([
        _stat_tile("Chapters analyzed", str(n)),
        _stat_tile("Avg. reasoning length", f"{avg_words:,} words"),
        _stat_tile("Avg. blocks activated", f"{avg_blocks} / {len(blocks_present)}"),
        _stat_tile(
            "Zero-activation chapters", str(len(zero_block_chapters)),
            note=(", ".join(zero_block_chapters) if zero_block_chapters else ""),
        ),
    ])

    bubble_svg = _render_bubble_chart(results) if results else ""
    heatmap_svg = _render_heatmap(results, blocks_present) if results and blocks_present else ""
    table_html = _render_table(results)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>THINKING Density — {html.escape(volume_id)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="viz-root">
  <h1>THINKING Density — {html.escape(volume_id)}</h1>
  <p class="subtitle">
    Length and canon-graph breadth per chapter's silent reasoning (INTERNAL_REASONING,
    master_prompt_deepseek_en.xml). Detected by keyword/regex against real THINKING/*.md
    output, not the prompt's aspirational [TAG] format — the model doesn't reliably emit
    that; see the module docstring in src/translator/thinking_density.py.
  </p>

  <div class="stat-row">{stat_tiles}</div>

  <div class="panel">
    <h2>Reasoning length × block-activation breadth</h2>
    <p class="panel-note">
      Each dot is one chapter. Position (Y) is reasoning length in words; dot size is how
      many of this volume's {len(blocks_present)} filled context.xml blocks got referenced
      that turn. A gray dot activated none — hover any dot for exact counts.
    </p>
    {bubble_svg}
  </div>

  <div class="panel">
    <h2>Which blocks, which chapters</h2>
    <p class="panel-note">
      Binary presence, not magnitude — a filled cell means the block was referenced at
      least once in that chapter's reasoning. Hover a cell for detail.
    </p>
    {heatmap_svg}
  </div>

  <details class="panel">
    <summary>Per-chapter data table</summary>
    {table_html}
  </details>
</div>
</body>
</html>
"""


def build_density_report(work_dir: Path, volume_id: str) -> Optional[Path]:
    """Rebuild WORK/<vol>/THINKING/density_map.html from every THINKING/*.md
    file currently on disk. Returns None (never raises) if there's nothing to
    analyze yet — callers treat this as skip-not-fatal, matching how
    thinking_log itself degrades when disabled."""
    try:
        context_path = work_dir / "context.xml"
        blocks_present, _ = _load_vocabulary(context_path)
        results = collect_chapter_densities(work_dir)
    except ThinkingDensityError:
        return None
    if not results:
        return None

    from src.Deepseek.common.atomic_io import atomic_write_text
    out_path = work_dir / "THINKING" / "density_map.html"
    atomic_write_text(out_path, render_density_html(volume_id, results, blocks_present))
    return out_path
