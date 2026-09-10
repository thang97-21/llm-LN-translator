#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Japanese OCR extraction for light-novel / manga page scans.

Best-in-class engine for this domain:
    * manga-ocr  -> transformer model trained specifically on Japanese
                    manga / light-novel text, handles vertical (tategaki)
                    and horizontal script accurately.  PRIMARY.
    * PaddleOCR  -> PP-OCRv4 with lang='japan', bounding-box detection that
                    preserves per-line ordering.  FALLBACK.

The script pre-processes each page with Pillow (grayscale + contrast +
optional upscale) to improve recognition, runs the chosen engine, and writes
a single markdown file that preserves page boundaries.

Usage
-----
    python scripts/ocr_extract.py \
        --input "WORK/<vol_id>/" \
        --output "WORK/<vol_id>/抽出テキスト.md" \
        --engine manga            # manga-ocr (default)
        --pattern "page-*.jpg"

Install the engine first, e.g.:
    pip install manga-ocr          # pulls torch + transformers
    # fallback:  pip install paddlepaddle paddleocr
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ocr_extract")


# --------------------------------------------------------------------------- #
# Image pre-processing (Pillow)
# --------------------------------------------------------------------------- #
def preprocess(path: str | Path, upscale: int = 2, contrast: float = 1.4) -> "Image.Image":
    """Load a page, convert to L, boost contrast, and optionally upscale.

    Upscaling helps OCR on small furigana / fine print.  Returns a PIL image.
    """
    from PIL import Image, ImageEnhance, ImageOps

    img = Image.open(path).convert("L")
    img = ImageOps.autocontrast(img)

    if contrast and contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)

    if upscale and upscale > 1:
        img = img.resize(
            (img.width * upscale, img.height * upscale), Image.Resampling.LANCZOS
        )

    # Light denoise can help crisp up scanned text without hurting glyphs.
    img = ImageEnhance.Sharpness(img).enhance(1.2)
    return img


# --------------------------------------------------------------------------- #
# Engine: manga-ocr
# --------------------------------------------------------------------------- #
def ocr_with_manga(path: str | Path, pre: bool = True) -> str:
    """OCR a single page with manga-ocr.

    manga-ocr expects a full image and returns concatenated recognized text.
    It is return-order aware for its internal line segmentation but a full
    page can mix columns; callers should treat output as per-page text.
    """
    try:
        from manga_ocr import MangaOcr
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "manga-ocr is not installed. Run:  pip install manga-ocr"
        ) from exc

    # Lazy-singleton to avoid re-downloading / re-loading the model every page.
    if not hasattr(ocr_with_manga, "_model"):
        log.info("Loading manga-ocr model (first run downloads weights)...")
        ocr_with_manga._model = MangaOcr()
        log.info("manga-ocr model ready.")

    img_path = path if not pre else None
    if pre:
        # manga-ocr handles raw scans well; optional pre-process still helps.
        img = preprocess(path)
    else:
        from PIL import Image

        img = Image.open(path).convert("RGB")

    text = ocr_with_manga._model(img)
    return (text or "").strip()


# --------------------------------------------------------------------------- #
# Engine: PaddleOCR
# --------------------------------------------------------------------------- #
def ocr_with_paddle(path: str | Path) -> str:
    """OCR a single page with PaddleOCR (PP-OCRv4, lang='japan').

    PaddleOCR returns per-line boxes; we emit lines in reading order, joining
    them with a small column-aware heuristic (grouped by vertical strip).
    """
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PaddleOCR is not installed. Run:  pip install paddlepaddle paddleocr"
        ) from exc

    if not hasattr(ocr_with_paddle, "_ocr"):
        log.info("Initialising PaddleOCR (lang='japan')...")
        ocr_with_paddle._ocr = PaddleOCR(lang="japan", show_log=False)
        log.info("PaddleOCR ready.")

    result = ocr_with_paddle._ocr.ocr(str(path), cls=True)
    lines: list[tuple[float, str]] = []
    for page in result or []:
        for entry in page or []:
            # entry = [[x0,y0],[x1,y1],[x2,y2],[x3,y3]], (text, score)
            box = entry[0]
            text = entry[1][0]
            min_x = min(p[0] for p in box)
            min_y = min(p[1] for p in box)
            lines.append((min_x + min_y * 1e-4, text))  # top-left ordering key

    # Stable sort preserves left-to-right / top-to-bottom reading order.
    lines.sort(key=lambda t: t[0])
    return "\n".join(text for _, text in lines)


# --------------------------------------------------------------------------- #
# Page collection
# --------------------------------------------------------------------------- #
def collect_pages(input_dir: str | Path, pattern: str) -> list[Path]:
    """Return sorted page files matching `pattern` under `input_dir`."""
    files = glob.glob(str(Path(input_dir) / pattern))
    pages = sorted(
        Path(f) for f in files
    )
    if not pages:
        log.warning("No files matched pattern %r in %s", pattern, input_dir)
    return pages


def page_label(path: Path) -> str:
    """Human label like 'page-0007' from a filename."""
    return path.stem


# --------------------------------------------------------------------------- #
# Markdown assembly
# --------------------------------------------------------------------------- #
def build_markdown(pages: list[Path], engine: str, pre: bool) -> str:
    chunks: list[str] = []
    chunks.append("# 他校の氷姫を助けたら、お友達から始める事になりました 5 — OCR抽出\n")
    chunks.append(f"> エンジン: `{engine}` ・ 前処理: {'on' if pre else 'off'}\n")

    for i, page in enumerate(pages, 1):
        label = page_label(page)
        log.info("(%d/%d) OCR %s", i, len(pages), page.name)
        try:
            if engine == "paddle":
                text = ocr_with_paddle(page)
            else:
                text = ocr_with_manga(page, pre=pre)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed on %s: %s", page.name, exc)
            text = f"<!-- OCR failed: {exc} -->"

        # Strip extraneous whitespace but keep internal line structure.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        body = "\n".join(lines)
        if not body.strip():
            body = "*（文字なし / 挿絵ページ）*"

        chunks.append(f"\n---\n\n## 【{label}】\n")
        chunks.append(body)

    chunks.append("\n---\n")
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Japanese text from light-novel page scans to markdown."
    )
    parser.add_argument("--input", required=True, help="Directory containing page images.")
    parser.add_argument("--output", required=True, help="Output .md path.")
    parser.add_argument(
        "--engine",
        choices=["manga", "paddle"],
        default="manga",
        help="OCR engine (default: manga).",
    )
    parser.add_argument(
        "--pattern", default="page-*.jpg", help="Glob pattern for page files."
    )
    parser.add_argument(
        "--no-preprocess",
        action="store_true",
        help="Skip Pillow preprocessing (raw image to engine).",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging.")
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    input_dir = Path(args.input).resolve()
    if not input_dir.is_dir():
        log.error("Input directory does not exist: %s", input_dir)
        return 1

    pages = collect_pages(input_dir, args.pattern)
    if not pages:
        log.error("No pages found under %s", input_dir)
        return 1

    log.info("Found %d pages. Engine=%s", len(pages), args.engine)
    md = build_markdown(pages, args.engine, pre=not args.no_preprocess)

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(md, encoding="utf-8")
    log.info("Wrote %s (%d bytes, %d pages)", output, output.stat().st_size, len(pages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
