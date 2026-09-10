# -*- coding: utf-8 -*-
"""Quick single-page test of EasyOCR (Japanese) to assess quality.
Loads images via Pillow (handles non-ASCII paths) and passes numpy arrays
to avoid OpenCV's imread limitation on Windows.
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

reader = easyocr.Reader(["ja"], gpu=False, verbose=False)

for name in ["page-0005.jpg", "page-0006.jpg"]:
    img = os.path.join(vol_dir, name)
    pil = Image.open(img).convert("RGB")
    arr = np.array(pil)[:, :, ::-1]  # RGB -> BGR
    print("=" * 60)
    print("FILE:", name)
    res = reader.readtext(arr, detail=0, paragraph=False)
    for ln in res:
        if ln:
            print("  ", ln)
