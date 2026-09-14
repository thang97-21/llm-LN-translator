"""Deterministic completeness checks between a chapter's source and its translation.

The companion to verbatim_anchors.py. That module asks "did the locked
*phrases* survive"; this one asks "did the chapter's *structure and bulk*
survive" — the failure class no anchor can catch, because the thing lost was
never a locked phrase to begin with.

Motivating case, volume 6e63bc CHAPTER_04: the shipped English dropped both of
its source's illustration placeholders (``p085.jpg``, ``p121.jpg``) and a short
dialogue exchange, while every anchor the chapter carried was honoured. Nothing
in the pipeline noticed, because nothing in the pipeline was counting. The
advisor could not have caught it either — it is consulted BEFORE drafting and
never sees the finished prose.

Counting is the right instrument here, and counting belongs to code. Asking a
frontier model to certify that nothing is absent from a 30,000-token draft is
both the most expensive way to ask and the least reliable answer available.
"""

from __future__ import annotations

import re
import statistics
from typing import Dict, List, Optional, Sequence

# Markdown image embeds. The pipeline carries illustration placeholders through
# translation verbatim (``![illustration](p085.jpg)``) so the builder can place
# the real asset later; a dropped tag silently loses a page of artwork from the
# finished book.
ILLUSTRATION_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# Japanese ranges used for the bulk comparison: kana + CJK ideographs. Latin
# characters, digits and punctuation are excluded so that a chapter heavy with
# romanized names does not skew its own ratio.
_JP_CHAR_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]")

# How far a chapter's JP-chars-per-EN-word ratio may stray from the volume's
# own median before it is worth a human's eye. Translation density genuinely
# varies with dialogue-to-narration mix, so this is deliberately loose: it is
# a sentinel for "this chapter lost a chunk", not a style metric.
DEFAULT_RATIO_TOLERANCE = 0.35

# A volume needs at least this many already-translated peers before a median
# means anything. Below it, the check reports `insufficient_peers` rather than
# inventing a baseline from one or two samples.
MIN_PEERS_FOR_RATIO = 3


def illustration_ids(text: str) -> List[str]:
    """Every markdown image target in *text*, in document order."""
    return [match.group(1).strip() for match in ILLUSTRATION_RE.finditer(text or "")]


def check_illustration_parity(jp_source: str, en_text: str) -> Dict[str, object]:
    """Compare illustration placeholders between source and translation.

    Exact parity is required, and order is preserved in the report so a
    reviewer can see *which* plate went missing rather than only how many.
    A chapter whose source carries no illustrations passes trivially.
    """
    jp_ids = illustration_ids(jp_source)
    en_ids = illustration_ids(en_text)
    missing = [asset for asset in jp_ids if asset not in en_ids]
    unexpected = [asset for asset in en_ids if asset not in jp_ids]
    if missing or unexpected or len(jp_ids) != len(en_ids):
        status = "mismatch"
    else:
        status = "confirmed"
    return {
        "check": "illustration_parity",
        "status": status,
        "jp_count": len(jp_ids),
        "en_count": len(en_ids),
        "missing": missing,
        "unexpected": unexpected,
    }


def jp_char_count(text: str) -> int:
    """Kana + ideograph count, excluding latin/digits/punctuation."""
    return len(_JP_CHAR_RE.findall(text or ""))


def en_word_count(text: str) -> int:
    return len((text or "").split())


def length_ratio(jp_source: str, en_text: str) -> Optional[float]:
    """Japanese characters per English word, or None when uncomputable."""
    words = en_word_count(en_text)
    if not words:
        return None
    chars = jp_char_count(jp_source)
    if not chars:
        return None
    return chars / words


def check_length_ratio(
    jp_source: str,
    en_text: str,
    peer_ratios: Sequence[float],
    *,
    tolerance: float = DEFAULT_RATIO_TOLERANCE,
) -> Dict[str, object]:
    """Flag a chapter whose translation bulk departs from the volume's norm.

    A chapter that quietly drops an exchange renders fewer English words for
    the same Japanese, pushing its ratio ABOVE the median. The reverse
    (padding, or a leaked untranslated fragment) pushes it below. Both are
    worth a look, so the band is two-sided.

    This is advisory by design and should not gate a run on its own: a
    legitimately dialogue-dense chapter can sit outside the band innocently.
    It earns its place by being free and by pointing a reviewer at the right
    chapter out of twenty.
    """
    ratio = length_ratio(jp_source, en_text)
    usable = [value for value in peer_ratios if value and value > 0]
    if ratio is None:
        return {"check": "length_ratio", "status": "not_checked", "ratio": None}
    if len(usable) < MIN_PEERS_FOR_RATIO:
        return {
            "check": "length_ratio",
            "status": "insufficient_peers",
            "ratio": round(ratio, 4),
            "peers": len(usable),
        }
    median = statistics.median(usable)
    deviation = abs(ratio - median) / median if median else 0.0
    return {
        "check": "length_ratio",
        "status": "outlier" if deviation > tolerance else "confirmed",
        "ratio": round(ratio, 4),
        "median": round(median, 4),
        "deviation": round(deviation, 4),
        "tolerance": tolerance,
        "peers": len(usable),
    }


def structural_report(
    jp_source: str,
    en_text: str,
    peer_ratios: Sequence[float] = (),
    *,
    tolerance: float = DEFAULT_RATIO_TOLERANCE,
) -> List[Dict[str, object]]:
    """Every structural check for one chapter, as reportable rows."""
    return [
        check_illustration_parity(jp_source, en_text),
        check_length_ratio(jp_source, en_text, peer_ratios, tolerance=tolerance),
    ]
