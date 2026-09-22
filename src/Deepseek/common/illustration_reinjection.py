"""Restore illustration tags dropped during translation, at provably exact positions.

Specification: docs/illustration-reinjection-spec.md

The translator is never instructed to preserve `![illustration](...)` tags, so
carrying them through is emergent behaviour that fails silently and unpredictably
(volume 646941 lost 5 of 10 plates; volume 6e63bc lost 2). This module restores
them before the fidelity gate counts them, turning a blocking failure into a
self-healing one.

Placement is proven, never approximated. A plate dropped a few paragraphs early can
depict a reveal before the prose delivers it, so a chapter is repaired completely or
left untouched -- see `ReinjectionPlan.refusals`.

The alignment runs on block-type signatures (dialogue / narration / heading), which
are language-independent and survive translation. Content matching across JP and EN
is impossible; structural matching is not. Validated blind against the five plates
that survived translation in volume 646941: 5 of 5 positions reproduced exactly,
alignment ratios 0.960-0.994.
"""

from __future__ import annotations

import difflib
import re
from typing import Dict, List, NamedTuple, Optional, Sequence, Set

__all__ = ["Plate", "ReinjectionPlan", "plan_reinjection", "apply_reinjection", "reinject"]

IMG = re.compile(r"^!\[.*\]\(.*\)\s*$")
IMG_TARGET = re.compile(r"\(([^)]*)\)")
HEAD = re.compile(r"^#")
BREAK = re.compile(r"^[─―—\-_]{5,}$")

# Below this the structural alignment is not trustworthy enough to place a plate.
# Observed range on known-good data: 0.960-0.994.
MIN_RATIO = 0.90


class Plate(NamedTuple):
    tag: str          # full markdown tag, preserved byte for byte
    target: str       # referenced file, e.g. "i-011.jpg"
    jp_line: int      # 1-based line in the JP source (a real line, not a block index)
    after_block: int  # 0-based EN block index to insert after; -1 = before the first


class ReinjectionPlan(NamedTuple):
    ratio: float
    plates: List[Plate]
    refusals: List[str]

    @property
    def ok(self) -> bool:
        """True when every missing plate resolved. Partial repair is never allowed."""
        return not self.refusals and bool(self.plates)

    @property
    def nothing_to_do(self) -> bool:
        return not self.plates and not self.refusals


def _blocks(text: str) -> List[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _raw_line_numbers(text: str) -> List[int]:
    """1-based source line number per block index.

    Block index is not a line number -- blank lines are stripped, so the two
    diverge by roughly a factor of two. Refusal reports name a JP line for a human
    to place the plate by hand, so it has to be the real one.
    """
    return [i + 1 for i, ln in enumerate(text.splitlines()) if ln.strip()]


def _sig_jp(line: str) -> str:
    if IMG.match(line):
        return "I"
    if HEAD.match(line):
        return "H"
    return "D" if line[:1] in ("「", "『") else "N"


def _sig_en(line: str) -> str:
    if IMG.match(line):
        return "I"
    if HEAD.match(line):
        return "H"
    if BREAK.match(line):
        return "B"
    stripped = line.lstrip("*_")
    return "D" if stripped[:1] in ('"', "“") else "N"


def plan_reinjection(
    jp_text: str, en_text: str, *, available_assets: Optional[Set[str]] = None
) -> ReinjectionPlan:
    """Compute an exact placement for every JP plate missing from the EN, or refuse."""
    jp, en = _blocks(jp_text), _blocks(en_text)
    jp_lines = _raw_line_numbers(jp_text)

    jp_plates = [(k, ln) for k, ln in enumerate(jp) if IMG.match(ln)]
    if not jp_plates:
        return ReinjectionPlan(1.0, [], [])

    present = {IMG_TARGET.search(ln).group(1) for ln in en if IMG.match(ln)}
    missing = [(k, ln) for k, ln in jp_plates if IMG_TARGET.search(ln).group(1) not in present]
    if not missing:
        return ReinjectionPlan(1.0, [], [])  # idempotent: nothing to do

    # Drop plates from both sides (they are what we are placing) and EN-only scene
    # breaks, which have no JP counterpart and would desynchronise the alignment.
    jp_idx = [k for k, ln in enumerate(jp) if not IMG.match(ln)]
    en_idx = [k for k, ln in enumerate(en) if not IMG.match(ln) and not BREAK.match(ln)]
    a = "".join(_sig_jp(jp[k]) for k in jp_idx)
    b = "".join(_sig_en(en[k]) for k in en_idx)

    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ratio = matcher.ratio()

    refusals: List[str] = []
    if ratio < MIN_RATIO:
        refusals.append(f"alignment ratio {ratio:.3f} below {MIN_RATIO}")

    # Map JP content-position -> EN content-position, from `equal` runs only.
    # Positions inside replace/delete opcodes stay unmapped on purpose: that is
    # where the model merged or split paragraphs, and placement there is a guess.
    index_map: Dict[int, int] = {}
    for op, i1, i2, j1, _j2 in matcher.get_opcodes():
        if op == "equal":
            for d in range(i2 - i1):
                index_map[i1 + d] = j1 + d

    plates: List[Plate] = []
    n_content = len(jp_idx)
    for k, line in missing:
        target = IMG_TARGET.search(line).group(1)
        prev_c = sum(1 for j in jp_idx if j < k) - 1
        next_c = prev_c + 1
        jp_line = jp_lines[k]

        if available_assets is not None and target not in available_assets:
            refusals.append(f"{target}: asset file not found (JP line {jp_line})")
            continue

        # Terminal plates have only one neighbour, and "first block" / "last block"
        # are unambiguous positions -- so they anchor on the side that exists.
        # Requiring both sides here would make a plate at the head or tail of a
        # chapter permanently unplaceable, which is how CHAPTER_11's end-of-chapter
        # plate was silently refused before this branch existed.
        leading = prev_c < 0
        trailing = next_c >= n_content

        if leading and trailing:
            refusals.append(f"{target}: chapter has no content blocks to anchor to (JP line {jp_line})")
            continue
        if leading:
            if next_c not in index_map:
                refusals.append(f"{target}: following neighbour unmapped (JP line {jp_line})")
                continue
            plates.append(Plate(line, target, jp_line, en_idx[index_map[next_c]] - 1))
            continue
        if trailing:
            if prev_c not in index_map:
                refusals.append(f"{target}: preceding neighbour unmapped (JP line {jp_line})")
                continue
            plates.append(Plate(line, target, jp_line, en_idx[index_map[prev_c]]))
            continue

        if prev_c not in index_map or next_c not in index_map:
            refusals.append(f"{target}: neighbour unmapped (JP line {jp_line})")
            continue
        if index_map[next_c] != index_map[prev_c] + 1:
            refusals.append(f"{target}: neighbours non-contiguous in EN (JP line {jp_line})")
            continue

        plates.append(Plate(line, target, jp_line, en_idx[index_map[prev_c]]))

    if len(plates) != len(missing):
        refusals.append(
            f"resolved {len(plates)} of {len(missing)} missing plates -- chapter left untouched"
        )
    return ReinjectionPlan(ratio, plates, refusals)


def apply_reinjection(en_text: str, plates: Sequence[Plate]) -> str:
    """Insert each plate after its resolved EN block. Insert-only; nothing else moves."""
    raw = en_text.splitlines()
    block_to_raw = [i for i, ln in enumerate(raw) if ln.strip()]
    # Descending, so earlier insertions do not shift later targets.
    for plate in sorted(plates, key=lambda p: p.after_block, reverse=True):
        # -1 means "before the first block": a bare index would wrap to the last.
        at = 0 if plate.after_block < 0 else block_to_raw[plate.after_block] + 1
        # Consume the blank run already at the insertion point and re-emit exactly
        # one blank either side, so spacing is identical whether the dropped plate
        # left its surrounding blanks behind or took them with it.
        end = at
        while end < len(raw) and not raw[end].strip():
            end += 1
        raw[at:end] = ["", plate.tag, ""]
    return "\n".join(raw) + "\n"


def reinject(
    jp_text: str, en_text: str, *, available_assets: Optional[Set[str]] = None
) -> tuple[str, ReinjectionPlan]:
    """Plan and, when fully resolved, apply. Returns (text, plan).

    The text is returned unchanged when there is nothing to do OR when the plan
    refused -- the caller decides what to do about a refusal, and the fidelity
    gate still fails loudly on the plates that are genuinely absent.
    """
    plan = plan_reinjection(jp_text, en_text, available_assets=available_assets)
    if not plan.ok:
        return en_text, plan
    return apply_reinjection(en_text, plan.plates), plan
