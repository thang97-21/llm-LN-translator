"""
Bible Writer — persists one volume's populated context.xml into a flat,
cumulative series bible for cross-volume continuity (PLANNING.md Step 6.6).

The bible is a concatenated merge of context.xml blocks across volumes —
plain, readable, diff-able JSON files under bibles/<series_id>/. No
database, no ChromaDB, no vector store, and NOT the main pipeline's
publisher/author/anime-metadata bibles/*.json — this is per-series
continuity data only: name_map + character_roster -> term_lock.json,
verbatim_anchors -> verbatim_anchors.json, voice_fingerprints +
eps_arc_tracker -> series_pack.json.

context.xml remains the per-volume source of truth; the bible only ever
reads it, never the other way around.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

from src.common.atomic_io import atomic_write_json
from src.common.config import PIPELINE_ROOT, WORK_DIR, get_config_section


class BibleError(RuntimeError):
    """Raised when the bible writer cannot proceed."""


def _bible_dir() -> Path:
    raw = get_config_section("prep").get("bible_dir", "bibles/")
    path = Path(raw)
    return path if path.is_absolute() else PIPELINE_ROOT / path


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _merge_term_lock(root: ET.Element, series_id: str, volume_number: int, existing: Dict[str, Any]) -> Dict[str, Any]:
    term_lock: Dict[str, Any] = existing or {
        "series_id": series_id,
        "characters": {},
        "locations": {},
        "romanization_policy": "Standard Hepburn without macrons",
    }
    term_lock.setdefault("characters", {})
    term_lock.setdefault("locations", {})

    roster_jp_names: set = set()
    roster = root.find("character_roster")
    if roster is not None:
        for character in roster.findall("character"):
            canonical_en = character.get("canonical_name", "").strip()
            identity = character.find("identity")
            jp_name = _text(identity.find("jp_name")) if identity is not None else ""
            if not jp_name or not canonical_en:
                continue
            roster_jp_names.add(jp_name)
            entry = term_lock["characters"].setdefault(
                jp_name, {"en": canonical_en, "locked": True, "first_seen_vol": volume_number}
            )
            entry["en"] = canonical_en  # bible entries stay authoritative-latest, never silently stale
            entry["locked"] = True

    name_map = root.find("name_map")
    if name_map is not None:
        for entry_el in name_map.findall("entry"):
            jp = entry_el.get("jp", "").strip()
            en = entry_el.get("en", "").strip()
            if not jp or not en or jp in roster_jp_names:
                continue
            location = term_lock["locations"].setdefault(
                jp, {"en": en, "locked": True, "first_seen_vol": volume_number}
            )
            location["en"] = en

    return term_lock


def _merge_verbatim_anchors(root: ET.Element, series_id: str, volume_number: int, existing: Dict[str, Any]) -> Dict[str, Any]:
    anchors_doc: Dict[str, Any] = existing or {"series_id": series_id, "anchors": []}
    anchors_doc.setdefault("anchors", [])
    by_jp: Dict[str, Dict[str, Any]] = {a.get("jp"): a for a in anchors_doc["anchors"] if isinstance(a, dict) and a.get("jp")}

    block = root.find("verbatim_anchors")
    if block is not None:
        for anchor_el in block.findall("anchor"):
            jp = _text(anchor_el.find("jp"))
            en = _text(anchor_el.find("en"))
            notes = _text(anchor_el.find("notes"))
            if not jp or not en:
                continue
            if jp in by_jp:
                existing_anchor = by_jp[jp]
                volumes = existing_anchor.setdefault("volumes", [])
                if volume_number not in volumes:
                    volumes.append(volume_number)
            else:
                new_anchor = {"jp": jp, "en": en, "volumes": [volume_number], "context": notes}
                by_jp[jp] = new_anchor
                anchors_doc["anchors"].append(new_anchor)

    return anchors_doc


def _merge_series_pack(root: ET.Element, series_id: str, volume_number: int, existing: Dict[str, Any]) -> Dict[str, Any]:
    pack: Dict[str, Any] = existing or {
        "series_id": series_id,
        "volumes_processed": [],
        "character_voice_fingerprints": {},
        "eps_endpoints": {},
        "volume_cost_audits": {},
    }
    pack.setdefault("volumes_processed", [])
    pack.setdefault("character_voice_fingerprints", {})
    pack.setdefault("eps_endpoints", {})
    pack.setdefault("volume_cost_audits", {})

    if volume_number not in pack["volumes_processed"]:
        pack["volumes_processed"].append(volume_number)
        pack["volumes_processed"].sort()

    voice_block = root.find("voice_fingerprints")
    if voice_block is not None:
        for fp in voice_block.findall("fingerprint"):
            character = fp.get("character", "").strip()
            if not character:
                continue
            pack["character_voice_fingerprints"][character] = {
                "archetype": fp.get("archetype", ""),
                "register": fp.get("register", ""),
                "voice_pattern": _text(fp.find("voice_pattern")),
            }

    arc_block = root.find("eps_arc_tracker")
    if arc_block is not None:
        for character_el in arc_block.findall("character"):
            name = character_el.get("character", "").strip()
            if not name:
                continue
            chapters = character_el.findall("chapter")
            if chapters:
                pack["eps_endpoints"][name] = chapters[-1].get("band", character_el.get("baseline_band", ""))
            elif character_el.get("baseline_band"):
                pack["eps_endpoints"][name] = character_el.get("baseline_band")

    # volume_cost_audits stays whatever it already was (setdefault above) — this
    # lightweight client's translator has no per-volume cost-audit artifact to
    # source it from; wiring that up is future work, not a silent fabrication.
    return pack


def run_write_bible(volume_id: str, series_id: Optional[str] = None) -> Dict[str, Any]:
    """Persist this volume's populated context.xml into the cumulative series bible."""
    work_dir = WORK_DIR / volume_id
    context_path = work_dir / "context.xml"
    if not context_path.exists():
        raise BibleError(f"No context.xml at {context_path} — run prep first.")

    root = ET.fromstring(context_path.read_text(encoding="utf-8"))
    resolved_series_id = series_id or root.get("series_id", "")
    if not resolved_series_id:
        raise BibleError(
            "No series_id — pass one explicitly or run prep_volume first "
            "(prep sets context.xml's root series_id attribute when it can detect one)."
        )

    manifest_path = work_dir / "manifest.json"
    manifest = _load_json(manifest_path)
    try:
        volume_number = int(manifest.get("metadata", {}).get("series_index") or 1)
    except (TypeError, ValueError):
        volume_number = 1

    series_dir = _bible_dir() / resolved_series_id
    series_dir.mkdir(parents=True, exist_ok=True)

    term_lock_path = series_dir / "term_lock.json"
    anchors_path = series_dir / "verbatim_anchors.json"
    pack_path = series_dir / "series_pack.json"

    existing_term_lock = _load_json(term_lock_path)
    existing_anchors = _load_json(anchors_path)
    existing_pack = _load_json(pack_path)

    new_char_count = len(existing_term_lock.get("characters", {}))
    new_anchor_count = len(existing_anchors.get("anchors", []))

    term_lock = _merge_term_lock(root, resolved_series_id, volume_number, existing_term_lock)
    anchors = _merge_verbatim_anchors(root, resolved_series_id, volume_number, existing_anchors)
    pack = _merge_series_pack(root, resolved_series_id, volume_number, existing_pack)

    atomic_write_json(term_lock_path, term_lock)
    atomic_write_json(anchors_path, anchors)
    atomic_write_json(pack_path, pack)

    return {
        "schema": "BibleWriteReceipt",
        "series_id": resolved_series_id,
        "volumes_processed": pack["volumes_processed"],
        "new_terms_added": len(term_lock.get("characters", {})) + len(term_lock.get("locations", {})) - new_char_count,
        "new_anchors_added": len(anchors.get("anchors", [])) - new_anchor_count,
        "bible_path": str(series_dir),
    }
