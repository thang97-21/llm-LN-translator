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

Parsing is deliberately forgiving. A block turn arrives after several earlier
turns have already been paid for and committed, so a response that is *almost*
JSON has to be salvaged rather than discarded — re-asking the model is both the
expensive path and, being stochastic, not a reliable one. loads_model_json()
repairs the failure modes models actually produce (unescaped control characters
inside string values, surrounding prose, inline <think> reasoning, a trailing
comma) and reports which tolerances it had to apply, so a salvage is never
silent.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json|xml)?\s*|\s*```$", re.MULTILINE)

# Inline reasoning (native OpenAI shape — a relay may not split it out even when
# the provider normally would). Stripped before parsing so braces inside the
# reasoning can't corrupt the object slice, matching what
# prep_cache_client.parse_fragment already does on the cached prep path.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# Characters XML 1.0 forbids outright: everything below 0x20 except tab/LF/CR,
# plus the two permanently-unassigned code points. Lenient JSON parsing lets
# these survive inside string values, and ElementTree will happily serialise one
# into context.xml — producing a file ElementTree itself then refuses to read
# back. Dropping them at conversion keeps a recoverable parse fault from turning
# into an unrecoverable one two phases downstream.
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

_CONTROL_CHAR_REPAIR = "accepted unescaped control characters inside string values"


class JsonNodeError(ValueError):
    """Raised when a model turn's response can't be read as a valid JSON node envelope."""


def strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", _THINK_RE.sub("", text)).strip()


def _slice_outermost_object(text: str) -> Optional[str]:
    """The span from the first '{' to the last '}', or None if there isn't one.

    Catches the wrapper the fence regex can't: a model that narrates around its
    object ("Here is the block:" ... "Let me know if you'd like more detail.").
    """
    first, last = text.find("{"), text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    return text[first : last + 1]


def _repair_candidates(stripped: str) -> Iterator[Tuple[List[str], str]]:
    """Yield (repairs_applied, text) in increasing order of intervention.

    The untouched text always comes first, so a well-formed response is never
    rewritten and never reports a repair it didn't need.
    """
    applied: List[str] = []
    yield list(applied), stripped

    sliced = _slice_outermost_object(stripped)
    if sliced is None:
        return
    if sliced != stripped:
        applied.append("trimmed prose surrounding the outermost JSON object")
        yield list(applied), sliced

    without_commas = _TRAILING_COMMA_RE.sub(r"\1", sliced)
    if without_commas != sliced:
        applied.append("removed a trailing comma before a closing brace/bracket")
        yield list(applied), without_commas


def loads_model_json(raw_text: str) -> Tuple[Any, List[str]]:
    """Parse text a model claims is JSON, repairing what models actually get wrong.

    Returns (value, repairs) where `repairs` names every tolerance that had to be
    applied — empty for a clean response, so a caller can distinguish "the model
    complied" from "we salvaged it". Raises json.JSONDecodeError when nothing
    works, leaving the domain error type to the caller.
    """
    stripped = strip_fences(raw_text)
    first_error: Optional[json.JSONDecodeError] = None

    for repairs, candidate in _repair_candidates(stripped):
        # Strict first: only fall back to the lenient decoder — which permits raw
        # newlines/tabs inside string values — once the correct reading has
        # actually been ruled out for this candidate.
        for strict in (True, False):
            try:
                value = json.loads(candidate, strict=strict)
            except json.JSONDecodeError as exc:
                if first_error is None:
                    first_error = exc
                continue
            return value, repairs if strict else repairs + [_CONTROL_CHAR_REPAIR]

    # Last resort: decode the first complete object and discard whatever trails
    # it, for when _slice_outermost_object over-reaches because the model's
    # closing prose contains a '}' of its own.
    start = stripped.find("{")
    if start != -1:
        for strict in (True, False):
            try:
                value, _end = json.JSONDecoder(strict=strict).raw_decode(stripped, start)
            except json.JSONDecodeError:
                continue
            repairs = ["decoded the first complete JSON object and discarded trailing text"]
            return value, repairs if strict else repairs + [_CONTROL_CHAR_REPAIR]

    if first_error is not None:
        raise first_error
    raise json.JSONDecodeError("no JSON object in response", stripped or "", 0)


def extract_json_node(raw_text: str, *, expected_tag: str | None = None) -> Dict[str, Any]:
    """Parse one model turn's response into a JSON node envelope.

    Raises JsonNodeError (never a bare json/other exception) so callers can
    report a single, consistent failure type regardless of what went wrong —
    malformed JSON, a non-object payload, or a tag that doesn't match what
    this turn was supposed to produce.
    """
    stripped = strip_fences(raw_text)
    try:
        data, repairs = loads_model_json(raw_text)
    except json.JSONDecodeError as exc:
        raise JsonNodeError(
            f"malformed JSON ({exc}) — response started with: {stripped[:300]!r}"
        ) from exc

    if repairs:
        # A salvage is a fact about model behaviour worth seeing in the run log,
        # not something to swallow because the turn ultimately succeeded.
        logger.warning(
            "[PREP:json-node] block '%s' returned non-conforming JSON; salvaged by: %s",
            expected_tag or "<untagged>", "; ".join(repairs),
        )

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


def _xml_safe(value: str) -> str:
    """Drop the characters XML 1.0 cannot represent (see _XML_ILLEGAL_RE)."""
    return _XML_ILLEGAL_RE.sub("", value)


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
    attrs = {
        str(key): "" if value is None else _xml_safe(str(value))
        for key, value in raw_attrs.items()
    }

    element = ET.Element(tag, attrs)

    text = node.get("text")
    if text is not None:
        element.text = _xml_safe(str(text))

    children = node.get("children") or []
    if not isinstance(children, list):
        raise JsonNodeError(f"<{tag}>'s 'children' must be an array, got {type(children).__name__}")
    for child in children:
        element.append(node_to_element(child))

    return element
