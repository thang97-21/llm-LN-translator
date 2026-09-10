# -*- coding: utf-8 -*-
"""Quick single-page test of RapidOCR (Japanese) to assess quality."""
import glob
import os

from rapidocr import RapidOCR

work = r"C:/Users/Minh Thang/Documents/llm-LN-translator/work"
vol_dir = next(
    (p for p in glob.glob(os.path.join(work, "*d72740*")) if os.path.isdir(p)), None
)
assert vol_dir, "volume dir not found"

# Pick a body-text page (page-0006) and a cover page.
for name in ["page-0006.jpg", "page-0005.jpg"]:
    img = os.path.join(vol_dir, name)
    print("=" * 60)
    print("FILE:", name)
    ocr = RapidOCR(lang="japan")
    result = ocr(img)
    # RapidOCR 3.x returns a result object; normalize to lines.
    if hasattr(result, "txts"):
        lines = list(result.txts)
    elif isinstance(result, (list, tuple)):
        lines = []
        for entry in result:
            if entry is None:
                continue
            if isinstance(entry, dict):
                lines.append(entry.get("text") or entry.get("txts", ""))
            else:
                # [box, text, score]
                lines.append(entry[1] if len(entry) > 1 else entry)
    else:
        lines = [str(result)]
    for ln in lines:
        if ln:
            print("  ", ln)
    print("ENGINE_CLEANUP: delete engine")
    del ocr
