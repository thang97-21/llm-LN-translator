# -*- coding: utf-8 -*-
"""Test rotation (tategaki -> horizontal) + EasyOCR on sample pages.
Writes results to a UTF-8 text file to avoid console encoding mangling.
"""
import glob
import os

import numpy as np
from PIL import Image

import easyocr

work = r"C:/Users/Minh Thang/Documents/llm-LN-translator/work"
vol_dir = next(
    (p for p in glob.glob(os.path.join(work, "*d72740*")) if os.path.isdir(p)), None
)
assert vol_dir, "volume dir not found"

out_path = os.path.join(vol_dir, "ocr_rotate_easy_sample.txt")
reader = easyocr.Reader(["ja"], gpu=False, verbose=False)

with open(out_path, "w", encoding="utf-8") as fh:
    for name in ["page-0005.jpg", "page-0006.jpg", "page-0010.jpg"]:
        img = os.path.join(vol_dir, name)
        pil = Image.open(img).convert("RGB")
        rot = pil.rotate(-90, expand=True)  # vertical -> horizontal
        arr = np.array(rot)[:, :, ::-1]
        res = reader.readtext(arr, detail=1, paragraph=False)
        fh.write("=" * 60 + "\n")
        fh.write("FILE: " + name + "\n")
        for box, text, conf in res:
            fh.write(f"  {text}\n")
        fh.write("\n")

print("WROTE", out_path)
