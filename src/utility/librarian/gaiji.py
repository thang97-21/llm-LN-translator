"""
Gaiji resolution - turn inline glyph images back into the characters they draw.

Publishers ship characters their fonts lack (芦, №, voiced-kana variants) as
tiny inline images. Dropping them silently corrupts the text: volume 29917f
lost the 芦 of the protagonist's surname 芦田 in all 31 occurrences, and prep
built its name map around a bare 田.

Resolution order:
1. The image's alt text, when the publisher filled it in.
2. gaiji_glyphs.json, keyed by SHA-256 of the image bytes (filenames are
   renumbered per book, the bytes are not).
3. Otherwise unresolved: the caller decides what to emit, and a warning names
   the image hash so the table can be extended.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GLYPH_TABLE_PATH = Path(__file__).with_name("gaiji_glyphs.json")

# The geta mark: JP typesetting's own "a glyph belongs here" placeholder.
GAIJI_PLACEHOLDER = "〓"

# Alt values that describe the image rather than transcribe it.
_GENERIC_ALTS = {"gaiji", "外字", "image", "img", "glyph"}
_MAX_ALT_GLYPH_LEN = 4

_IMAGE_FOLDERS = ("image", "images", "Images", "IMAGES")


def _load_table(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[GAIJI] Glyph table unreadable at %s: %s", path, exc)
        return {}
    return {
        digest.lower(): entry["glyph"]
        for digest, entry in (data.get("by_sha256") or {}).items()
        if isinstance(entry, dict) and entry.get("glyph")
    }


class GaijiResolver:
    """Resolves gaiji images to text, caching per image filename."""

    def __init__(self, content_dir: Optional[Path] = None, table_path: Path = GLYPH_TABLE_PATH):
        self.content_dir = Path(content_dir) if content_dir else None
        self._table = _load_table(table_path)
        self._cache: Dict[str, Tuple[Optional[str], Optional[str], Optional[Path]]] = {}
        self._warned: set = set()
        self.unresolved: List[str] = []

    def resolve(self, image_filename: str, alt: str = "", warn: bool = True) -> Optional[str]:
        """
        Return the glyph for a gaiji image, or None when it cannot be determined.

        warn=False is for images that only *might* be gaiji (small-file
        heuristic): a miss there is usually a decorative icon, not lost text.
        """
        alt = (alt or "").strip()
        if alt and len(alt) <= _MAX_ALT_GLYPH_LEN and alt.lower() not in _GENERIC_ALTS:
            return alt

        if image_filename not in self._cache:
            self._cache[image_filename] = self._lookup_by_hash(image_filename)
        glyph, digest, image_path = self._cache[image_filename]
        if glyph is None and warn and image_filename not in self._warned:
            self._warned.add(image_filename)
            self.unresolved.append(image_filename)
            if image_path is None:
                logger.warning("[GAIJI] %s unresolved: image file not found (no content_dir?)", image_filename)
            else:
                logger.warning(
                    "[GAIJI] %s unresolved. Look at the image and add to %s: "
                    '"%s": {"glyph": "<char>", "source": "%s"}',
                    image_filename, GLYPH_TABLE_PATH.name, digest, image_path,
                )
        return glyph

    def _lookup_by_hash(self, image_filename: str) -> Tuple[Optional[str], Optional[str], Optional[Path]]:
        image_path = self._find_image(image_filename)
        if image_path is None:
            return None, None, None
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        return self._table.get(digest), digest, image_path

    def _find_image(self, image_filename: str) -> Optional[Path]:
        if not self.content_dir:
            return None
        for folder in _IMAGE_FOLDERS:
            candidate = self.content_dir / folder / image_filename
            if candidate.exists():
                return candidate
        return None
