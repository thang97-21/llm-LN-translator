"""CLI for illustration re-injection over an already-translated volume.

The logic lives in src/Deepseek/common/illustration_reinjection.py, which the
Anthropic route also calls inline before its fidelity gate. This wrapper exists for
repairing volumes that were translated before that wiring landed, and for dry-run
inspection.

Usage:
    python scripts/reinject_illustrations.py <volume_dir>            # dry run
    python scripts/reinject_illustrations.py <volume_dir> --apply    # write
    python scripts/reinject_illustrations.py <volume_dir> --chapter CHAPTER_02
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.Deepseek.common.illustration_reinjection import (  # noqa: E402
    apply_reinjection,
    plan_reinjection,
)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("volume", type=Path, help="WORK/<volume> directory")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--chapter", help="limit to one chapter id, e.g. CHAPTER_02")
    args = ap.parse_args(argv)

    jp_dir, en_dir = args.volume / "JP", args.volume / "EN"
    assets_dir = args.volume / "assets" / "illustrations"
    if not jp_dir.is_dir() or not en_dir.is_dir():
        print(f"error: {args.volume} has no JP/ and EN/ directories", file=sys.stderr)
        return 2

    assets = {p.name for p in assets_dir.iterdir()} if assets_dir.is_dir() else None

    repaired = blocked = 0
    for jp_path in sorted(jp_dir.glob("CHAPTER_*.md")):
        if args.chapter and jp_path.stem != args.chapter:
            continue
        en_path = en_dir / f"{jp_path.stem}_EN.md"
        if not en_path.exists():
            continue

        jp_text = jp_path.read_text(encoding="utf-8")
        en_text = en_path.read_text(encoding="utf-8")
        plan = plan_reinjection(jp_text, en_text, available_assets=assets)
        if plan.nothing_to_do:
            continue

        print(f"\n{jp_path.stem}  (alignment ratio {plan.ratio:.3f})")
        for plate in plan.plates:
            print(
                f"  {plate.target:<12} JP line {plate.jp_line:>5}"
                f"  -> after EN block {plate.after_block + 1}"
            )
        for reason in plan.refusals:
            print(f"  REFUSED: {reason}")

        if plan.ok:
            if args.apply:
                en_path.write_text(apply_reinjection(en_text, plan.plates), encoding="utf-8")
                print(f"  applied {len(plan.plates)} plate(s) to {en_path.name}")
            repaired += 1
        else:
            blocked += 1

    if not args.apply:
        print("\n[dry run] re-run with --apply to write changes")
    print(f"chapters repairable: {repaired} | refused: {blocked}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
