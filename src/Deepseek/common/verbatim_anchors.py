"""Verbatim anchor locks, per-chapter salience, and two-way reconciliation.

A verbatim anchor is a phrase whose English surface is decided once for a
whole series and must not drift: a catchphrase, a running gag, a coined term.
``context.xml`` already carries them as ``<verbatim_anchors>`` with a ``<jp>``
and an ``<en>``, and the series bible keeps the same set in
``bibles/<series_id>/verbatim_anchors.json``.

Why this module exists. Measured on Vol.4 (21 chapters, claude-fable-5-1,
Batch API at wave_size 4):

* 重畳 IS anchored (``<en>Splendid</en>``). It appears 45 times across eight
  chapters and the surface holds in every one of them, mockery and
  noun-ification included.
* こほろん is NOT anchored. It appears 4 times, in CH18 and CH19 — the SAME
  batch wave, therefore mutually blind — and shipped as three different
  surfaces: "kohoron", "Kohon", "Ahem".
* こいマジ is NOT anchored. 8 occurrences across CH06/12/19/20; only CH06
  carries a recognisable rendering at all.

The lock works. What was missing was (a) getting recurring phrases INTO the
anchor set, and (b) telling a chapter what its predecessors actually did with
an anchor, which no amount of context window supplies when the predecessor is
still in flight beside it in the same batch job.

Nothing here writes to ``context.xml``. That document is the cached system
prefix, and on a prefix-bound model (see
src/Anthropic/conversation.py::PREFIX_BOUND_THINKING_MODELS) rewriting it
mid-volume invalidates both the prompt cache and every stored thinking block.
Reconciliation is therefore emitted as its own artifact for a later QC or
bible pass to fold home.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

# An English surface longer than this is a title or a descriptive gloss
# ("a chill, fluffy board-game café romcom"), not a lexeme a translator
# repeats verbatim. Reconciling those produces noise, so they are carried as
# locks but reported as unchecked.
_MAX_RECONCILABLE_SURFACE = 60

# Characters that make a surface behave like an English word, and therefore
# want word-boundary matching rather than raw substring counting.
_ALPHA_RE = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class Anchor:
    """One locked phrase, as declared by prep in ``<verbatim_anchors>``."""

    anchor_id: str
    jp: str
    en: str
    locked: bool = True
    category: str = ""
    character: str = ""
    forbidden_synonyms: Tuple[str, ...] = ()
    notes: str = ""

    @property
    def reconcilable(self) -> bool:
        """Whether this anchor's surface is short enough to verify in prose."""
        return bool(self.en) and len(self.en) <= _MAX_RECONCILABLE_SURFACE


@dataclass
class AnchorObservation:
    """What already-translated chapters actually did with one anchor."""

    anchor: Anchor
    source_occurrences: int = 0
    established_rendering: Optional[str] = None
    established_in_chapter: Optional[str] = None
    citations: List[str] = field(default_factory=list)
    forbidden_hits: List[str] = field(default_factory=list)


def _text(element: Optional[ET.Element]) -> str:
    return (element.text or "").strip() if element is not None else ""


def parse_anchors(context_xml: Optional[str]) -> List[Anchor]:
    """Read ``<verbatim_anchors>`` out of a context document.

    Tolerant by design: a malformed or absent block yields an empty list
    rather than an exception, because a missing lexicon must degrade the
    translation's consistency, never stop the run.
    """
    if not context_xml:
        return []
    try:
        root = ET.fromstring(context_xml)
    except ET.ParseError:
        return []
    block = root.find("verbatim_anchors")
    if block is None:
        return []

    anchors: List[Anchor] = []
    for index, node in enumerate(block, start=1):
        jp = _text(node.find("jp"))
        en = _text(node.find("en"))
        if not jp or not en:
            continue
        forbidden = _text(node.find("forbidden_synonyms"))
        anchors.append(
            Anchor(
                anchor_id=str(node.get("id") or f"VREC-{index:03d}"),
                jp=jp,
                en=en,
                locked=str(node.get("locked", "true")).lower() != "false",
                category=str(node.get("category") or ""),
                character=str(node.get("character") or ""),
                forbidden_synonyms=tuple(
                    part.strip() for part in forbidden.split(",") if part.strip()
                ),
                notes=_text(node.find("notes")),
            )
        )
    return anchors


def merge_bible_anchors(anchors: Sequence[Anchor], bible_anchors_path: Path) -> List[Anchor]:
    """Enrich context.xml's anchors with the series bible's structured fields.

    The two stores disagree in shape, and each holds something the other does
    not. context.xml carries the per-volume anchor set with source citations;
    bibles/<series_id>/verbatim_anchors.json carries the cross-volume policy —
    including ``forbidden_synonyms`` as an actual list. In context.xml those
    same synonyms are only prose inside <notes>, so parsing context alone
    leaves drift detection blind (measured: every anchor came back with an
    empty forbidden tuple).

    Merged by Japanese surface. A bible that is absent, unreadable, or
    malformed leaves the anchors exactly as parsed — a series on its first
    volume has no bible yet, and that must not be an error.
    """
    import json

    try:
        payload = json.loads(Path(bible_anchors_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(anchors)
    index: Dict[str, Dict[str, object]] = {}
    for entry in payload.get("anchors") or []:
        if isinstance(entry, dict) and entry.get("jp"):
            index[str(entry["jp"])] = entry

    enriched: List[Anchor] = []
    for anchor in anchors:
        entry = index.get(anchor.jp)
        if not entry:
            enriched.append(anchor)
            continue
        forbidden = entry.get("forbidden_synonyms") or []
        if not isinstance(forbidden, (list, tuple)):
            forbidden = []
        enriched.append(
            Anchor(
                anchor_id=anchor.anchor_id,
                jp=anchor.jp,
                en=anchor.en,
                locked=anchor.locked or bool(entry.get("locked")),
                category=anchor.category,
                character=anchor.character,
                forbidden_synonyms=tuple(str(s).strip() for s in forbidden if str(s).strip())
                or anchor.forbidden_synonyms,
                notes=anchor.notes,
            )
        )
    return enriched


def _core_surface(surface: str) -> Optional[str]:
    """The head-noun core of a multi-word English surface, or None.

    VREC-008 locks 箱庭 to "open box garden", where "open" is a disambiguator
    against 匣庭 ("sealed box garden"). Prose that reasonably says "box
    garden" is not drift, and reporting it as such teaches an operator to
    ignore the report. Dropping the leading modifier gives a second, weaker
    surface to test before calling a chapter non-compliant.
    """
    words = surface.split()
    if len(words) < 3:
        return None
    return " ".join(words[1:])


def anchors_in_source(anchors: Sequence[Anchor], jp_source: str) -> List[Tuple[Anchor, int]]:
    """The anchors this chapter's Japanese actually contains, with counts.

    Scoping matters more than it looks. Shipping all fifteen anchors on every
    chapter would be affordable here, but the envelope sits AFTER the cache
    breakpoint — it is re-billed as fresh input every chapter — and a 714-page
    volume carries a far longer lexicon than a 21-chapter one. Sending only
    what the chapter can actually collide with keeps that cost proportional to
    the risk.
    """
    if not jp_source:
        return []
    found: List[Tuple[Anchor, int]] = []
    for anchor in anchors:
        count = jp_source.count(anchor.jp)
        if count:
            found.append((anchor, count))
    return found


def _surface_pattern(surface: str) -> re.Pattern:
    """Case-insensitive matcher for an English surface.

    Word boundaries only where the surface is alphabetic: 'splendid' must not
    match inside 'splendidly'-style neighbours by accident, while a surface
    carrying punctuation or kana is matched literally.
    """
    escaped = re.escape(surface)
    if _ALPHA_RE.search(surface):
        return re.compile(rf"\b{escaped}\b", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _excerpt(text: str, match: re.Match, width: int = 70) -> str:
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    return " ".join(text[start:end].split())


def observe_prior_usage(
    anchors_present: Sequence[Tuple[Anchor, int]],
    en_dir: Path,
    *,
    exclude_chapter_id: str,
    max_citations: int = 2,
) -> List[AnchorObservation]:
    """Harvest how ALREADY-TRANSLATED chapters rendered each present anchor.

    This is the half of the reconciliation that context.xml cannot supply. The
    anchor block declares what the surface SHOULD be; only the finished
    English says what it actually became, and that is what a later chapter
    needs in order to match rather than re-coin. Chapters are read newest-last
    so the most recent usage wins as the established rendering.

    ``exclude_chapter_id`` keeps a chapter from citing itself on a re-run.
    """
    observations: List[AnchorObservation] = []
    try:
        chapter_files = sorted(Path(en_dir).glob("*_EN.md"))
    except OSError:
        chapter_files = []

    texts: List[Tuple[str, str]] = []
    for path in chapter_files:
        chapter_id = path.stem[: -len("_EN")] if path.stem.endswith("_EN") else path.stem
        if chapter_id == exclude_chapter_id:
            continue
        try:
            texts.append((chapter_id, path.read_text(encoding="utf-8")))
        except OSError:
            continue

    for anchor, count in anchors_present:
        observation = AnchorObservation(anchor=anchor, source_occurrences=count)
        if not anchor.reconcilable:
            observations.append(observation)
            continue

        pattern = _surface_pattern(anchor.en)
        # Every chapter that used the surface, in chapter order.
        hits: List[Tuple[str, str]] = []
        for chapter_id, text in texts:
            match = pattern.search(text)
            if match:
                hits.append((chapter_id, f"{chapter_id}: …{_excerpt(text, match)}…"))

        if hits:
            # The NEAREST PRECEDING chapter, not the newest overall. The rule
            # this serves is "send what the previous chapter did", and on a
            # re-run or a resumed volume later chapters already exist on disk
            # — citing CHAPTER_20 at CHAPTER_15 would be citing the future.
            # Chapter ids are zero-padded, so a lexical compare orders them.
            preceding = [hit for hit in hits if hit[0] < exclude_chapter_id]
            if preceding:
                observation.established_rendering = anchor.en
                observation.established_in_chapter = preceding[-1][0]
                # Citation and attribution come from the same chapters, so a
                # quote always evidences the chapter named beside it.
                observation.citations = [
                    citation for _, citation in preceding[::-1][:max_citations]
                ]
            # No fallback to a LATER chapter. On a re-run or a QC pass the whole
            # volume is on disk, and telling CHAPTER_06 that a surface was
            # "established in CHAPTER_13" cites the future. Where no predecessor
            # used the anchor, the lock travels alone -- which is the honest
            # state, and still authoritative.

        for synonym in anchor.forbidden_synonyms:
            synonym_pattern = _surface_pattern(synonym)
            for chapter_id, text in texts:
                # Only meaningful where the locked surface is absent from that
                # chapter — see the precedence note in reconcile_chapter.
                if synonym_pattern.search(text) and not pattern.search(text):
                    observation.forbidden_hits.append(f"{synonym} ({chapter_id})")
                    break
        observations.append(observation)
    return observations


def reconcile_chapter(
    anchors: Sequence[Anchor],
    *,
    chapter_id: str,
    jp_source: str,
    en_text: str,
) -> List[Dict[str, object]]:
    """Compare one finished chapter against every anchor its source contains.

    Statuses:
      ``confirmed``   the locked surface is present in the English.
      ``partial``     the locked surface's head-noun core appears but its full
                      form does not — usually an over-specified lock
                      (箱庭 -> "open box garden" against prose saying "box
                      garden"), which is an anchor-quality question rather
                      than translator drift.
      ``missing``     the source carries the phrase; neither the surface nor its
                      core appears. This is the こほろん/こいマジ failure — the
                      chapter coined its own rendering.
      ``drifted``     a forbidden synonym appears. Worse than missing, because
                      the surface reservation is actively broken.
      ``not_checked`` the surface is a long gloss rather than a lexeme.
    """
    report: List[Dict[str, object]] = []
    for anchor, count in anchors_in_source(anchors, jp_source):
        row: Dict[str, object] = {
            "anchor_id": anchor.anchor_id,
            "jp": anchor.jp,
            "en": anchor.en,
            "chapter_id": chapter_id,
            "source_occurrences": count,
        }
        if not anchor.reconcilable:
            row.update({"status": "not_checked", "en_occurrences": None})
            report.append(row)
            continue
        observed = len(_surface_pattern(anchor.en).findall(en_text))
        forbidden = [
            synonym
            for synonym in anchor.forbidden_synonyms
            if _surface_pattern(synonym).search(en_text)
        ]
        core = _core_surface(anchor.en)
        core_observed = len(_surface_pattern(core).findall(en_text)) if core else 0
        # Precedence matters, and getting it wrong makes the report useless.
        # A forbidden synonym signals drift ONLY when the locked surface is
        # absent. The bible forbids rendering 重畳 as "Great"; it does not ban
        # the word "Great" from the book -- and VREC-014 locks グレートスプリット
        # to "Great Split", so a naive scan flags every chapter containing it.
        # Measured before this fix: 8 false "drifted" rows, all VREC-001, in
        # chapters where "Splendid" was present and correct.
        if observed:
            status = "confirmed"
        elif forbidden:
            status = "drifted"
        elif core_observed:
            status = "partial"
        else:
            status = "missing"
        row.update(
            {
                "status": status,
                "en_occurrences": observed,
                "core_occurrences": core_observed,
                "forbidden_synonyms_found": forbidden,
            }
        )
        report.append(row)
    return report


def find_recurring_unanchored(
    candidate_phrases: Iterable[str],
    jp_texts: Dict[str, str],
    anchors: Sequence[Anchor],
    *,
    threshold: int = 2,
) -> List[Dict[str, object]]:
    """Phrases recurring more than *threshold* times that carry no locked English.

    The counting half of the "record any verbatim phrase recurring >2" rule.
    Deterministic on purpose: a model is the right judge of WHICH phrases are
    load-bearing (which is why candidates come from prep's own
    ``signature_phrases`` and ``cultural_glossary``), and the wrong tool for
    counting occurrences across a 714-page volume.

    Validated against Vol.4: at threshold 2 this promotes こいマジ (8) and
    こほろん (4) — both of which drifted in the shipped English — while
    correctly leaving 好きピ (2, single chapter) alone.
    """
    anchored = {anchor.jp for anchor in anchors}
    promoted: List[Dict[str, object]] = []
    for phrase in dict.fromkeys(p.strip() for p in candidate_phrases if p and p.strip()):
        if phrase in anchored:
            continue
        per_chapter = {
            chapter_id: text.count(phrase)
            for chapter_id, text in sorted(jp_texts.items())
            if text.count(phrase)
        }
        total = sum(per_chapter.values())
        if total > threshold:
            promoted.append(
                {
                    "jp": phrase,
                    "total_occurrences": total,
                    "chapters": per_chapter,
                    "chapter_span": len(per_chapter),
                    "reason": f"recurs {total}x across {len(per_chapter)} chapter(s) with no locked EN surface",
                }
            )
    promoted.sort(key=lambda row: (-int(row["total_occurrences"]), str(row["jp"])))
    return promoted


def parse_candidate_phrases(context_xml: Optional[str]) -> List[str]:
    """Candidate phrases prep itself judged load-bearing.

    ``<signature_phrases>`` ONLY. An earlier draft also drew from
    ``<cultural_glossary>``, and on Vol.4 that promoted ボドゲ (126), 人妻
    (54), 制服 (44) — ordinary vocabulary that recurs because the book is
    about board games, not because the wording is locked. Frequency is a
    threshold, never a classifier: the model decides WHAT is a signature
    phrase, this module only counts how often it recurs.

    Two exclusions:

    * anything already in ``<name_map>`` (孤太郎君 at 78 occurrences, コタくん,
      モモちゃん, ツクちゃん) — those have a bilingual lock already.
    * pattern entries carrying a 〜 placeholder (〜ですぞ, 〜っしょ？), which are
      inflection templates rather than literal strings and never match source
      text anyway.
    """
    if not context_xml:
        return []
    try:
        root = ET.fromstring(context_xml)
    except ET.ParseError:
        return []
    name_map = root.find("name_map")
    mapped = {
        str(entry.get("jp") or "").strip()
        for entry in (name_map if name_map is not None else [])
        if str(entry.get("jp") or "").strip()
    }
    phrases: List[str] = []
    for node in root.iter("signature_phrases"):
        for phrase in node:
            value = (phrase.text or "").strip()
            if value and value not in mapped and "〜" not in value:
                phrases.append(value)
    return list(dict.fromkeys(phrases))
