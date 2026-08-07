"""
Optional XTC/XTCH export, delegated to an external renderer.

.xtc / .xtch is the CrossPoint firmware's native format: a container of
PRE-RENDERED bitmap pages. It stores no text at all. The container itself is
trivial -- 56-byte header, optional 256-byte metadata block, 96 bytes per
chapter, a 16-byte-per-page index, then embedded XTG/XTH pages -- and we parse
enough of it below to verify what we produced.

Filling it is the hard part. Every page has to be typeset and rasterized:
line breaking, hyphenation, pagination, and hinted 1-bit font rendering. That
is why every converter in existence wraps CREngine from CoolReader, and why we
do not write our own. cr2xt is the most capable of them and is GUI-only, so the
only automatable option is bigbag/epub-to-xtc-converter's Node CLI.

The trade is genuinely unattractive for a translation pipeline, which is why
this is opt-in and off by default:

    ~400-page volume     optimized EPUB ~1-2 MB     XTC ~19 MB     XTCH ~38 MB
    first chapter open   5-10s, then cached         none           none
    reader font/size     user's choice              baked in       baked in

We save a one-time parse the device already caches, and charge the reader their
ability to resize the text for it. Worth having; not worth defaulting to.

A note on settings.json: its schema belongs to the converter, not to us. Rather
than inventing key names from documentation, export_settings() runs the tool's
own `init` to generate its current defaults and patches conservatively on top.
Anything we cannot verify is left to the operator via builder.xtc.settings,
which is deep-merged verbatim.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .device_profiles import DeviceProfile

# Container magic, little-endian uint32 at offset 0.
XTC_MAGIC = b"XTC\x00"
XTCH_MAGIC = b"XTCH"

# Header field offsets we actually read back.
_OFF_VERSION = 0x04
_OFF_PAGE_COUNT = 0x06

MIN_NODE_MAJOR = 18

VALID_FORMATS = ("xtc", "xtch")


@dataclass
class XtcResult:
    """Outcome of an export attempt. Never raises into the build."""

    ok: bool
    output_path: Optional[Path] = None
    size_bytes: int = 0
    page_count: int = 0
    fmt: str = "xtc"
    error: Optional[str] = None
    warnings: list = field(default_factory=list)


class XtcPreconditionError(RuntimeError):
    """A prerequisite is missing. Carries a message meant for a human."""


def _resolve_node(node_bin: str) -> str:
    """Locate node and confirm it is new enough for the converter."""
    node = shutil.which(node_bin) if node_bin else shutil.which("node")
    if not node:
        raise XtcPreconditionError(
            f"Node.js not found (looked for {node_bin or 'node'!r}). The XTC "
            f"converter is a Node CLI; install Node {MIN_NODE_MAJOR}+ or set "
            f"builder.xtc.node_bin to its full path."
        )

    try:
        proc = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XtcPreconditionError(f"Could not run {node} --version: {exc}") from exc

    match = re.search(r"v(\d+)\.", proc.stdout.strip())
    if not match:
        raise XtcPreconditionError(
            f"Could not parse Node version from {proc.stdout.strip()!r}"
        )
    major = int(match.group(1))
    if major < MIN_NODE_MAJOR:
        raise XtcPreconditionError(
            f"Node {major} is too old; the converter needs {MIN_NODE_MAJOR}+"
        )
    return node


def _resolve_cli(converter_path: str) -> Path:
    """Find cli/index.js inside the converter checkout."""
    if not converter_path:
        raise XtcPreconditionError(
            "builder.xtc.converter_path is not set. Clone "
            "https://github.com/bigbag/epub-to-xtc-converter, run `npm install` "
            "in its cli/ directory, and point converter_path at the checkout."
        )

    root = Path(converter_path).expanduser()
    for candidate in (root / "cli" / "index.js", root / "index.js"):
        if candidate.is_file():
            if not (candidate.parent / "node_modules").is_dir():
                raise XtcPreconditionError(
                    f"Found {candidate} but no node_modules beside it - run "
                    f"`npm install` in {candidate.parent}"
                )
            return candidate

    raise XtcPreconditionError(
        f"No cli/index.js under {root}. Is builder.xtc.converter_path pointing "
        f"at the epub-to-xtc-converter checkout?"
    )


def _resolve_font(font_path: str) -> Path:
    """
    Confirm a usable font.

    This cannot be defaulted: there are no font files anywhere in this
    repository (get_fonts_to_embed() in config.py names four Google Sans faces
    that do not exist on disk), and the converter will not rasterize without
    one. Prefer a HINTED face -- Roboto, Tahoma, Verdana. Hinting is what keeps
    text legible once it is dithered to one bit; an unhinted face that looks
    elegant at 300 PPI turns to mush at 219.
    """
    if not font_path:
        raise XtcPreconditionError(
            "builder.xtc.font_path is not set. XTC rendering rasterizes text, "
            "so a TTF/OTF is required, and this repository ships none. Use a "
            "hinted face (Roboto, Tahoma, Verdana) - hinting is what survives "
            "1-bit dithering."
        )

    path = Path(font_path).expanduser()
    if not path.is_file():
        raise XtcPreconditionError(f"Font not found: {path}")
    if path.suffix.lower() not in (".ttf", ".otf"):
        raise XtcPreconditionError(f"Font must be .ttf or .otf, got {path.suffix}")
    return path


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge; overrides win, nested dicts are merged not replaced."""
    result = dict(base)
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _patch_known_keys(
    settings: Dict[str, Any],
    profile: DeviceProfile,
    font: Path,
) -> Dict[str, Any]:
    """
    Patch only the keys we can see in the converter's own generated defaults.

    Deliberately conservative. We do not create structure the tool did not
    emit, because a key we invented would be silently ignored and we would have
    no way to tell. Whatever this misses, builder.xtc.settings can set
    explicitly.
    """
    patched = dict(settings)
    width, height = profile.screen or (0, 0)

    if isinstance(patched.get("font"), dict):
        patched["font"] = dict(patched["font"])
        patched["font"]["path"] = str(font)

    # Panel geometry comes from the Part A registry so the EPUB and the XTC can
    # never disagree about how big the screen is.
    if width and height:
        for key, value in (("width", width), ("height", height)):
            if key in patched:
                patched[key] = value
        for container in ("device", "screen", "display"):
            if isinstance(patched.get(container), dict):
                nested = dict(patched[container])
                if "width" in nested:
                    nested["width"] = width
                if "height" in nested:
                    nested["height"] = height
                patched[container] = nested

    return patched


def _generate_settings(
    node: str,
    cli: Path,
    scratch: Path,
    profile: DeviceProfile,
    font: Path,
    overrides: Dict[str, Any],
) -> Path:
    """Run the converter's own `init`, then patch its output."""
    subprocess.run(
        [node, str(cli), "init"],
        cwd=str(scratch),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    generated = scratch / "settings.json"
    base: Dict[str, Any] = {}
    if generated.is_file():
        try:
            base = json.loads(generated.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            base = {}

    settings = _patch_known_keys(base, profile, font)
    settings = _deep_merge(settings, overrides)

    target = scratch / "mtls-settings.json"
    target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return target


def read_container_header(path: Path) -> Dict[str, Any]:
    """
    Read back magic, version and page count.

    A cheap self-check that we produced a real container rather than a truncated
    file the converter gave up on halfway through.
    """
    with open(path, "rb") as handle:
        header = handle.read(56)

    if len(header) < 56:
        return {"valid": False, "reason": f"header is {len(header)} bytes, expected 56"}

    magic = header[0:4]
    if magic not in (XTC_MAGIC, XTCH_MAGIC):
        return {"valid": False, "reason": f"bad magic {magic!r}"}

    version = struct.unpack_from("<H", header, _OFF_VERSION)[0]
    page_count = struct.unpack_from("<H", header, _OFF_PAGE_COUNT)[0]
    return {
        "valid": True,
        "magic": magic.decode("ascii", "replace").rstrip("\x00"),
        "version": f"{version >> 8}.{version & 0xFF}",
        "page_count": page_count,
    }


def export_xtc(
    epub_path: Path,
    profile: DeviceProfile,
    xtc_cfg: Dict[str, Any],
    output_dir: Optional[Path] = None,
    scratch_dir: Optional[Path] = None,
) -> XtcResult:
    """
    Render an already-built EPUB to .xtc/.xtch.

    Only `convert` is called, never the converter's own `optimize`: the EPUB
    handed in here has already been through the device profile, and running its
    optimizer over ours would re-encode every image a second time.

    Failure is non-fatal by contract. The caller has a finished .epub either
    way; this returns ok=False with an error rather than raising.
    """
    fmt = str(xtc_cfg.get("format", "xtc")).strip().lower()
    if fmt not in VALID_FORMATS:
        return XtcResult(
            ok=False,
            fmt=fmt,
            error=f"builder.xtc.format must be one of {VALID_FORMATS}, got {fmt!r}",
        )

    if not profile.screen:
        return XtcResult(
            ok=False,
            fmt=fmt,
            error=(
                f"Profile {profile.name!r} has no panel geometry. XTC renders "
                f"fixed-size pages, so it needs a device profile "
                f"(xteink-x3 or xteink-x4)."
            ),
        )

    try:
        node = _resolve_node(str(xtc_cfg.get("node_bin", "") or ""))
        cli = _resolve_cli(str(xtc_cfg.get("converter_path", "") or ""))
        font = _resolve_font(str(xtc_cfg.get("font_path", "") or ""))
    except XtcPreconditionError as exc:
        return XtcResult(ok=False, fmt=fmt, error=str(exc))

    epub_path = Path(epub_path)
    output_dir = Path(output_dir) if output_dir else epub_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{epub_path.stem}.{fmt}"

    scratch = Path(scratch_dir) if scratch_dir else output_dir / ".xtc-scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    try:
        settings = _generate_settings(
            node, cli, scratch, profile, font, xtc_cfg.get("settings") or {}
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return XtcResult(ok=False, fmt=fmt, error=f"Could not prepare settings: {exc}")

    command = [
        node,
        str(cli),
        "convert",
        str(epub_path),
        "-o",
        str(output_path),
        "-f",
        fmt,
        "-c",
        str(settings),
    ]

    timeout = int(xtc_cfg.get("timeout_seconds", 1800) or 1800)
    print(f"     [XTC] {' '.join(command)}")

    try:
        proc = subprocess.run(
            command,
            cwd=str(cli.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return XtcResult(
            ok=False,
            fmt=fmt,
            error=f"Converter timed out after {timeout}s (builder.xtc.timeout_seconds)",
        )
    except OSError as exc:
        return XtcResult(ok=False, fmt=fmt, error=f"Could not run converter: {exc}")

    for line in (proc.stdout or "").splitlines()[-20:]:
        print(f"     [XTC] {line}")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        return XtcResult(
            ok=False,
            fmt=fmt,
            error=f"Converter exited {proc.returncode}: " + " | ".join(tail),
        )

    if not output_path.is_file():
        return XtcResult(
            ok=False,
            fmt=fmt,
            error=f"Converter reported success but {output_path} does not exist",
        )

    header = read_container_header(output_path)
    warnings = []
    if not header.get("valid"):
        warnings.append(f"container header looks wrong: {header.get('reason')}")

    return XtcResult(
        ok=True,
        output_path=output_path,
        size_bytes=output_path.stat().st_size,
        page_count=int(header.get("page_count", 0)),
        fmt=fmt,
        warnings=warnings,
    )
