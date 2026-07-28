#!/usr/bin/env python
"""
DeepSeek_MTLS — minimal CLI.

8 commands, 3 phases plus a prep step and a QC gate. Run from the
DeepSeek_MTLS/ root (or invoke via mtl.bat, which cd's there for you) so the
`src.` import prefix resolves.

    extract <epub_path>      Phase 1: EPUB -> JP chapters (Librarian)
    prep <vol_id>            Unified DeepSeek call: fills context.xml (no Gemini)
    translate <vol_id> [--no-thinking-log] [--dry-run]
                             Phase 2: JP -> EN (DeepSeek V4 Pro). Saves DeepSeek's
                             per-chapter reasoning to work/<vol_id>/THINKING/ by
                             default; --no-thinking-log skips that. --dry-run
                             (developer flag) assembles each chapter's full API
                             payload and writes it to work/<vol_id>/DRY_RUN/ instead
                             of sending it — zero API cost, no EN/ output, no
                             manifest changes.
    qc <vol_id>               Filesystem-only sanity gate (zero API cost)
    build <vol_id>            Phase 4: EN -> EPUB (Builder)
    run <epub_path> [--no-thinking-log]
                              Full pipeline: extract -> prep -> translate -> qc -> build
    list                      List volumes in work/
    status <vol_id>           Pipeline state + chapter completion summary

Bible writing (cross-volume continuity) is intentionally MCP/IDE-agent-only,
not a CLI command — see .github/skills/deepseek-translator/SKILL.md for why.
Everything else the main pipeline's CLI has (phase1.5 through phase1.7,
batch, multimodal, config, schema, ...) is out of scope — those phases were
never copied into this client.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.config import WORK_DIR, ensure_utf8_console

# Every command here prints JP titles/names straight to stdout. On Windows,
# a piped/redirected stdout defaults to the console's ANSI codepage, which
# crashes on the first non-ASCII character — see ensure_utf8_console's
# docstring. Must run before any of this module's own print() calls.
ensure_utf8_console()

# Every phase module logs its own progress via logging.getLogger(__name__)
# (chapter-start/chapter-done, cache-hit ratios, conversation truncation
# warnings, ...) instead of print(). With no handler attached, the root
# logger drops all of it — a multi-minute translate call goes completely
# silent on stdout/stderr even though it's working. stream=stderr keeps it
# out of the way of the plain-text summaries cmd_* functions print on
# stdout, and out of anything that greps this CLI's stdout for output paths.
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

from src.librarian.agent import run_librarian
from src.builder.agent import run_builder
from src.translator.agent import translate_volume
from src.prep.agent import PrepError, run_prep
from src.qc.agent import run_qc


def cmd_extract(args: argparse.Namespace) -> int:
    manifest = run_librarian(
        epub_path=Path(args.epub_path),
        volume_id=args.volume_id,
        source_lang="ja",
        target_lang="en",
    )
    print(f"\nManifest saved to: {manifest.volume_id}/manifest.json")
    return 0


def cmd_prep(args: argparse.Namespace) -> int:
    try:
        receipt = run_prep(args.volume_id, series_id=args.series_id)
    except PrepError as exc:
        print(f"\nPrep failed: {exc}")
        return 1
    print(f"\nPrep complete for {receipt['volume_id']}:")
    print(f"  Chapters:   {receipt['chapter_count']}")
    print(f"  Characters: {receipt['character_count']}")
    print(f"  Series:     {receipt['series_id'] or '(not detected)'}"
          + (" [sequel — bible loaded]" if receipt["is_sequel"] else ""))
    print(f"  Blocks populated: {len(receipt['blocks_populated'])}/15")
    if receipt["blocks_pending"]:
        print(f"  Still pending:    {', '.join(receipt['blocks_pending'])}")
    return 0


def cmd_qc(args: argparse.Namespace) -> int:
    report = run_qc(args.volume_id)
    status = "PASSED" if report["passed"] else "FAILED"
    print(f"\nQC {status} — {args.volume_id}")
    completeness = report["completeness"]
    print(f"  Completeness: {completeness['en_chapter_count']}/{completeness['jp_chapter_count']} chapters")
    for rec in report["recommendations"]:
        print(f"  - {rec}")
    return 0 if report["passed"] else 1


def cmd_translate(args: argparse.Namespace) -> int:
    results = translate_volume(
        args.volume_id,
        chapters=args.chapters,
        thinking_log_enabled=False if args.no_thinking_log else None,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(f"\nDry run — {len(results)} chapter(s), no API calls made:")
        for chapter_id in sorted(results):
            print(f"  {chapter_id} -> work/{args.volume_id}/DRY_RUN/")
        return 0
    print(f"\nTranslated {len(results)} chapter(s):")
    for chapter_id, output_path in sorted(results.items()):
        print(f"  {chapter_id} -> {output_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    result = run_builder(
        volume_id=args.volume_id,
        output_filename=args.output,
    )
    if result.success:
        print(f"\nEPUB built successfully: {result.output_path}")
        return 0
    print(f"\nBuild failed: {result.error}")
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    manifest = run_librarian(
        epub_path=Path(args.epub_path),
        volume_id=args.volume_id,
        source_lang="ja",
        target_lang="en",
    )
    volume_id = manifest.volume_id
    print(f"\n[1/5] Extracted -> {volume_id}/manifest.json")

    try:
        prep_receipt = run_prep(volume_id, series_id=args.series_id)
    except PrepError as exc:
        print(f"[2/5] Prep failed: {exc}")
        return 1
    print(f"[2/5] Prepped — {len(prep_receipt['blocks_populated'])}/15 context.xml blocks populated")

    results = translate_volume(
        volume_id,
        thinking_log_enabled=False if args.no_thinking_log else None,
    )
    print(f"[3/5] Translated {len(results)} chapter(s)")

    qc_report = run_qc(volume_id)
    qc_status = "passed" if qc_report["passed"] else "FAILED (see recommendations below)"
    print(f"[4/5] QC {qc_status}")
    if not qc_report["passed"]:
        for rec in qc_report["recommendations"]:
            print(f"       - {rec}")

    result = run_builder(volume_id=volume_id)
    if not result.success:
        print(f"[5/5] Build failed: {result.error}")
        return 1
    print(f"[5/5] EPUB built successfully: {result.output_path}")
    return 0 if qc_report["passed"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    manifest_path = WORK_DIR / args.volume_id / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json for volume {args.volume_id!r} at {manifest_path}")
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters = manifest.get("chapters", []) or []
    completed = sum(1 for c in chapters if isinstance(c, dict) and c.get("translation_status") == "completed")

    print(f"Volume:    {args.volume_id}")
    print(f"Chapters:  {completed}/{len(chapters)} translated")
    pipeline_state = manifest.get("pipeline_state", {}) or {}
    if pipeline_state:
        print("Pipeline state:")
        for phase, state in sorted(pipeline_state.items()):
            status = state.get("status", "unknown") if isinstance(state, dict) else state
            print(f"  {phase}: {status}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if not WORK_DIR.exists():
        print("No volumes yet — work/ does not exist.")
        return 0
    volumes = sorted(p.name for p in WORK_DIR.iterdir() if p.is_dir())
    if not volumes:
        print("No volumes yet.")
        return 0
    for volume_id in volumes:
        print(volume_id)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtl",
        description="DeepSeek_MTLS — lightweight DeepSeek V4 Pro-exclusive translation client.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_extract = subparsers.add_parser("extract", help="Phase 1: EPUB -> JP chapters")
    p_extract.add_argument("epub_path", type=str, help="Path to source EPUB file")
    p_extract.add_argument("--volume-id", "-v", type=str, default=None, help="Custom volume ID")
    p_extract.set_defaults(func=cmd_extract)

    p_prep = subparsers.add_parser("prep", help="Unified DeepSeek call: fills context.xml")
    p_prep.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_prep.add_argument(
        "--series-id", type=str, default=None,
        help="Explicit series bible to load (default: auto-detect from bibles/ by JP title match)",
    )
    p_prep.set_defaults(func=cmd_prep)

    p_translate = subparsers.add_parser("translate", help="Phase 2: JP -> EN via DeepSeek V4 Pro")
    p_translate.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_translate.add_argument(
        "--chapters", "-c", type=str, nargs="+", default=None,
        help="Specific chapter IDs to translate (default: all pending)",
    )
    p_translate.add_argument(
        "--no-thinking-log", action="store_true",
        help="Skip saving DeepSeek's per-chapter reasoning to work/<vol_id>/THINKING/ (on by default)",
    )
    p_translate.add_argument(
        "--dry-run", action="store_true",
        help="Developer flag: assemble each chapter's full API payload and write it to "
             "work/<vol_id>/DRY_RUN/ instead of sending it. Zero API cost, no EN/ output, "
             "no manifest changes.",
    )
    p_translate.set_defaults(func=cmd_translate)

    p_qc = subparsers.add_parser("qc", help="Filesystem-only sanity gate (zero API cost)")
    p_qc.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_qc.set_defaults(func=cmd_qc)

    p_build = subparsers.add_parser("build", help="Phase 4: EN -> EPUB")
    p_build.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_build.add_argument("--output", "-o", type=str, default=None, help="Output filename")
    p_build.set_defaults(func=cmd_build)

    p_run = subparsers.add_parser("run", help="Full pipeline: extract -> prep -> translate -> qc -> build")
    p_run.add_argument("epub_path", type=str, help="Path to source EPUB file")
    p_run.add_argument("--volume-id", "-v", type=str, default=None, help="Custom volume ID")
    p_run.add_argument("--series-id", type=str, default=None, help="Explicit series bible to load for prep")
    p_run.add_argument(
        "--no-thinking-log", action="store_true",
        help="Skip saving DeepSeek's per-chapter reasoning to work/<vol_id>/THINKING/ (on by default)",
    )
    p_run.set_defaults(func=cmd_run)

    p_list = subparsers.add_parser("list", help="List volumes in work/")
    p_list.set_defaults(func=cmd_list)

    p_status = subparsers.add_parser("status", help="Pipeline state + chapter completion summary")
    p_status.add_argument("volume_id", type=str, help="Volume ID (directory name in work/)")
    p_status.set_defaults(func=cmd_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
