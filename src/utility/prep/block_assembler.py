"""
Parallel-prep output assembly — merges Phase 1's Pro root fragment and Phase
2's 13 Flash block fragments into one context.xml tree, in place of the
unified path's single "echo the whole document back" response.

Each model response is expected to be one or more bare top-level XML
elements (no document wrapper) — see block_prompts.py's OUTPUT_CONTRACT
text, which is what asks the model to return exactly that shape.
"""

from __future__ import annotations

from typing import Dict, Iterable, List
from xml.etree import ElementTree as ET

from src.utility.prep.agent import _strip_fences  # same de-fencing the unified path uses
from src.utility.prep.block_prompts import PRO_BLOCKS


class AssemblyError(RuntimeError):
    """Raised when a model response can't be parsed into the expected block(s)."""


def parse_fragment(raw_text: str, expected_tags: Iterable[str]) -> Dict[str, ET.Element]:
    """Parse a response expected to contain one or more bare top-level elements.

    Wraps in a synthetic root since a multi-element fragment (e.g. Phase 1's
    character_roster + name_map) isn't valid XML on its own.
    """
    stripped = _strip_fences(raw_text)
    try:
        root = ET.fromstring(f"<_root>{stripped}</_root>")
    except ET.ParseError as exc:
        raise AssemblyError(
            f"malformed XML fragment ({exc}) — expected {list(expected_tags)}. "
            f"Response started with: {stripped[:300]!r}"
        ) from exc

    found = {child.tag: child for child in root}
    missing = [tag for tag in expected_tags if tag not in found]
    if missing:
        raise AssemblyError(
            f"expected block(s) {missing} not found in response — got {list(found)}. "
            f"Response started with: {stripped[:300]!r}"
        )
    return found


def _replace_block(root: ET.Element, tag: str, new_el: ET.Element) -> None:
    """Swap the existing <tag> child for new_el, preserving its position in
    document order (falls back to appending if the barebone never had it —
    true for chapter_titles_en, which prep creates fresh)."""
    for index, child in enumerate(root):
        if child.tag == tag:
            root.remove(child)
            root.insert(index, new_el)
            return
    root.append(new_el)


def assemble_context_xml(
    existing_context_xml: str,
    pro_raw: str,
    flash_fragments_raw: Dict[str, str],
) -> ET.Element:
    """Merge the barebone context.xml with Phase 1's Pro fragment and Phase
    2's per-block Flash fragments. Raises AssemblyError on any malformed or
    missing block — the caller decides whether that's fatal or a fallback
    trigger, this function never silently drops a block."""
    root = ET.fromstring(existing_context_xml)

    pro_fragments = parse_fragment(pro_raw, PRO_BLOCKS)
    for tag, el in pro_fragments.items():
        _replace_block(root, tag, el)

    for block_name, raw in flash_fragments_raw.items():
        parsed = parse_fragment(raw, (block_name,))
        _replace_block(root, block_name, parsed[block_name])

    return root


def validate_cross_block_consistency(root: ET.Element) -> List[str]:
    """Best-effort character-name consistency check across independently
    generated blocks. Non-fatal — Flash calls run in parallel with no
    visibility into each other's output, so minor drift is a warning to
    surface, not a reason to fail an otherwise-good prep run."""
    warnings: List[str] = []

    roster = root.find("character_roster")
    if roster is None:
        return warnings
    canonical_names = {
        c.get("canonical_name") for c in roster.findall("character") if c.get("canonical_name")
    }
    if not canonical_names:
        return warnings

    for block_name, attr in (("voice_fingerprints", "character"), ("eps_arc_tracker", "character")):
        block = root.find(block_name)
        if block is None:
            continue
        for el in block:
            name = el.get(attr)
            if name and name not in canonical_names:
                warnings.append(f"{block_name}: character '{name}' not found in character_roster")

    eps_signals = root.find("eps_signals")
    if eps_signals is not None:
        for chapter_el in eps_signals.findall("chapter"):
            for char_el in chapter_el.findall("character"):
                name = char_el.get("name")
                if name and name not in canonical_names:
                    warnings.append(
                        f"eps_signals[{chapter_el.get('id')}]: character '{name}' not in character_roster"
                    )

    return warnings
