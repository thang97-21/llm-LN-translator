"""
Build-time budget checks against documented CrossPoint firmware limits.

Every check here warns. None of them fail a build. The point is that when a
book opens slowly or a footnote link goes nowhere on the device, the reason was
printed at build time instead of being discovered by holding the hardware and
guessing.

The limits are not ours; they are the firmware's:

  - a 500 KB HTML chapter costs 5-10s on first load, and that layout is redone
    from scratch whenever the reader changes font, line spacing, margins or
    alignment, because all four key the layout cache
  - anchor IDs are capped at 1024 per chapter; past the cap they are dropped,
    and footnote and TOC navigation breaks without saying so
  - <table> is replaced with the literal string "[Table omitted]"
  - image layout is scale = min(maxW/w, maxH/h, 1.0), so an image smaller than
    the panel renders as a stamp surrounded by white
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Sequence

from .device_profiles import Budgets, DeviceProfile
from .image_optimizer import EmitResult
from .stylesheets import EINK_SUPPORTED_PROPERTIES

# id="..." on any element becomes an anchor the firmware has to track.
_ANCHOR_RE = re.compile(r'\bid\s*=\s*"', re.IGNORECASE)
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)

# Selectors and rule bodies, for the stylesheet audit. Deliberately crude --
# this is a smoke alarm, not a CSS parser. Comments and at-statements are
# stripped first, because otherwise a comment explaining that we avoid
# p[class="..."] gets read as a selector using it, which is a very silly way
# for a linter to fail.
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.MULTILINE)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_AT_STATEMENT_RE = re.compile(r"@[a-zA-Z-]+[^;{}]*;")

# Selector constructs CrossPoint cannot match. Its resolution order is
# inline style -> class -> tag -> default, which is a priority list and not a
# cascade, so anything structural is dead weight at best.
_UNSUPPORTED_SELECTOR_TOKENS = {
    "[": "attribute selector",
    ">": "child combinator",
    "~": "sibling combinator",
    "+": "adjacent sibling combinator",
    ":": "pseudo-class or pseudo-element",
}


def audit_chapters(text_dir: Path, budgets: Budgets) -> List[str]:
    """Check every generated XHTML file against the per-chapter limits."""
    warnings: List[str] = []

    if not text_dir.exists():
        return warnings

    for xhtml in sorted(text_dir.glob("*.xhtml")):
        try:
            content = xhtml.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"{xhtml.name}: unreadable ({exc})")
            continue

        size = len(content.encode("utf-8"))
        if size > budgets.max_chapter_bytes:
            warnings.append(
                f"{xhtml.name}: {size / 1024:.0f} KB exceeds the "
                f"{budgets.max_chapter_bytes / 1024:.0f} KB chapter budget - "
                f"expect a slow first open, re-paid on every reader setting change"
            )

        anchors = len(_ANCHOR_RE.findall(content))
        if anchors > budgets.max_anchors_per_chapter:
            warnings.append(
                f"{xhtml.name}: {anchors} anchors, over the "
                f"{budgets.max_anchors_per_chapter} budget - the firmware caps "
                f"at 1024 and drops the rest, breaking footnote and TOC links "
                f"silently"
            )

        if budgets.warn_on_tables and _TABLE_RE.search(content):
            warnings.append(
                f"{xhtml.name}: contains <table>, which renders on-device as "
                f"the literal text '[Table omitted]'"
            )

    return warnings


def audit_stylesheet(css: str) -> List[str]:
    """
    Flag anything in a stylesheet the CrossPoint CSS engine will discard.

    Not a validator. It exists so that the next person who adds a tidy little
    `page-break-before` to the e-ink sheet finds out at build time that the
    device has never once honoured one.
    """
    warnings: List[str] = []

    # Strip comments and at-statements (@charset, @import) before anything
    # else, so prose about CSS is never mistaken for CSS.
    stripped = _AT_STATEMENT_RE.sub("", _COMMENT_RE.sub("", css))

    if "@media" in stripped:
        warnings.append("stylesheet: @media block present - never evaluated on-device")
    if "!important" in stripped:
        warnings.append(
            "stylesheet: !important present - there is no cascade here to override"
        )

    unsupported_props = set()
    unsupported_selectors = set()

    for selector_blob, body in _RULE_RE.findall(stripped):
        for selector in selector_blob.split(","):
            selector = " ".join(selector.split())
            if not selector or selector.startswith("@"):
                continue
            for token, description in _UNSUPPORTED_SELECTOR_TOKENS.items():
                if token in selector:
                    unsupported_selectors.add(f"{selector} ({description})")
            # A space inside a normalized selector means a descendant combinator.
            if " " in selector:
                unsupported_selectors.add(f"{selector} (descendant combinator)")

        # Split rather than regex: a trailing declaration may omit its
        # semicolon, and values can contain colons (url(), data URIs).
        for declaration in body.split(";"):
            declaration = declaration.strip()
            if not declaration or ":" not in declaration:
                continue
            prop = declaration.split(":", 1)[0].strip().lower()
            if prop and prop not in EINK_SUPPORTED_PROPERTIES:
                unsupported_props.add(prop)

    if unsupported_props:
        warnings.append(
            "stylesheet: properties the engine ignores: "
            + ", ".join(sorted(unsupported_props))
        )
    if unsupported_selectors:
        warnings.append(
            "stylesheet: selectors that can never match: "
            + "; ".join(sorted(unsupported_selectors))
        )

    return warnings


def audit_images(results: Sequence[EmitResult], profile: DeviceProfile) -> List[str]:
    """Report images that will render smaller than the panel."""
    warnings: List[str] = []

    if not profile.image_box:
        return warnings

    undersized = [r for r in results if r.undersized]
    if undersized:
        box = profile.image_box
        sample = ", ".join(r.emitted_name for r in undersized[:5])
        more = f" (+{len(undersized) - 5} more)" if len(undersized) > 5 else ""
        warnings.append(
            f"{len(undersized)} image(s) are smaller than the {box[0]}x{box[1]} "
            f"panel and will render undersized - the engine caps scale at 1.0 "
            f"and never enlarges: {sample}{more}"
        )

    return warnings


def report(warnings: Iterable[str], header: str = "Device budget") -> List[str]:
    """
    Print the warnings and hand them back for BuildResult.warnings.

    Returns the list so the caller can attach it to the build result rather
    than the log being the only place it ever existed.
    """
    collected = list(warnings)
    if not collected:
        print(f"     [OK] {header}: no warnings")
        return collected

    print(f"     [WARN] {header}: {len(collected)} item(s)")
    for warning in collected:
        print(f"        - {warning}")
    return collected
