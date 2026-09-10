# -*- coding: utf-8 -*-
"""Test rotation (tategaki -> horizontal) + manga-ocr on sample pages.
Writes results to a UTF-8 text file to avoid console encoding mangling.
"""
import glob
import os

from PIL import Image

from manga_ocr import MangaOcr

work = r"C:/Users/Minh Thang/Documents/llm-LN-translator/work"
vol_dir = next(
    (p for p in glob.glob(os.path.join(work, "*d72740*")) if os.path.isdir(p)), None
)
assert vol_dir, "volume dir not found"

out_path = os.path.join(vol_dir, "ocr_rotate_sample.txt")
mocr = MangaOcr()

with open(out_path, "w", encoding="utf-8") as fh:
    for name in ["page-0005.jpg", "page-0006.jpg", "page-0010.jpg"]:
        img = os.path.join(vol_dir, name)
        pil = Image.open(img).convert("RGB")
        # Rotate 90 deg clockwise -> vertical columns become horizontal lines.
        rot = pil.rotate(-90, expand=True)
        text = mocr(rot)
        fh.write("=" * 60 + "\n")
        fh.write("FILE: " + name + "\n")
        fh.write(text + "\n\n")

print("WROTE", out_path)
