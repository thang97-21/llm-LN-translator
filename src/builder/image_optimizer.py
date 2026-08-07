"""
Per-profile image emission for the EPUB builder.

Replaces the straight shutil.copy2 the builder used to do for every asset. What
happens to a given image is decided entirely by its DeviceProfile:

  - passthrough copies, always
  - standard fits to 1200x1800 and forces baseline JPEG
  - the XTEINK profiles additionally convert to 8-bit grayscale and fit to the
    panel exactly

Three decisions here are worth knowing about before someone "simplifies" them:

1. progressive=False on every JPEG save. Progressive JPEG does not decode on
   CrossPoint at all -- the image simply does not appear. Pillow carries the
   source's progressive flag through a re-save unless told otherwise, so this
   cannot be left to the default.

2. Grayscale means 8-bit L, never 1-bit. The device dithers to bilevel itself
   with a waveform matched to its panel. Handing it something already dithered
   means dithering dithered input, which is how you get moire on a page that
   would otherwise have looked fine.

3. An image that is already grayscale, already baseline and already inside the
   box is COPIED, not re-encoded. Running a clean q85 JPEG through q72 buys a
   few kilobytes and pays for them in generational artefacts, on a panel that
   is going to throw most of the tonal range away regardless.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image

from .device_profiles import DeviceProfile, Levels


# Extensions the CrossPoint firmware has decoders for.
DEVICE_DECODABLE = frozenset({".jpg", ".jpeg", ".png"})

JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})

# What a transcoded image becomes.
TRANSCODE_TARGET = ".jpg"


@dataclass
class EmitResult:
    """What actually happened to one image."""

    source_name: str
    emitted_name: str
    source_bytes: int
    emitted_bytes: int
    width: int
    height: int
    action: str  # "copied" | "optimized" | "transcoded"
    undersized: bool = False

    @property
    def renamed(self) -> bool:
        return self.emitted_name != self.source_name

    @property
    def saved_bytes(self) -> int:
        return max(self.source_bytes - self.emitted_bytes, 0)


def _levels_lut(levels: Levels) -> List[int]:
    """
    Build a 256-entry LUT for black point / white point / midtone, matching the
    external web optimizer's grayscaleSettings semantics.

    Midtone is expressed as the input value that should land on 50% output, so
    it becomes a gamma exponent rather than a straight offset.
    """
    black = min(max(levels.black_point, 0), 255)
    white = min(max(levels.white_point, 0), 255)
    if white <= black:
        white = min(black + 1, 255)

    span = float(white - black)
    mid = min(max(levels.midtone, black + 1), white - 1) if white - black > 1 else black + 1
    ratio = (mid - black) / span

    if 0.0 < ratio < 1.0:
        gamma = math.log(0.5) / math.log(ratio)
    else:
        gamma = 1.0

    lut: List[int] = []
    for value in range(256):
        scaled = (value - black) / span
        scaled = min(max(scaled, 0.0), 1.0)
        if gamma != 1.0:
            scaled = scaled ** gamma
        lut.append(int(round(scaled * 255)))
    return lut


def _flatten_alpha(image: Image.Image) -> Image.Image:
    """
    Composite transparency onto white.

    Without this, an RGBA or LA source converted straight to L turns every
    transparent pixel black, which on a page of white paper is spectacular and
    wrong. Palette images get promoted first because P can carry alpha too.
    """
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")

    if image.mode in ("RGBA", "LA"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        alpha = image.getchannel("A")
        background.paste(image.convert("RGB"), mask=alpha)
        return background

    return image


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    """
    Avoid clobbering when a transcode collides with an existing name -- e.g. a
    volume carrying both art.gif and art.jpg, where the first becomes the
    second. Rare, but silent overwrites are not an acceptable failure mode.
    """
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    for index in range(1, 100):
        candidate = dest_dir / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate

    raise FileExistsError(f"Could not find a free filename for {filename} in {dest_dir}")


def optimize_image_to(
    src: Path,
    dest_dir: Path,
    profile: DeviceProfile,
    is_cover: bool = False,
) -> EmitResult:
    """
    Emit one image into dest_dir according to the profile.

    Args:
        src: Source image path.
        dest_dir: Destination directory (created if missing).
        profile: Target device profile.
        is_cover: Cover images get their own box, which may differ.

    Returns:
        EmitResult. Check .renamed before assuming the filename survived --
        transcoded images do not, and the OPF manifest and every <img src>
        need to follow.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    source_bytes = src.stat().st_size
    ext = src.suffix.lower()
    box = profile.box_for(is_cover)
    needs_transcode = ext in profile.transcode_formats

    def _copy(reason_width: int = 0, reason_height: int = 0) -> EmitResult:
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        return EmitResult(
            source_name=src.name,
            emitted_name=dest.name,
            source_bytes=source_bytes,
            emitted_bytes=dest.stat().st_size,
            width=reason_width,
            height=reason_height,
            action="copied",
        )

    # Passthrough, or a profile with nothing to say about images.
    if box is None and not needs_transcode and not profile.grayscale:
        return _copy()

    try:
        with Image.open(src) as opened:
            opened.load()
            image = opened
            width, height = image.size
            is_jpeg = ext in JPEG_EXTENSIONS
            progressive = bool(image.info.get("progressive"))
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )

            oversized = bool(box and (width > box[0] or height > box[1]))
            wrong_mode = profile.grayscale and image.mode != "L"
            bad_encoding = profile.force_baseline_jpeg and is_jpeg and progressive
            needs_levels = profile.grayscale and not profile.levels.is_identity

            needs_work = (
                needs_transcode
                or oversized
                or wrong_mode
                or bad_encoding
                or needs_levels
                or has_alpha
            )

            if not needs_work:
                # Already in the shape the device wants. Leave it alone rather
                # than paying a recompression generation for nothing.
                return _copy(width, height)

            if has_alpha:
                image = _flatten_alpha(image)

            if profile.grayscale:
                if image.mode != "L":
                    image = image.convert("L")
                if not profile.levels.is_identity:
                    image = image.point(_levels_lut(profile.levels))

            target_ext = TRANSCODE_TARGET if needs_transcode else ext
            if target_ext == ".png" and profile.grayscale:
                # Grayscale PNG stays PNG: gaiji glyphs and line art are small
                # and lossless here, and JPEG would only add ringing.
                pass
            elif target_ext not in DEVICE_DECODABLE:
                target_ext = TRANSCODE_TARGET

            if target_ext in JPEG_EXTENSIONS and image.mode not in ("L", "RGB"):
                image = image.convert("RGB")

            if box:
                image.thumbnail(box, Image.Resampling.LANCZOS)

            final_width, final_height = image.size
            # The engine caps scale at 1.0, so a source smaller than the panel
            # renders as a stamp. We do not upscale -- inventing detail for a
            # 1-bit dither is pointless -- but the caller should know.
            undersized = bool(
                box and final_width < box[0] and final_height < box[1]
            )

            dest = _unique_destination(dest_dir, src.stem + target_ext)

            if target_ext in JPEG_EXTENSIONS:
                image.save(
                    dest,
                    format="JPEG",
                    quality=profile.jpeg_quality,
                    optimize=True,
                    progressive=False,  # see module docstring
                )
            elif target_ext == ".png":
                image.save(dest, format="PNG", optimize=True)
            else:
                image.save(dest)

    except (OSError, ValueError) as exc:
        # A source we cannot decode is not worth failing a whole build over.
        # Copy it through untouched and let the budget report flag it.
        print(f"     [WARN] Could not optimize {src.name} ({exc}); copied unchanged")
        return _copy()

    return EmitResult(
        source_name=src.name,
        emitted_name=dest.name,
        source_bytes=source_bytes,
        emitted_bytes=dest.stat().st_size,
        width=final_width,
        height=final_height,
        action="transcoded" if needs_transcode else "optimized",
        undersized=undersized,
    )


def summarize(results: List[EmitResult]) -> str:
    """One line for the build log: how much this profile actually bought."""
    if not results:
        return "Images: none"

    before = sum(r.source_bytes for r in results)
    after = sum(r.emitted_bytes for r in results)
    changed = sum(1 for r in results if r.action != "copied")
    renamed = sum(1 for r in results if r.renamed)

    line = (
        f"Images: {len(results)} files, {before / 1024 / 1024:.1f} MB -> "
        f"{after / 1024 / 1024:.1f} MB ({changed} re-encoded"
    )
    if renamed:
        line += f", {renamed} renamed"
    return line + ")"
