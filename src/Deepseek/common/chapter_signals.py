"""Shared chapter-risk signals read from the prepared context.xml.

The signal contract is intentionally provider-neutral.  Translation routes may
use the rendered guidance as ordinary chapter metadata, while the OpenAI
Responses route additionally maps it to GPT-6 Astra reasoning updates.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

ASTRA_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

_SIGNAL_ALIASES = {
    "ambiguity": "ambiguity",
    "pronoun_ambiguity": "ambiguity",
    "reference_ambiguity": "ambiguity",
    "ateji": "ateji",
    "ateji_device": "ateji",
    "ateji_devices": "ateji",
    "multi_speaker": "multi_speaker",
    "multi_speakers": "multi_speaker",
    "speaker_collision": "multi_speaker",
    "speaker_disambiguation": "multi_speaker",
    "voice_contrast": "voice_contrast",
    "fingerprint_contrast": "voice_contrast",
    "different_fingerprints": "voice_contrast",
    "wordplay": "wordplay",
    "pun": "wordplay",
    "pov_shift": "pov_shift",
    "pov_ambiguity": "pov_shift",
    "continuity": "continuity",
    "continuity_risk": "continuity",
    "structural": "structural",
    "structural_risk": "structural",
}
_HIGH_RISK_SIGNALS = {"ambiguity", "ateji", "multi_speaker", "voice_contrast"}
# Public alias: other routes (src/Anthropic/optimization.py's advisor-mode
# gating) need to test category membership without reaching into a
# module-private name.
HIGH_RISK_SIGNALS = frozenset(_HIGH_RISK_SIGNALS)
_SEVERITIES = {"low", "medium", "high", "critical"}


def parse_chapter_signals(context_xml: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Return normalized chapter signal records from the shared XML contract.

    The parser accepts the canonical ``<signal name=...>`` shape and a small
    set of equivalent ``risk``/``flag`` spellings so hand-maintained or older
    context files remain consumable.  Missing or malformed XML is a safe empty
    result, matching the provider-owned context parsers.
    """
    root = _parse(context_xml)
    block = root.find("chapter_signals") if root is not None else None
    if block is None:
        return {}

    chapters: Dict[str, Dict[str, Any]] = {}
    for chapter in block.findall("chapter"):
        chapter_id = (chapter.get("id") or "").strip()
        if not chapter_id or (chapter.get("status") or "").strip().lower() == "pending":
            continue

        signals: List[Dict[str, str]] = []
        speakers: List[Dict[str, str]] = []
        for element in chapter.iter():
            tag = _local_name(element.tag)
            if tag in {"signal", "risk", "flag"}:
                raw_name = (
                    element.get("name")
                    or element.get("kind")
                    or element.get("type")
                    or (element.text or "")
                )
                name = _canonical_signal_name(raw_name)
                if not name:
                    continue
                severity = _normalize_severity(element.get("severity"), name)
                record: Dict[str, str] = {"name": name, "severity": severity}
                detail = (element.get("detail") or element.get("reason") or "").strip()
                if detail:
                    record["detail"] = detail
                signals.append(record)
            elif tag in {"speaker", "active_speaker"}:
                name = (
                    element.get("name")
                    or element.get("character")
                    or element.get("canonical_name")
                    or ""
                ).strip()
                if not name:
                    continue
                profile = (
                    element.get("voice_profile")
                    or element.get("fingerprint")
                    or element.get("profile")
                    or ""
                ).strip()
                speaker = {"name": name}
                if profile:
                    speaker["voice_profile"] = profile
                speakers.append(speaker)

        chapters[chapter_id] = {
            "signals": _dedupe_signals(signals),
            "speakers": _dedupe_speakers(speakers),
        }
    return chapters


def build_chapter_signal_guidance(record: Optional[Dict[str, Any]]) -> str:
    """Render one chapter's signals for any model's current user turn."""
    if not record:
        return ""
    signals = record.get("signals") or []
    speakers = record.get("speakers") or []
    if not signals and not speakers:
        return ""

    lines = [
        "<chapter_signal_guidance>",
        "Use these prep-derived semantic signals to focus translation review; verify them against the current source and do not invent facts:",
    ]
    for signal in signals:
        label = signal.get("name", "signal")
        severity = signal.get("severity", "medium")
        detail = f" — {signal['detail']}" if signal.get("detail") else ""
        lines.append(f"- {label} (severity={severity}){detail}")
    if speakers:
        rendered = []
        for speaker in speakers:
            profile = speaker.get("voice_profile")
            rendered.append(f"{speaker['name']} [{profile}]" if profile else speaker["name"])
        lines.append("- active speakers: " + ", ".join(rendered))
    lines.append("Preserve source ambiguity and keep distinct active voice fingerprints separate.")
    lines.append("</chapter_signal_guidance>")
    return "\n".join(lines)


def select_reasoning_effort(
    model: str,
    configured_effort: Any,
    record: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Select an Astra-only effort target without changing other providers.

    This is a policy result, not an API mutation.  The OpenAI client decides
    whether the target differs from the currently applied effort and, only for
    GPT-6 Astra, serializes the resulting configuration_update item.
    """
    if str(model).strip().lower() != "gpt-6-astra":
        return None

    baseline = normalize_reasoning_effort(model, configured_effort)

    signals = record.get("signals") if record else []
    signals = signals if isinstance(signals, list) else []
    names = {str(signal.get("name") or "") for signal in signals if isinstance(signal, dict)}
    severities = {
        str(signal.get("severity") or "").lower()
        for signal in signals
        if isinstance(signal, dict)
    }

    speakers = record.get("speakers") if record else []
    profiles = {
        str(speaker.get("voice_profile") or "")
        for speaker in speakers or []
        if isinstance(speaker, dict) and str(speaker.get("voice_profile") or "").strip()
    }
    distinct_voice_speakers = len(speakers or []) >= 2 and len(profiles) >= 2
    if distinct_voice_speakers:
        names.add("voice_contrast")
        names.add("multi_speaker")

    if "critical" in severities or {"ambiguity", "ateji", "multi_speaker"}.issubset(names):
        required = "max"
    elif names & _HIGH_RISK_SIGNALS or "high" in severities:
        required = "high"
    elif "medium" in severities:
        required = "medium"
    else:
        required = "low"

    return max((baseline, required), key=ASTRA_EFFORT_ORDER.index)


def normalize_reasoning_effort(model: str, configured_effort: Any) -> str:
    """Normalize legacy disabled/minimal values for Astra's effort enum."""
    effort = str(configured_effort or "medium").strip().lower()
    if str(model).strip().lower() == "gpt-6-astra" and effort in {"none", "minimal"}:
        return "low"
    return effort if effort in ASTRA_EFFORT_ORDER else "medium"


def _parse(context_xml: Optional[str]) -> Optional[ET.Element]:
    if not context_xml:
        return None
    try:
        return ET.fromstring(context_xml)
    except ET.ParseError:
        return None


def _local_name(tag: Any) -> str:
    value = str(tag)
    return value.rsplit("}", 1)[-1].lower()


def _canonical_signal_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return _SIGNAL_ALIASES.get(normalized, normalized)


def _normalize_severity(value: Optional[str], signal_name: str) -> str:
    severity = str(value or "").strip().lower()
    if severity in _SEVERITIES:
        return severity
    return "high" if signal_name in _HIGH_RISK_SIGNALS else "medium"


def _dedupe_signals(signals: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for signal in signals:
        key = (signal.get("name"), signal.get("severity"), signal.get("detail", ""))
        if key not in seen:
            seen.add(key)
            result.append(signal)
    return result


def _dedupe_speakers(speakers: List[Dict[str, str]]) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for speaker in speakers:
        key = (speaker.get("name"), speaker.get("voice_profile", ""))
        if key not in seen:
            seen.add(key)
            result.append(speaker)
    return result


__all__ = [
    "ASTRA_EFFORT_ORDER",
    "build_chapter_signal_guidance",
    "normalize_reasoning_effort",
    "parse_chapter_signals",
    "select_reasoning_effort",
]
