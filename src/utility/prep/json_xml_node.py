"""
Generic JSON-node <-> XML element converter for the multi-turn prep path.

Every block turn in multiturn_agent.py returns its content as ONE small JSON
envelope instead of raw XML:

    {"tag": "...", "attrs": {...}, "text": "...", "children": [...]}

`attrs`, `text`, and `children` are all optional; `children` is a list of
nodes in this same shape. This is deliberately the ONLY schema every block
turn uses — the block-specific shape (what `character_roster` vs `name_map`
vs `eps_arc_tracker` actually looks like as XML) still lives in exactly one
place, the prose `BLOCK_SCHEMA` text in src/Deepseek/prompt/prep_prompt_deepseek_en.xml
(reused verbatim by block_prompts.py). Defining 15 separate JSON schemas here
would just be that same shape written a second time, with its own drift risk.

node_to_element() is the deterministic script referenced by config.yaml's
prep.multi_turn docs: it never asks the model anything, it only converts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict
from xml.etree import ElementTree as ET

_FENCE_RE = re.compile(r"^```(?:json|xml)?\s*|\s*```$", re.MULTILINE)


class JsonNodeError(ValueError):
    """Raised when a model turn's response can't be read as a valid JSON node envelope."""


def strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def extract_json_node(raw_text: str, *, expected_tag: str | None = None) -> Dict[str, Any]:
    """Parse one model turn's response into a JSON node envelope.

    Raises JsonNodeError (never a bare json/other exception) so callers can
    report a single, consistent failure type regardless of what went wrong —
    malformed JSON, a non-object payload, or a tag that doesn't match what
    this turn was supposed to produce.
    """
    stripped = strip_fences(raw_text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise JsonNodeError(
            f"malformed JSON ({exc}) — response started with: {stripped[:300]!r}"
        ) from exc

    if not isinstance(data, dict):
        raise JsonNodeError(
            f"expected a JSON object node, got {type(data).__name__} — "
            f"response started with: {stripped[:300]!r}"
        )
    tag = data.get("tag")
    if not isinstance(tag, str) or not tag:
        raise JsonNodeError(
            f"JSON node is missing a non-empty string 'tag' — got: {stripped[:300]!r}"
        )
    if expected_tag is not None and tag != expected_tag:
        raise JsonNodeError(
            f"expected block '{expected_tag}', got tag '{tag}' — response started with: "
            f"{stripped[:300]!r}"
        )
    return data


def node_to_element(node: Dict[str, Any]) -> ET.Element:
    """Recursively convert one JSON node envelope into an ET.Element.

    Deterministic and total over any well-formed envelope — every field
    (attrs/text/children) is optional, and this never talks to a model.
    """
    if not isinstance(node, dict):
        raise JsonNodeError(f"expected a JSON object node, got {type(node).__name__}")
    tag = node.get("tag")
    if not isinstance(tag, str) or not tag:
        raise JsonNodeError(f"node is missing a non-empty string 'tag': {node!r}")

    raw_attrs = node.get("attrs") or {}
    if not isinstance(raw_attrs, dict):
        raise JsonNodeError(f"<{tag}>'s 'attrs' must be an object, got {type(raw_attrs).__name__}")
    attrs = {str(key): "" if value is None else str(value) for key, value in raw_attrs.items()}

    element = ET.Element(tag, attrs)

    text = node.get("text")
    if text is not None:
        element.text = str(text)

    children = node.get("children") or []
    if not isinstance(children, list):
        raise JsonNodeError(f"<{tag}>'s 'children' must be an array, got {type(children).__name__}")
    for child in children:
        element.append(node_to_element(child))

    return element
