"""
Device profiles - target-hardware presets for EPUB output.

A profile is everything the builder needs to know about the machine that will
render the book: how large its panel is, whether it can show colour, and how
much of our CSS it will actually read before discarding the rest.

The XTEINK profiles exist because CrossPoint firmware is unusually candid about
its limits, and they are severe:

  - 528x792 (X3) / 480x800 (X4), 1-bit e-ink, ESP32-C3 with ~380 KB usable RAM
  - images are decoded to 1-bit BMP on-device and cached; only JPEG and PNG
    have decoders
  - image layout is scale = min(maxW/w, maxH/h, 1.0). Note the cap: the engine
    NEVER upscales, so an image smaller than the panel renders as a stamp in a
    field of white. Undersizing is a visible defect, not a saving.
  - the CSS engine implements nine properties and ignores the rest entirely
    (see stylesheets.py, which is where that argument is made at length)

Anything here that reads as paranoid is load-bearing.

This module is the single source of panel dimensions. The XTC export path reads
the same registry, so the two can never disagree about how big a screen is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple


# Stylesheet keys. The actual CSS strings are resolved in agent.py, which owns
# DEFAULT_CSS; naming them here instead of importing avoids a circular import.
STYLESHEET_DEFAULT = "default"
STYLESHEET_EINK = "eink"


@dataclass(frozen=True)
class Levels:
    """
    Grayscale level mapping, mirroring the external web optimizer's
    grayscaleSettings so our profiles and its exported JSON stay legible to
    each other. The default is identity and costs nothing - the LUT is skipped
    entirely rather than applied as a no-op.
    """

    black_point: int = 0
    white_point: int = 255
    midtone: int = 128

    @property
    def is_identity(self) -> bool:
        return (self.black_point, self.white_point, self.midtone) == (0, 255, 128)


@dataclass(frozen=True)
class Budgets:
    """
    Build-time warning thresholds derived from documented firmware limits.
    These warn; they never fail a build. A noisy log beats a book that opens
    slowly for reasons nobody wrote down.
    """

    # A 500 KB chapter costs 5-10s on first load, and that layout is redone
    # whenever the reader changes font, spacing, margins or alignment.
    max_chapter_bytes: int = 250_000

    # The firmware caps anchor IDs at 1024 per chapter. Past that, footnote and
    # TOC navigation breaks silently, which is the worst way for it to break.
    # Our footnote markup emits two anchors per note, so leave real headroom.
    max_anchors_per_chapter: int = 900

    # <table> is replaced with the literal text "[Table omitted]".
    warn_on_tables: bool = True


@dataclass(frozen=True)
class DeviceProfile:
    """One target device (or device class) and how to build for it."""

    name: str
    label: str

    # Physical panel, portrait. None for profiles that aren't device-specific.
    screen: Optional[Tuple[int, int]] = None

    # Box that inline art and covers are fitted into. None means "leave images
    # exactly as they are", which is what passthrough does.
    image_box: Optional[Tuple[int, int]] = None
    cover_box: Optional[Tuple[int, int]] = None

    jpeg_quality: int = 85
    grayscale: bool = False

    # Progressive JPEG does not decode on these devices at all. Pillow carries
    # the source's progressive flag through a re-save unless told otherwise,
    # so this has to be explicit rather than assumed.
    force_baseline_jpeg: bool = False

    # Extensions with no on-device decoder, re-encoded to JPEG.
    transcode_formats: FrozenSet[str] = frozenset()

    unwrap_svg_images: bool = False
    flatten_tables: bool = False
    embed_fonts: bool = False

    stylesheet: str = STYLESHEET_DEFAULT
    levels: Levels = Levels()
    budgets: Optional[Budgets] = None

    @property
    def rewrites_images(self) -> bool:
        """True when the profile re-encodes images rather than copying them."""
        return self.image_box is not None

    @property
    def is_eink(self) -> bool:
        return self.stylesheet == STYLESHEET_EINK

    def box_for(self, is_cover: bool) -> Optional[Tuple[int, int]]:
        return self.cover_box if is_cover else self.image_box


# Formats the CrossPoint firmware has no decoder for. SVG gained handling in
# firmware 1.5.0, but we unwrap it anyway - the wrapper costs parse time and
# buys nothing that a plain <img> doesn't.
_NO_DECODER = frozenset({".gif", ".webp"})


PROFILES: Dict[str, DeviceProfile] = {
    # Byte-for-byte today's behaviour. Exists so the refactor is provable, not
    # because anyone should ship with it.
    "passthrough": DeviceProfile(
        name="passthrough",
        label="Passthrough (no image or CSS changes)",
        stylesheet=STYLESHEET_DEFAULT,
    ),

    # 300 PPI readers - Kindle, Kobo, tablets. Finally implements the values
    # config.yaml has been declaring to nobody.
    "standard": DeviceProfile(
        name="standard",
        label="Standard 300 PPI reader",
        image_box=(1200, 1800),
        cover_box=(1600, 2400),
        jpeg_quality=85,
        force_baseline_jpeg=True,
        transcode_formats=_NO_DECODER,
        stylesheet=STYLESHEET_DEFAULT,
    ),

    "xteink-x3": DeviceProfile(
        name="xteink-x3",
        label="XTEINK X3 (528x792, 259 PPI, CrossPoint)",
        screen=(528, 792),
        image_box=(528, 792),
        cover_box=(528, 792),
        jpeg_quality=72,
        # 8-bit grayscale, never pre-dithered to 1-bit: the firmware dithers
        # with a waveform matched to its own panel, and dithering already
        # dithered input is how you get moire.
        grayscale=True,
        force_baseline_jpeg=True,
        transcode_formats=_NO_DECODER,
        unwrap_svg_images=True,
        flatten_tables=True,
        embed_fonts=False,
        stylesheet=STYLESHEET_EINK,
        budgets=Budgets(),
    ),

    "xteink-x4": DeviceProfile(
        name="xteink-x4",
        label="XTEINK X4 (480x800, 219 PPI, CrossPoint)",
        screen=(480, 800),
        image_box=(480, 800),
        cover_box=(480, 800),
        jpeg_quality=72,
        grayscale=True,
        force_baseline_jpeg=True,
        transcode_formats=_NO_DECODER,
        unwrap_svg_images=True,
        flatten_tables=True,
        embed_fonts=False,
        stylesheet=STYLESHEET_EINK,
        budgets=Budgets(),
    ),
}


DEFAULT_PROFILE_NAME = "standard"

PROFILE_NAMES = tuple(PROFILES)


class UnknownProfileError(ValueError):
    """Raised for a profile name that isn't in the registry."""


def resolve_profile(name: Optional[str] = None) -> DeviceProfile:
    """
    Resolve a profile by name, falling back to the configured default.

    Args:
        name: Profile name, or None/empty to use builder.device_profile from
              config.yaml (which itself defaults to 'standard').

    Returns:
        The matching DeviceProfile.

    Raises:
        UnknownProfileError: naming the valid options, because a typo here
            should cost one glance and not a bisect.
    """
    if not name:
        # Imported late: config.py reads config.yaml on call, and this module
        # is imported by tooling that has no business paying for that.
        from .config import get_device_profile_name

        name = get_device_profile_name()

    key = str(name).strip().lower()
    profile = PROFILES.get(key)
    if profile is None:
        raise UnknownProfileError(
            f"Unknown device profile {name!r}. Valid profiles: "
            f"{', '.join(PROFILE_NAMES)}"
        )
    return profile


def describe_profile(profile: DeviceProfile) -> str:
    """One-line summary for the build header."""
    if not profile.rewrites_images:
        return f"{profile.label} - images copied unchanged"

    box = profile.image_box
    colour = "grayscale" if profile.grayscale else "colour"
    return (
        f"{profile.label} - images fitted to {box[0]}x{box[1]}, "
        f"{colour}, JPEG q{profile.jpeg_quality}"
    )
