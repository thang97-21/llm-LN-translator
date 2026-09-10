"""
Librarian Agent - EPUB Extraction and Cataloging Orchestrator.

Main entry point for Phase 1 of the MT Publishing Pipeline.
Extracts source EPUBs, parses metadata, converts chapters to markdown,
and generates manifest.json for downstream agents.
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple, Set
from dataclasses import dataclass, field, asdict
import re
from xml.etree import ElementTree as ET

from .epub_extractor import EPUBExtractor, ExtractionResult, extract_opf_metadata
from .metadata_parser import MetadataParser
from .toc_parser import TOCParser, TableOfContents
from .spine_parser import SpineParser, Spine, SpineItem
from .xhtml_to_markdown import XHTMLToMarkdownConverter, ConvertedChapter
from .image_extractor import ImageExtractor, catalog_images
from .content_splitter import ContentSplitter, KodanshaSplitter
from .config import get_volume_structure, get_work_dir, get_pre_toc_detection_config
from .publisher_profiles.manager import get_profile_manager, PublisherProfile

# Phase 1.55: Reference Validator — stripped from the lightweight client.
# post_processor was never copied here, so this path is permanently disabled
# rather than a real optional-import fallback.
compile_reference_payloads = None
REFERENCE_VALIDATOR_AVAILABLE = False


@dataclass
class ChapterEntry:
    """Chapter entry for manifest."""
    id: str
    source_file: str
    translated_file: str
    word_count: int
    toc_order: int = 0  # Position in canonical TOC order
    toc_level: int = 0  # Nesting level (0=main, 1=sub-chapter, etc.)
    translation_status: str = "pending"
    qc_status: str = "pending"
    is_pre_toc_content: bool = False  # True if unlisted opening hook
    source_files: List[str] = field(default_factory=list)  # Original XHTML spine files for this chapter
    raw_group_index: Optional[int] = None  # Original unsplit spine-group index
    raw_group_title: Optional[str] = None  # Original unsplit spine-group title
    split_strategy: Optional[str] = None  # Spine fallback split strategy name

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineState:
    """State tracking for pipeline stages."""
    status: str = "pending"
    timestamp: Optional[str] = None
    chapters_completed: int = 0
    chapters_total: int = 0
    current_chapter: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Manifest:
    """Runtime-only manifest for pipeline orchestration.

    Semantic and bibliographic data are persisted in ``context.xml``.
    """
    version: str = "1.0"
    volume_id: str = ""
    created_at: str = ""
    runtime_config: Dict[str, Any] = field(default_factory=dict)
    pipeline_state: Dict[str, Any] = field(default_factory=dict)
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    assets: Dict[str, Any] = field(default_factory=dict)
    volume_structure: Dict[str, Any] = field(default_factory=dict)  # Multi-act/merged volume structure

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path):
        """Save manifest to JSON file."""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> 'Manifest':
        """Load manifest from JSON file."""
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls(**data)


class LibrarianAgent:
    """
    Orchestrates EPUB extraction and content cataloging.

    Workflow:
    1. Extract EPUB to working directory
    2. Parse OPF metadata (title, author, etc.)
    3. Parse TOC for chapter ordering
    4. Convert XHTML chapters to markdown
    5. Catalog images by type
    6. Generate manifest.json
    """

    # Non-story XHTML classes/markers seen in JP commercial EPUB front/end matter.
    # These pages are notices/credits/colophon metadata and must not become chapters.
    NON_CONTENT_BODY_CLASSES: Set[str] = {
        "caution-page",    # vertical-reading notice
        "info-middle",     # illustrator/designer credits
        "info-top",        # external link notices
        "colophon-page",   # publication/legal page
    }

    @staticmethod
    def _use_overlap_kuchie_format(publisher_profile: Optional[PublisherProfile]) -> bool:
        """
        Overlap Bunko kuchie naming convention:
        keep normalized front-matter color plates as k001/k002/... for
        downstream traceability with source EPUB image IDs.
        """
        if publisher_profile is None:
            return False
        canonical = str(getattr(publisher_profile, "canonical_name", "") or "").strip().lower()
        aliases = [str(a).strip().lower() for a in (getattr(publisher_profile, "aliases", []) or [])]
        if canonical == "overlap":
            return True
        return any(a in {"overlap", "オーバーラップ", "株式会社オーバーラップ"} for a in aliases)

    @classmethod
    def _format_kuchie_filename(
        cls,
        index: int,
        extension: str,
        publisher_profile: Optional[PublisherProfile],
    ) -> str:
        """Build normalized kuchie filename using publisher-specific convention."""
        ext = extension if extension else ".jpg"
        if cls._use_overlap_kuchie_format(publisher_profile):
            return f"k{index:03d}{ext}"
        return f"kuchie-{index:03d}{ext}"

    @staticmethod
    def _is_frontmatter_spine_page(spine_item: SpineItem) -> bool:
        """
        Return True for spine entries that belong to front/back matter wrappers.

        These entries may include TOC/title pages between color plates and should
        not be treated as chapter-content boundaries.
        """
        idref_lower = str(getattr(spine_item, "idref", "") or "").lower()
        href_lower = str(getattr(spine_item, "href", "") or "").lower()
        markers = (
            "cover",
            "fmatter",
            "frontmatter",
            "toc",
            "nav",
            "caution",
            "insert",
            "notice",
            "colophon",
        )
        return any(token in idref_lower or token in href_lower for token in markers)
    NON_CONTENT_TEXT_PAIRS: Tuple[Tuple[str, str], ...] = (
        ("本作品は、縦書き表示での閲覧を推奨", "表示が一部くずれる恐れがあります"),
        ("口絵・本文イラスト", "デザイン／"),
        ("本電子書籍内の外部リンクに関して", "ご利用の端末によっては"),
        ("本電子書籍は、購入者個人の閲覧の目的のためにのみ", "私的利用の範囲をこえる行為は著作権法上、禁じられています"),
    )

    def __init__(self, work_base: Optional[Path] = None):
        """
        Initialize Librarian agent.

        Args:
            work_base: Base working directory (defaults to WORK/)
        """
        self.work_base = Path(work_base) if work_base else get_work_dir()
        self.work_base.mkdir(parents=True, exist_ok=True)
    
    def process_epub(
        self,
        epub_path: Path,
        volume_id: Optional[str] = None,
        source_lang: str = "ja",
        target_lang: str = "en",
        validate_references: bool = False,
        force_rerun: bool = False,
    ) -> Manifest:
        """
        Process source EPUB through full extraction pipeline.

        Args:
            epub_path: Path to source EPUB file
            volume_id: Optional custom volume ID
            source_lang: Source language code (default: ja)
            target_lang: Target language code (default: en)
            force_rerun: Auto-continue re-extractions into a NEW derived volume
                directory (old output preserved) instead of prompting — see
                EPUBExtractor.extract.

        Returns:
            Manifest object with extraction results
        """
        epub_path = Path(epub_path)
        print(f"\n{'='*60}")
        print(f"LIBRARIAN AGENT - Processing: {epub_path.name}")
        print(f"{'='*60}\n")

        # Step 1: Extract EPUB
        print("[STEP 1/5] Extracting EPUB...")
        extractor = EPUBExtractor(self.work_base)
        extraction = extractor.extract(epub_path, volume_id, force_rerun=force_rerun)

        if not extraction.success:
            raise RuntimeError(f"EPUB extraction failed: {extraction.error}")

        volume_id = extraction.volume_id
        work_dir = extraction.work_dir
        structure = get_volume_structure()

        # Step 2: Parse metadata
        print("\n[STEP 2/5] Parsing metadata...")
        metadata_parser = MetadataParser()
        metadata = metadata_parser.parse_opf(extraction.opf_path)
        print(f"     Title: {metadata.title}")
        print(f"     Author: {metadata.author}")
        print(f"     Language: {metadata.language}")

        # Step 3: Parse TOC and Spine
        print("\n[STEP 3/5] Parsing table of contents and spine...")
        toc_parser = TOCParser(extraction.epub_root)
        toc = toc_parser.parse()
        print(f"     Found {len(toc.get_flat_list())} navigation entries")
        print(f"     Format: {toc.format.upper()}")

        # Parse spine for actual reading order
        spine_parser = SpineParser(extraction.opf_path)
        spine = spine_parser.parse()
        spine_parser.detect_illustration_pages(spine)
        print(f"     Spine: {len(spine.items)} items ({len([i for i in spine.items if i.is_illustration])} illustration pages)")

        # Detect multi-act/multi-volume structure (e.g., merged Shueisha publications)
        volume_acts = self._detect_volume_acts(spine, toc, extraction.content_dir)
        if volume_acts:
            print(f"     [MULTI-ACT] Detected {len(volume_acts)} acts in merged volume")
            for act in volume_acts:
                print(f"       Act {act['act_number']}: {act['title']} ({len(act['kuchie_spine_indices'])} kuchie pages)")

        # Get publisher profile for content handling configuration
        profile_manager = get_profile_manager()
        publisher_name = extraction.publisher_canonical
        publisher_profile = extraction.publisher_profile

        # Hybrid spine fallback detection (Solution 3)
        toc_entry_count = len(toc.get_flat_list())
        UNIVERSAL_TOC_THRESHOLD = 3
        
        # Check 1: Is TOC suspiciously minimal?
        is_minimal_toc = toc_entry_count <= UNIVERSAL_TOC_THRESHOLD
        
        # Check 2: Does TOC cover spine content files?
        toc_coverage, missing_files = self._validate_toc_completeness(toc, spine)
        toc_alignment, toc_missing_in_spine = self._validate_toc_alignment(toc, spine)
        # A low spine-coverage ratio can be normal when TOC lists chapter boundaries
        # but chapters span multiple continuation XHTML files.
        # Treat TOC as incomplete only when both signals are weak:
        # 1) low spine coverage AND 2) TOC entries themselves do not align to spine.
        is_incomplete_toc = (toc_coverage < 0.5) and (toc_alignment < 0.9)
        
        # Check 3: Publisher-specific config
        is_publisher_minimal = profile_manager.should_use_spine_fallback(publisher_name, toc_entry_count)

        # Check 4: Are ALL TOC entries front/back-matter only?
        # Kodansha and similar publishers produce TOCs with only cover, contents,
        # colophon entries — zero story chapters. Detecting this prevents the
        # entire story body from collapsing into a single chapter.
        FRONT_BACK_MATTER_LABELS: Set[str] = {
            "表紙", "目次", "奥付",
            "電子特典", "電子版特典", "電子書籍特典",
            "書き下ろしショートストーリー",
            "Cover", "Contents", "Colophon",
            "Bonus", "Digital Bonus", "Afterword",
        }
        is_frontmatter_only = False
        toc_labels = set()
        for np in toc.get_flat_list():
            label_stripped = (np.label or "").strip()
            if label_stripped:
                toc_labels.add(label_stripped)
        if toc_labels:
            # A TOC is frontmatter-only if every label is a known non-story keyword
            # OR if the label contains a known front/back-matter pattern.
            all_known = True
            for label in toc_labels:
                known = label in FRONT_BACK_MATTER_LABELS
                if not known:
                    for fm_label in FRONT_BACK_MATTER_LABELS:
                        if fm_label in label:
                            known = True
                            break
                if not known:
                    all_known = False
                    break
            is_frontmatter_only = all_known

        # Use spine fallback if ANY condition met
        use_spine_fallback = is_minimal_toc or is_incomplete_toc or is_publisher_minimal or is_frontmatter_only
        
        # Track recovery reason for diagnostics
        recovery_reasons = []
        if is_minimal_toc:
            recovery_reasons.append("minimal_toc")
            print(f"     [WARNING] TOC has only {toc_entry_count} entries - likely corrupted")
        if is_incomplete_toc:
            recovery_reasons.append(f"incomplete_coverage_{int(toc_coverage*100)}%")
            print(f"     [WARNING] TOC only covers {toc_coverage:.0%} of spine content ({len(missing_files)} files missing)")
            if len(missing_files) <= 5:
                print(f"     [WARNING] Missing from TOC: {sorted(missing_files)}")
            else:
                print(f"     [WARNING] Missing from TOC: {sorted(list(missing_files)[:5])} ... and {len(missing_files)-5} more")
        elif toc_coverage < 0.5 and toc_alignment >= 0.9:
            print(
                f"     [INFO] TOC uses chapter-boundary entries "
                f"(spine coverage {toc_coverage:.0%}, TOC alignment {toc_alignment:.0%}); "
                f"keeping TOC-based extraction."
            )
        if toc_missing_in_spine:
            print(
                f"     [WARNING] {len(toc_missing_in_spine)} TOC entries not present in spine: "
                f"{sorted(list(toc_missing_in_spine)[:5])}"
            )
        if is_publisher_minimal:
            recovery_reasons.append("publisher_config")
            print(f"     [INFO] Publisher {publisher_name} uses minimal TOC pattern")
        if is_frontmatter_only:
            recovery_reasons.append("frontmatter_only_toc")
            print(f"     [WARNING] TOC contains only front/back-matter entries ({', '.join(sorted(toc_labels))}), no story chapters")
        
        if use_spine_fallback:
            print(f"     [INFO] Using spine-based chapter detection")

        # Step 4: Convert chapters to markdown (with content merging)
        print("\n[STEP 4/5] Converting chapters to markdown...")
        source_dir = work_dir / structure["source_chapters"]
        source_dir.mkdir(parents=True, exist_ok=True)

        # Preserve ruby readings for translator (format: 愛歌{まなか})
        # Pass content_dir for scene break icon detection
        converter = XHTMLToMarkdownConverter(
            remove_ruby=False,
            content_dir=extraction.content_dir,
            exclude_image_matcher=lambda img_name: profile_manager.is_excluded_image(img_name, publisher_name),
        )

        if use_spine_fallback:
            # Use spine-based chapter detection (for minimal-TOC publishers like Hifumi Shobo)
            chapters = self._convert_chapters_from_spine(
                extraction.content_dir,
                source_dir,
                spine,
                converter,
                publisher_name,
                volume_acts=volume_acts
            )
        else:
            # Use standard TOC-based chapter detection
            chapters = self._convert_chapters_with_spine(
                extraction.content_dir,
                source_dir,
                toc,
                spine,
                converter,
                publisher=publisher_name
            )
        print(f"     Converted {len(chapters)} chapters")

        # Post-process: Check for Kodansha heading-based splitting
        heading_split_config = profile_manager.get_heading_split_config(publisher_name)
        if heading_split_config and heading_split_config.get("split_on_heading", False):
            # Pre-scan: detect which chapters actually contain multiple sub-chapters
            _preview_splitter = KodanshaSplitter(heading_patterns=heading_split_config.get("heading_patterns", []))
            _chapters_to_split = []
            for _ch in chapters:
                _ch_path = source_dir / _ch.filename
                if _ch_path.exists():
                    _content = _ch_path.read_text(encoding='utf-8')
                    if _preview_splitter.should_split(_content, min_chapters=2):
                        _num = _preview_splitter.detect_chapters(_content)
                        _chapters_to_split.append((_ch.filename, _num))

            if _chapters_to_split:
                # Show the user what was detected and let them decide
                print()
                print("  ┌─────────────────────────────────────────────────────────────────┐")
                print("  │  [HEADING SPLIT] Unified chapter content detected               │")
                print("  └─────────────────────────────────────────────────────────────────┘")
                print("  The following file(s) contain multiple sub-chapters (### markers):")
                for _fname, _num in _chapters_to_split:
                    print(f"    • {_fname}: {_num} sub-chapters detected")
                print()
                print("  [1] Split into separate chapter files  ← recommended")
                print("  [2] Keep as unified full-chapter content")
                print()
                from src.Deepseek.common.interactive import prompt_or_default
                _choice = prompt_or_default("  Choose [1/2] (default: 1): ", "1").strip()

                if _choice != "2":
                    chapters = self._apply_heading_split(
                        chapters,
                        source_dir,
                        heading_split_config.get("heading_patterns", [])
                    )
                    print(f"     [KODANSHA] Re-split into {len(chapters)} chapters using heading markers")
                else:
                    print("     [INFO] Keeping unified chapter content as-is")

        chapters, inline_afterword_splits = self._split_inline_afterword_chapters(
            chapters,
            source_dir,
        )
        if inline_afterword_splits > 0:
            print(
                f"     [AFTERWORD] Inline afterword split applied: +{inline_afterword_splits} chapter(s)"
            )
        
        # Collect all inline illustrations referenced in chapters
        chapter_illustrations = set()
        for ch in chapters:
            if hasattr(ch, 'illustrations') and ch.illustrations:
                for img in ch.illustrations:
                    chapter_illustrations.add(img)
        if chapter_illustrations:
            print(f"     Found {len(chapter_illustrations)} inline illustrations in chapters")

        # Step 4.5: Extract kuchie from spine (AUTHORITATIVE ORDER)
        print("\n[STEP 4.5/6] Extracting kuchie from spine...")
        spine_kuchie = self._extract_kuchie_from_spine(
            spine,
            extraction.content_dir,
            publisher_profile,
            volume_acts=volume_acts
        )
        print(f"     Found {len(spine_kuchie)} kuchie pages in spine order")
        if spine_kuchie:
            print(f"     Spine order: {' → '.join([k['original'] for k in spine_kuchie])}")

        # Step 5: Catalog/copy images (spine-driven kuchie + spine-driven illustrations)
        print("\n[STEP 5/6] Cataloging images...")
        image_extractor = ImageExtractor(extraction.content_dir, publisher=publisher_name)
        
        # Get kuchie filenames to exclude from illustration extraction
        spine_kuchie_originals = {k['original'] for k in spine_kuchie}
        
        # Catalog images for cover detection only.
        image_catalog = image_extractor.catalog_all()
        image_catalog["kuchie"] = []

        # Authoritative illustration extraction from spine reading order.
        spine_illustrations = self._extract_illustrations_from_spine(
            spine=spine,
            content_dir=extraction.content_dir,
            excluded_originals=spine_kuchie_originals,
            publisher_name=publisher_name,
        )
        from types import SimpleNamespace
        image_catalog["illustrations"] = [
            SimpleNamespace(filename=meta["file"]) for meta in spine_illustrations
        ]
        if spine_illustrations:
            print(f"     Found {len(spine_illustrations)} spine-derived illustrations")

        # Copy images to assets
        assets_dir = work_dir / structure["assets"]
        
        # First, copy spine-based kuchie with proper sequential naming.
        # Keep them under assets/kuchie to match downstream builder expectations.
        kuchie_output_paths = []
        kuchie_filename_mapping = {}
        kuchie_assets_dir = work_dir / structure["kuchie"]
        kuchie_assets_dir.mkdir(parents=True, exist_ok=True)
        for kuchie_meta in spine_kuchie:
            # Source: resolve image_path relative to content_dir
            src_image = extraction.content_dir / kuchie_meta['image_path']
            
            # Destination: assets/kuchie/kuchie-NNN.ext
            dst_image = kuchie_assets_dir / kuchie_meta['file']
            
            if src_image.exists():
                import shutil
                shutil.copy2(src_image, dst_image)
                kuchie_output_paths.append(dst_image)
                kuchie_filename_mapping[kuchie_meta['original']] = kuchie_meta['file']
        
        # Then copy cover only from pattern catalog.
        # Kuchie + illustrations are copied from spine metadata below.
        output_paths, filename_mapping = image_extractor.copy_to_assets(
            assets_dir, 
            exclude_files=spine_kuchie_originals,
            copy_kuchie=False,
            copy_illustrations=False,
        )

        # Guardrail: if cover copy succeeded but first-pass catalog missed it,
        # backfill cover for manifest consistency.
        if not image_catalog["cover"] and output_paths.get("cover"):
            from types import SimpleNamespace
            cover_name = Path(output_paths["cover"]).name
            image_catalog["cover"] = [SimpleNamespace(filename=cover_name)]
            print(f"     [INFO] Backfilled cover from copied assets: {cover_name}")
        
        # Merge filename mappings
        filename_mapping.update(kuchie_filename_mapping)
        
        # Copy spine-derived inline illustrations (authoritative order).
        import shutil
        illust_dir = assets_dir / "illustrations"
        illust_dir.mkdir(parents=True, exist_ok=True)

        copied_spine_illustrations = []
        spine_illustration_originals = {meta["original"] for meta in spine_illustrations}
        for meta in spine_illustrations:
            src_path = extraction.content_dir / meta["image_path"]
            if not src_path.exists():
                continue
            dst_path = illust_dir / meta["file"]
            if not dst_path.exists():
                shutil.copy2(src_path, dst_path)
                copied_spine_illustrations.append(meta["file"])
            # Identity mapping keeps chapter placeholders stable.
            filename_mapping[meta["original"]] = meta["file"]

        # Fallback: copy chapter-referenced images that were not found in spine walk.
        chapter_illustrations_to_copy = (
            chapter_illustrations - spine_kuchie_originals - spine_illustration_originals
        )
        copied_illustrations = []
        filtered_illustrations = 0
        existing_illustrations = {img.filename for img in image_catalog["illustrations"]}
        
        for img_filename in chapter_illustrations_to_copy:
            # Hard-filter excluded assets (gaiji, fan-letter, bookwalker promo, stylized headers).
            if profile_manager.is_excluded_image(img_filename, publisher_name):
                filtered_illustrations += 1
                continue

            # Try to find the image in content_dir
            for images_folder in ['images', 'image', 'Images', 'IMAGES']:
                src_path = extraction.content_dir / images_folder / img_filename
                if src_path.exists():
                    dst_path = illust_dir / img_filename
                    if not dst_path.exists():  # Avoid overwriting
                        shutil.copy2(src_path, dst_path)
                        copied_illustrations.append(img_filename)
                    # Keep mapping as identity (no illustration renaming)
                    filename_mapping[img_filename] = img_filename
                    if img_filename not in existing_illustrations:
                        image_catalog["illustrations"].append(
                            type('obj', (object,), {'filename': img_filename})()
                        )
                        existing_illustrations.add(img_filename)
                    break
        
        if copied_spine_illustrations:
            print(
                f"     Copied {len(copied_spine_illustrations)} spine-derived illustrations to assets"
            )
        if copied_illustrations:
            print(f"     Copied {len(copied_illustrations)} inline illustrations to assets")
        if filtered_illustrations:
            print(f"     Hard-filtered {filtered_illustrations} excluded inline image references")

        # Phase 1.55: Validate real-world references in source chapters (optional, off by default).
        # Keep this after asset copy so a slow/failed validator does not block asset extraction.
        if validate_references:
            print("\n[PHASE 1.55] Validating real-world references...")
            self._validate_references_in_chapters(chapters, source_dir)
        else:
            print("\n[PHASE 1.55] Reference validation skipped (use --ref-validate to enable)")

        # Update illustration references in markdown files
        if filename_mapping:
            print(f"\n[POST-PROCESS] Updating illustration references...")
            self._update_markdown_illustrations(source_dir, filename_mapping)
            print(f"     Updated {len(filename_mapping)} illustration references")

        # Count images
        cover_count = len(image_catalog["cover"])
        kuchie_count = len(spine_kuchie)  # Use spine-based count
        illust_count = len(image_catalog["illustrations"])
        print(f"     Cover: {cover_count}, Kuchie: {kuchie_count}, Illustrations: {illust_count}")
        
        # Build manifest
        print("\n[FINALIZING] Building manifest...")
        manifest = self._build_manifest(
            volume_id=volume_id,
            epub_path=epub_path,
            toc=toc,
            spine=spine,
            chapters=chapters,
            image_catalog=image_catalog,
            spine_kuchie=spine_kuchie,  # Add spine-based kuchie metadata
            source_lang=source_lang,
            target_lang=target_lang,
            recovery_info={
                "used_spine_fallback": use_spine_fallback,
                "recovery_reasons": recovery_reasons,
                "toc_entries": toc_entry_count,
                "spine_content_files": len([i for i in spine.items if i.linear and not i.is_illustration]),
                "toc_coverage": toc_coverage,
                "toc_alignment": toc_alignment,
                "missing_from_toc": sorted(list(missing_files)) if missing_files else []
            },
            volume_acts=volume_acts,
        )

        # Sync manifest filenames with mapped output names
        if filename_mapping:
            print("[POST-PROCESS] Syncing manifest filenames...")
            self._sync_manifest_filenames(manifest, filename_mapping)
            print(f"     Synced {len(filename_mapping)} filename mappings")

        # Save manifest
        manifest_path = work_dir / "manifest.json"
        manifest.save(manifest_path)
        print(f"     Saved: {manifest_path}")

        self._write_context_placeholder(
            work_dir / "context.xml",
            volume_id=volume_id,
            opf_metadata=extract_opf_metadata(extraction.opf_path),
            target_lang=target_lang,
        )
        print(f"     Saved: {work_dir / 'context.xml'}")

        # Save TOC separately
        toc_path = work_dir / "toc.json"
        with open(toc_path, 'w', encoding='utf-8') as f:
            json.dump(toc.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"     Saved: {toc_path}")

        # Verify JP directory and chapter files exist
        jp_dir = work_dir / structure["source_chapters"]
        if not jp_dir.exists():
            raise RuntimeError(f"CRITICAL ERROR: JP directory not created at {jp_dir}")
        
        jp_files = list(jp_dir.glob("CHAPTER_*.md"))
        if len(jp_files) != len(chapters):
            raise RuntimeError(
                f"CRITICAL ERROR: Chapter count mismatch - Expected {len(chapters)} files in JP/, "
                f"found {len(jp_files)}. Files: {[f.name for f in jp_files]}"
            )
        print(f"\n[VERIFICATION] ✓ JP directory verified: {len(jp_files)} chapter files present")

        # Summary
        asset_count = (
            len(manifest.assets.get("illustrations", []))
            + (1 if manifest.assets.get("cover") else 0)
            + len(manifest.assets.get("kuchie", []))
        )
        print(f"\n{'='*60}")
        print("LIBRARIAN COMPLETE")
        print(f"{'='*60}")
        print(f"Volume ID:    {volume_id}")
        print(f"Work Dir:     {work_dir}")
        print(f"Chapters:     {len(chapters)}")
        print(f"Assets:       {asset_count}  (cover + kuchie + illustrations)")
        print(f"")
        print(f"manifest.json — runtime configuration only")
        print(f"context.xml   — raw OPF metadata + 17-block agent placeholder")
        print(f"")
        print(f"Status:       Ready for Phase 1.15 → Title Philosophy")
        print(f"{'='*60}\n")

        return manifest

    def _convert_chapters(
        self,
        content_dir: Path,
        output_dir: Path,
        chapter_order: List[str],
        toc: TableOfContents,
        converter: XHTMLToMarkdownConverter
    ) -> List[ConvertedChapter]:
        """Convert XHTML chapters to markdown files."""
        chapters = []
        titles = {np.content_src: np.label for np in toc.get_flat_list()}

        # Track processed files to avoid duplicates
        processed = set()

        for ref in chapter_order:
            # Handle fragment references (file.xhtml#id)
            filename = ref.split('#')[0]
            if filename in processed:
                continue

            xhtml_path = content_dir / filename
            if not xhtml_path.exists():
                # Try in subdirectories
                for sub in ['text', 'Text', 'xhtml', 'XHTML']:
                    alt_path = content_dir / sub / filename
                    if alt_path.exists():
                        xhtml_path = alt_path
                        break

            if not xhtml_path.exists():
                print(f"     [SKIP] Not found: {filename}")
                continue

            # Skip navigation, cover, and toc files
            lower_name = filename.lower()
            if any(skip in lower_name for skip in ['nav', 'toc', 'cover', 'copyright']):
                print(f"     [SKIP] Navigation/cover: {filename}")
                continue
            if self._is_non_content_xhtml_file(xhtml_path):
                print(f"     [SKIP] Non-content XHTML: {filename}")
                continue

            try:
                # Get title from TOC
                title = titles.get(ref, titles.get(filename, ""))

                # Convert
                chapter = converter.convert_file(xhtml_path, title)

                # Save markdown
                md_filename = self._make_markdown_filename(filename, len(chapters) + 1)
                md_path = output_dir / md_filename

                with open(md_path, 'w', encoding='utf-8') as f:
                    f.write(chapter.content)

                # Update chapter info
                chapter.filename = md_filename
                chapters.append(chapter)
                processed.add(filename)

                print(f"     [OK] {filename} -> {md_filename}")

            except Exception as e:
                print(f"     [FAIL] {filename}: {e}")

        return chapters

    def _convert_chapters_with_spine(
        self,
        content_dir: Path,
        output_dir: Path,
        toc: TableOfContents,
        spine: Spine,
        converter: XHTMLToMarkdownConverter,
        publisher: str = None
    ) -> List[ConvertedChapter]:
        """
        Convert XHTML chapters to markdown, merging content between TOC entries.

        This handles EPUBs where:
        - Illustrations are standalone XHTML files between content files
        - Chapter content is split across multiple XHTML files
        - The spine order differs from the TOC navigation order

        Args:
            content_dir: Directory containing XHTML files
            output_dir: Directory to write markdown files
            toc: Parsed table of contents
            spine: Parsed spine with reading order
            converter: XHTML to Markdown converter
            publisher: Publisher canonical name for exclusion matching

        Returns:
            List of ConvertedChapter objects
        """
        import re
        profile_manager = get_profile_manager()
        chapters = []

        # Build TOC entry map: normalized filename -> (title, TOC entry)
        toc_entries = {}
        for np in toc.get_flat_list():
            # Normalize: xhtml/p-007.xhtml#toc-001 -> p-007.xhtml
            filename = np.content_src.split('#')[0]
            if '/' in filename:
                filename = filename.split('/')[-1]
            toc_entries[filename] = np

        # Build spine item map: normalized filename -> SpineItem
        spine_map = {}
        spine_order = []  # Ordered list of normalized filenames
        for item in spine.items:
            filename = item.href.split('/')[-1] if '/' in item.href else item.href
            spine_map[filename] = item
            spine_order.append(filename)

        # Identify chapter boundaries from TOC
        # A chapter starts at a TOC entry and ends at the next TOC entry
        toc_filenames = list(toc_entries.keys())

        # Skip navigation, cover, colophon entries
        skip_patterns = ['cover', 'toc', 'nav', 'titlepage', 'caution', 'colophon', '998', '999']

        # Load pre-TOC detection config
        pre_toc_config = get_pre_toc_detection_config()
        
        # Detect pre-TOC content (opening hooks before prologue)
        pre_toc_content = []
        if pre_toc_config["enabled"]:
            pre_toc_content = self._detect_pre_toc_content(
                spine_order, toc_filenames, content_dir, skip_patterns, pre_toc_config
            )
        
        # Group spine items into chapters
        chapter_groups = []  # List of (title, [spine_files], is_pre_toc)
        
        # Add pre-TOC content as first chapter if found
        if pre_toc_content:
            chapter_title = pre_toc_config["chapter_title"]
            chapter_groups.append((chapter_title, pre_toc_content, True))  # Mark as pre-TOC
            print(f"     [INFO] Found pre-prologue content: {len(pre_toc_content)} files")
        
        current_chapter = None
        current_files = []

        for filename in spine_order:
            lower_name = filename.lower()

            # Skip navigation/special files
            if any(skip in lower_name for skip in skip_patterns):
                continue
            
            # Skip if already processed as pre-TOC content
            if filename in pre_toc_content:
                continue

            # Check if this file starts a new chapter (is in TOC)
            if filename in toc_entries:
                # Save previous chapter if exists
                if current_chapter is not None and current_files:
                    chapter_groups.append((current_chapter, current_files, False))  # Regular TOC chapter

                # Start new chapter
                current_chapter = toc_entries[filename].label
                current_files = [filename]
            elif current_chapter is not None:
                # Continue current chapter
                current_files.append(filename)

        # Don't forget the last chapter
        if current_chapter is not None and current_files:
            chapter_groups.append((current_chapter, current_files, False))  # Regular TOC chapter

        # Process each chapter group
        for chapter_title, file_list, *is_pre_toc_flag in chapter_groups:
            is_pre_toc_chapter = is_pre_toc_flag[0] if is_pre_toc_flag else False
            try:
                merged_content = []
                all_illustrations = []

                for filename in file_list:
                    spine_item = spine_map.get(filename)
                    xhtml_path = self._find_xhtml_file(content_dir, filename)

                    if xhtml_path is None:
                        print(f"     [SKIP] Not found: {filename}")
                        continue

                    # Check if this is an illustration-only page
                    if spine_item and spine_item.is_illustration:
                        # Extract illustration reference from the file
                        illust_ref = self._extract_illustration_from_file(xhtml_path)
                        if illust_ref:
                            if profile_manager.is_excluded_image(illust_ref, publisher):
                                continue
                            merged_content.append(f"\n[ILLUSTRATION: {illust_ref}]\n")
                            all_illustrations.append(illust_ref)
                        continue

                    # Skip non-story front/end-matter XHTML pages.
                    if self._is_non_content_xhtml_file(xhtml_path):
                        continue

                    # Convert regular content file
                    # Don't include title for continuation files
                    is_first = (filename == file_list[0])
                    chapter = converter.convert_file(
                        xhtml_path,
                        chapter_title if is_first else ""
                    )

                    # For continuation files, strip the auto-detected title
                    content = chapter.content
                    if not is_first and content.startswith('# '):
                        # Remove the first heading line
                        lines = content.split('\n', 1)
                        if len(lines) > 1:
                            content = lines[1].lstrip('\n')

                    merged_content.append(content)
                    all_illustrations.extend(chapter.illustrations)

                # Combine all content
                full_content = '\n\n'.join(merged_content)

                # Clean up excessive newlines
                full_content = re.sub(r'\n{3,}', '\n\n', full_content)

                # Create merged chapter
                word_count = len(re.findall(r'\S+', full_content))
                paragraphs = [p for p in full_content.split('\n\n') if p.strip() and not p.startswith('#')]

                merged_chapter = ConvertedChapter(
                    filename="",  # Will be set below
                    title=chapter_title,
                    content=full_content,
                    illustrations=all_illustrations,
                    word_count=word_count,
                    paragraph_count=len(paragraphs),
                    is_pre_toc_content=is_pre_toc_chapter,
                    source_files=list(file_list),
                    raw_group_index=None,
                    raw_group_title=None,
                    split_strategy=None,
                )

                # Generate filename and save
                first_file = file_list[0]
                md_filename = self._make_markdown_filename(first_file, len(chapters) + 1)
                md_path = output_dir / md_filename

                with open(md_path, 'w', encoding='utf-8') as f:
                    f.write(full_content)

                merged_chapter.filename = md_filename
                chapters.append(merged_chapter)

                files_str = f"{len(file_list)} files" if len(file_list) > 1 else "1 file"
                print(f"     [OK] {chapter_title[:40]}... ({files_str}) -> {md_filename}")

            except Exception as e:
                print(f"     [FAIL] {chapter_title}: {e}")

        return chapters

    def _convert_chapters_from_spine(
        self,
        content_dir: Path,
        output_dir: Path,
        spine: Spine,
        converter: XHTMLToMarkdownConverter,
        publisher: str = None,
        volume_acts: Optional[List[Dict[str, Any]]] = None
    ) -> List[ConvertedChapter]:
        """
        Convert XHTML chapters to markdown using spine order when TOC is minimal/broken.

        This is used for publishers like Hifumi Shobo that have intentionally minimal
        TOCs (only cover + colophon). Instead of relying on TOC entries, this method:
        1. Scans each spine item for content
        2. Detects chapter boundaries by looking for chapter titles in the content
        3. Groups consecutive content files into chapters

        Args:
            content_dir: Directory containing XHTML files
            output_dir: Directory to write markdown files
            spine: Parsed spine with reading order
            converter: XHTML to Markdown converter
            publisher: Publisher name for profile-specific patterns

        Returns:
            List of ConvertedChapter objects
        """
        import re

        chapters = []
        profile_manager = get_profile_manager()
        title_patterns = profile_manager.get_chapter_title_patterns(publisher)
        content_config = profile_manager.get_content_config(publisher)

        # Build spine order list
        spine_order = []
        spine_map = {}
        for item in spine.items:
            filename = item.href.split('/')[-1] if '/' in item.href else item.href
            spine_map[filename] = item
            spine_order.append(filename)

        # Skip patterns for non-content files
        skip_patterns = ['cover', 'toc', 'nav', 'titlepage', 'caution', 'colophon', '998', '999']

        # In multi-act books, keep act>=2 frontmatter image pages inline with
        # the FIRST chapter of that act (not the previous chapter).
        act_boundary_files = set()
        if volume_acts:
            for act in volume_acts:
                act_num = act.get('act_number', 1)
                if act_num < 2:
                    continue
                boundary_indices = act.get('tobira_spine_indices', []) + act.get('kuchie_spine_indices', [])
                for idx in boundary_indices:
                    if 0 <= idx < len(spine.items):
                        boundary_file = spine.items[idx].href.split('/')[-1] if '/' in spine.items[idx].href else spine.items[idx].href
                        act_boundary_files.add(boundary_file)

        # Group files into raw chapter groups based on title scan.
        raw_groups = []  # List[Dict[str, Any]]
        current_title = None
        current_files = []
        chapter_counter = 0
        pending_next_chapter_files = []  # Deferred act-boundary pages
        raw_group_index = 0

        for filename in spine_order:
            lower_name = filename.lower()

            # Skip navigation/special files
            if any(skip in lower_name for skip in skip_patterns):
                continue

            # Multi-act boundary images (e.g., act tobira + fmatter) should be
            # attached to the next chapter, not to the previous one.
            if filename in act_boundary_files:
                pending_next_chapter_files.append(filename)
                continue

            xhtml_path = self._find_xhtml_file(content_dir, filename)
            if xhtml_path is None:
                continue

            spine_item = spine_map.get(filename)
            is_non_content = self._is_non_content_xhtml_file(xhtml_path)

            # Check if this is an illustration-only page
            if spine_item and spine_item.is_illustration:
                # Add to current chapter if we have one
                if current_title is not None:
                    current_files.append(filename)
                continue

            # Check if this file has a chapter title
            detected_title = self._detect_chapter_title_in_file(xhtml_path, title_patterns)

            # Also check if file has meaningful text content
            has_content = self._file_has_text_content(xhtml_path)

            if not has_content:
                if is_non_content:
                    # Do not attach caution/credit/legal pages to chapters.
                    continue
                # Skip files without text content (image pages, etc.)
                # But still track illustration references
                if current_title is not None:
                    current_files.append(filename)
                continue

            # Publishers such as KADOKAWA render the chapter title as a heading
            # ("midashi") image (e.g. m-001.jpg) instead of text, and ship a
            # minimal cover+colophon nav. The text-only detector above is blind
            # to these, so without this signal every chapter after the first
            # collapses into a single spine group (see volume 066c). A content
            # page that *leads* with a heading image is a chapter boundary.
            if not detected_title and self._page_leads_with_heading_image(
                xhtml_path, profile_manager, publisher
            ):
                image_title_boundary = True
            else:
                image_title_boundary = False

            if detected_title or image_title_boundary:
                # Save previous chapter if exists
                if current_title is not None and current_files:
                    raw_groups.append({
                        "title": current_title,
                        "files": list(current_files),
                        "raw_group_index": raw_group_index,
                        "raw_group_title": current_title,
                        "split_strategy": None,
                    })
                    raw_group_index += 1

                # Start new chapter. Image-titled chapters have no extractable
                # text title, so fall back to a numbered placeholder (the real
                # title is filled in later from translated metadata).
                chapter_counter += 1
                current_title = (
                    detected_title
                    or content_config.fallback_chapter_title.format(n=chapter_counter)
                )
                current_files = pending_next_chapter_files + [filename]
                pending_next_chapter_files = []
            elif current_title is None:
                # First content file without explicit title - use fallback
                chapter_counter += 1
                fallback_title = content_config.fallback_chapter_title.format(n=chapter_counter)
                current_title = fallback_title
                current_files = pending_next_chapter_files + [filename]
                pending_next_chapter_files = []
            else:
                # Continue current chapter
                current_files.append(filename)

        # Don't forget the last chapter
        if current_title is not None and current_files:
            raw_groups.append({
                "title": current_title,
                "files": list(current_files),
                "raw_group_index": raw_group_index,
                "raw_group_title": current_title,
                "split_strategy": None,
            })

        # Future-proofing: detect "collapsed" groups in malformed/minimal TOC books
        # and split them using original text page boundaries.
        chapter_groups, split_stats = self._apply_text_page_boundary_split_if_needed(
            raw_groups=raw_groups,
            content_dir=content_dir,
            spine_map=spine_map,
            title_patterns=title_patterns,
            content_config=content_config,
        )
        if split_stats.get("applied"):
            print(
                "     [INFO] Text-page boundary split applied: "
                f"{split_stats.get('raw_groups', 0)} raw groups -> "
                f"{split_stats.get('final_groups', 0)} chapter files"
            )

        # Convert each chapter group to markdown
        split_config = profile_manager.get_chapter_split_config(publisher)
        for idx, group in enumerate(chapter_groups):
            try:
                chapter_title = group.get("title", "")
                file_list = group.get("files", [])
                raw_group_idx = group.get("raw_group_index")
                raw_group_title = group.get("raw_group_title")
                split_strategy = group.get("split_strategy")

                merged_content = []
                all_illustrations = []

                for filename in file_list:
                    spine_item = spine_map.get(filename)
                    xhtml_path = self._find_xhtml_file(content_dir, filename)

                    if xhtml_path is None:
                        continue

                    # Check if this is an illustration-only page
                    if spine_item and spine_item.is_illustration:
                        illust_ref = self._extract_illustration_from_file(xhtml_path)
                        if illust_ref:
                            if profile_manager.is_excluded_image(illust_ref, publisher):
                                continue
                            merged_content.append(f"\n[ILLUSTRATION: {illust_ref}]\n")
                            all_illustrations.append(illust_ref)
                        continue

                    # Convert regular content file
                    is_first = (filename == file_list[0])
                    chapter = converter.convert_file(
                        xhtml_path,
                        chapter_title if is_first else ""
                    )

                    content = chapter.content
                    if not is_first and content.startswith('# '):
                        lines = content.split('\n', 1)
                        if len(lines) > 1:
                            content = lines[1].lstrip('\n')

                    merged_content.append(content)
                    all_illustrations.extend(chapter.illustrations)

                # Combine all content
                full_content = '\n\n'.join(merged_content)
                full_content = re.sub(r'\n{3,}', '\n\n', full_content)

                # Check if chapter splitting is needed (publisher-specific long-chapter handling)
                if split_config and split_config.get("enabled", False):
                    # Check if this chapter exceeds token limit
                    splitter = ContentSplitter(
                        max_tokens=split_config.get("max_tokens_per_chapter", 2000),
                        min_tokens=split_config.get("min_part_tokens", 800),
                        scene_break_patterns=split_config.get("scene_break_patterns")
                    )
                    
                    estimated_tokens = splitter.estimate_tokens(full_content)
                    
                    if estimated_tokens > split_config.get("max_tokens_per_chapter", 2000):
                        # Pre-compute parts so we can show the user a preview
                        parts = splitter.split_chapter(full_content)

                        # Ask user whether to split or keep as single file
                        print()
                        print("  ┌─────────────────────────────────────────────────────────────────┐")
                        print("  │  [TOKEN SPLIT] Oversized chapter detected                       │")
                        print("  └─────────────────────────────────────────────────────────────────┘")
                        print(f"  Chapter : {chapter_title}")
                        print(f"  Tokens  : ~{estimated_tokens} (limit: {split_config['max_tokens_per_chapter']})")
                        print(f"  Parts   : {len(parts)} files if split")
                        print()
                        print("  [1] Split into part files  ← recommended")
                        print("  [2] Keep as single chapter file")
                        print()
                        from src.Deepseek.common.interactive import prompt_or_default
                        _tok_choice = prompt_or_default("  Choose [1/2] (default: 1): ", "1").strip()

                        if _tok_choice != "2":
                            print(f"     [SPLIT] Splitting into {len(parts)} parts")

                            for part in parts:
                                part_title = f"{chapter_title} - Part {part.part_number}"
                                part_filename = self._make_markdown_filename(
                                    file_list[0],
                                    idx + 1,
                                    part_suffix=f"_PART_{part.part_number:02d}"
                                )
                                part_path = output_dir / part_filename

                                with open(part_path, 'w', encoding='utf-8') as f:
                                    f.write(part.content)

                                part_chapter = ConvertedChapter(
                                    filename=part_filename,
                                    title=part_title,
                                    content=part.content,
                                    illustrations=part.illustrations,
                                    word_count=part.word_count,
                                    paragraph_count=len([p for p in part.content.split('\n\n') if p.strip()]),
                                    is_pre_toc_content=False,
                                    source_files=list(file_list),
                                    raw_group_index=raw_group_idx,
                                    raw_group_title=raw_group_title,
                                    split_strategy=split_strategy,
                                )
                                chapters.append(part_chapter)

                                print(f"     [OK] {part_title} (~{part.estimated_tokens} tokens) -> {part_filename}")

                            continue  # Skip single chapter creation
                        else:
                            print(f"     [INFO] Keeping oversized chapter as single file")

                # Create single chapter (default behavior)
                word_count = len(re.findall(r'\S+', full_content))
                paragraphs = [p for p in full_content.split('\n\n') if p.strip() and not p.startswith('#')]

                merged_chapter = ConvertedChapter(
                    filename="",
                    title=chapter_title,
                    content=full_content,
                    illustrations=all_illustrations,
                    word_count=word_count,
                    paragraph_count=len(paragraphs),
                    is_pre_toc_content=False,
                    source_files=list(file_list),
                    raw_group_index=raw_group_idx,
                    raw_group_title=raw_group_title,
                    split_strategy=split_strategy,
                )

                # Generate filename and save
                md_filename = self._make_markdown_filename(file_list[0], idx + 1)
                md_path = output_dir / md_filename

                with open(md_path, 'w', encoding='utf-8') as f:
                    f.write(full_content)

                merged_chapter.filename = md_filename
                chapters.append(merged_chapter)

                files_str = f"{len(file_list)} files" if len(file_list) > 1 else "1 file"
                print(f"     [OK] {chapter_title[:40]}... ({files_str}) -> {md_filename}")

            except Exception as e:
                print(f"     [FAIL] {chapter_title}: {e}")

        return chapters

    def _is_fallback_chapter_title(self, title: str, fallback_template: str) -> bool:
        """Return True when title matches generated fallback chapter naming."""
        import re

        normalized = (title or "").strip()
        if not normalized:
            return True

        # Template-aware fallback check (e.g., "Chapter {n}").
        regex = re.escape(fallback_template).replace(r'\{n\}', r'\d+').replace(r'\{num\}', r'\d+')
        if re.match(rf'^{regex}$', normalized, re.IGNORECASE):
            return True

        # Safety baseline for legacy generated titles.
        return bool(re.match(r'^Chapter\s+\d+$', normalized, re.IGNORECASE))

    def _split_spine_group_on_text_pages(
        self,
        file_list: List[str],
        content_dir: Path,
        spine_map: Dict[str, SpineItem],
        title_patterns: List
    ) -> Dict[str, Any]:
        """
        Split one raw spine group into segments using text XHTML files as boundaries.

        Returns:
            {
              "segments": [{"files": [...], "detected_title": "..."}, ...],
              "text_page_count": int,
              "detected_title_count": int
            }
        """
        segments: List[Dict[str, Any]] = []
        current_segment: Optional[Dict[str, Any]] = None
        leading_non_text: List[str] = []
        text_page_count = 0
        detected_title_count = 0

        for filename in file_list:
            xhtml_path = self._find_xhtml_file(content_dir, filename)
            if xhtml_path is None:
                if current_segment is not None:
                    current_segment["files"].append(filename)
                else:
                    leading_non_text.append(filename)
                continue

            spine_item = spine_map.get(filename)
            if spine_item and spine_item.is_illustration:
                if current_segment is not None:
                    current_segment["files"].append(filename)
                else:
                    leading_non_text.append(filename)
                continue

            is_non_content = self._is_non_content_xhtml_file(xhtml_path)
            has_content = self._file_has_text_content(xhtml_path)
            if not has_content:
                if is_non_content:
                    # Explicitly drop caution/credits/legal metadata pages.
                    continue
                if current_segment is not None:
                    current_segment["files"].append(filename)
                else:
                    leading_non_text.append(filename)
                continue

            detected_title = self._detect_chapter_title_in_file(xhtml_path, title_patterns)
            if detected_title:
                detected_title_count += 1
            text_page_count += 1

            if current_segment is not None:
                segments.append(current_segment)

            segment_files = list(leading_non_text)
            segment_files.append(filename)
            leading_non_text = []
            current_segment = {
                "files": segment_files,
                "detected_title": detected_title,
            }

        if current_segment is not None:
            segments.append(current_segment)

        # If no text pages found, preserve original ordering as one segment.
        if not segments and file_list:
            segments = [{"files": list(file_list), "detected_title": None}]

        return {
            "segments": segments,
            "text_page_count": text_page_count,
            "detected_title_count": detected_title_count,
        }

    def _apply_text_page_boundary_split_if_needed(
        self,
        raw_groups: List[Dict[str, Any]],
        content_dir: Path,
        spine_map: Dict[str, SpineItem],
        title_patterns: List,
        content_config
    ) -> tuple:
        """
        Detect collapsed spine groups and split by original text-page boundaries.

        Trigger heuristic:
        - Raw group title is generated fallback (e.g., "Chapter 1")
        - Group contains many text pages
        - Very few explicit chapter markers inside the group
        """
        if not raw_groups:
            return raw_groups, {
                "applied": False,
                "raw_groups": 0,
                "final_groups": 0,
            }

        rebuilt_groups: List[Dict[str, Any]] = []
        applied = False

        for raw_group in raw_groups:
            split_result = self._split_spine_group_on_text_pages(
                file_list=raw_group.get("files", []),
                content_dir=content_dir,
                spine_map=spine_map,
                title_patterns=title_patterns,
            )
            segments = split_result["segments"]
            text_page_count = split_result["text_page_count"]
            detected_title_count = split_result["detected_title_count"]
            base_title = raw_group.get("title", "")

            should_split = (
                len(segments) >= 2
                and text_page_count >= 4
                and detected_title_count <= 1
                and self._is_fallback_chapter_title(base_title, content_config.fallback_chapter_title)
            )

            if should_split:
                applied = True
                for segment in segments:
                    rebuilt_groups.append({
                        "title": segment.get("detected_title") or base_title,
                        "files": list(segment.get("files", [])),
                        "raw_group_index": raw_group.get("raw_group_index"),
                        "raw_group_title": raw_group.get("raw_group_title") or base_title,
                        "split_strategy": "text_page_boundary",
                    })
            else:
                rebuilt_groups.append({
                    "title": base_title,
                    "files": list(raw_group.get("files", [])),
                    "raw_group_index": raw_group.get("raw_group_index"),
                    "raw_group_title": raw_group.get("raw_group_title") or base_title,
                    "split_strategy": raw_group.get("split_strategy"),
                })

        if applied:
            # Renumber all generated fallback titles after split.
            fallback_counter = 0
            for group in rebuilt_groups:
                title = group.get("title", "")
                if self._is_fallback_chapter_title(title, content_config.fallback_chapter_title):
                    fallback_counter += 1
                    group["title"] = content_config.fallback_chapter_title.format(n=fallback_counter)

        return rebuilt_groups, {
            "applied": applied,
            "raw_groups": len(raw_groups),
            "final_groups": len(rebuilt_groups),
        }

    def _page_leads_with_heading_image(
        self,
        xhtml_path: Path,
        profile_manager,
        publisher: Optional[str],
    ) -> bool:
        """
        Return True if a content page begins with a chapter-heading image.

        Some publishers (notably KADOKAWA) render each chapter's title as a
        "midashi" (見出し / heading) image such as m-001.jpg rather than as text,
        and ship a minimal cover+colophon navigation. The text-only detector in
        ``_detect_chapter_title_in_file`` cannot see these titles, so image-titled
        chapters would otherwise collapse into a single spine group. Treating a
        page that *leads* with a recognised heading image as a chapter boundary
        restores correct chapter separation at extraction time.

        A page "leads with" a heading image when the first ``<img>`` inside
        ``<body>`` matches a heading-image convention and no meaningful prose
        precedes it (guarding against chapters that merely open with an inline
        illustration).
        """
        from bs4 import BeautifulSoup

        try:
            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'xml')
            body = soup.find('body')
            if body is None:
                return False

            # Walk the body in reading order, accumulating any text that appears
            # before the first image.
            preceding_text: List[str] = []
            for node in body.descendants:
                node_name = getattr(node, "name", None)
                if node_name == "img":
                    if ''.join(preceding_text).strip():
                        # Real prose precedes the first image -> inline
                        # illustration, not a chapter heading.
                        return False
                    src = (node.get("src") or "").split('/')[-1].strip()
                    return profile_manager.is_chapter_heading_image(src, publisher)
                if node_name is None:
                    # NavigableString / Comment: only count visible text.
                    text = str(node)
                    if text.strip():
                        preceding_text.append(text)

            return False

        except Exception:
            return False

    def _detect_chapter_title_in_file(
        self,
        xhtml_path: Path,
        title_patterns: List
    ) -> Optional[str]:
        """
        Detect chapter title from XHTML file content.

        Looks for headings or text matching chapter title patterns.

        Args:
            xhtml_path: Path to XHTML file
            title_patterns: List of compiled regex patterns for chapter titles

        Returns:
            Detected chapter title or None
        """
        from bs4 import BeautifulSoup

        try:
            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'xml')
            body = soup.find('body')

            if not body:
                return None

            # Check headings first (h1, h2, h3)
            for tag in ['h1', 'h2', 'h3']:
                heading = body.find(tag)
                if heading:
                    text = heading.get_text(strip=True)
                    if text and len(text) > 0 and len(text) < 100:
                        # Check if it matches a chapter pattern
                        for pattern in title_patterns:
                            if pattern.search(text):
                                return text
                        # Also check if it looks like a title (not just a number)
                        if len(text) > 2:
                            return text

            # Check first few paragraphs for chapter markers
            paragraphs = body.find_all('p', limit=5)
            for p in paragraphs:
                text = p.get_text(strip=True)
                if not text:
                    continue

                for pattern in title_patterns:
                    match = pattern.search(text)
                    if match:
                        # Return just the matched portion if it's a clear title
                        matched_text = match.group(0)
                        # If the match is at the start, use the full paragraph as title
                        if text.startswith(matched_text):
                            return text if len(text) < 50 else matched_text
                        return matched_text

            return None

        except Exception:
            return None

    def _is_non_content_body(self, body, text: str) -> bool:
        """
        Identify non-story XHTML pages (caution/credits/legal/colophon).
        """
        if body is None:
            return False

        body_classes = body.get('class', [])
        if isinstance(body_classes, str):
            class_set = {c.strip().lower() for c in body_classes.split() if c.strip()}
        elif isinstance(body_classes, list):
            class_set = {str(c).strip().lower() for c in body_classes if str(c).strip()}
        else:
            class_set = set()

        if class_set.intersection(self.NON_CONTENT_BODY_CLASSES):
            return True

        normalized_text = ''.join((text or '').split())
        if not normalized_text:
            return False

        for marker_a, marker_b in self.NON_CONTENT_TEXT_PAIRS:
            if marker_a in normalized_text and marker_b in normalized_text:
                return True

        return False

    def _is_non_content_xhtml_file(self, xhtml_path: Path) -> bool:
        """Classify a specific XHTML file as non-content front/end matter."""
        from bs4 import BeautifulSoup

        try:
            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()
            soup = BeautifulSoup(content, 'xml')
            body = soup.find('body')
            if not body:
                return False
            text = body.get_text(strip=True)
            return self._is_non_content_body(body, text)
        except Exception:
            return False

    def _file_has_text_content(self, xhtml_path: Path) -> bool:
        """
        Check if XHTML file has meaningful text content (not just images).

        Args:
            xhtml_path: Path to XHTML file

        Returns:
            True if file has text content
        """
        from bs4 import BeautifulSoup

        try:
            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'xml')
            body = soup.find('body')

            if not body:
                return False

            # Check if body is just an SVG/image wrapper
            if body.find('svg') and not body.get_text(strip=True):
                return False

            # Get text content
            text = body.get_text(strip=True)

            # Skip non-story boilerplate pages (caution/credits/colophon/legal notices).
            if self._is_non_content_body(body, text):
                return False

            # Need at least some text
            return len(text) > 20

        except Exception:
            return False

    def _apply_heading_split(
        self,
        chapters: List[ConvertedChapter],
        output_dir: Path,
        heading_patterns: List[str]
    ) -> List[ConvertedChapter]:
        """
        Apply Kodansha-style heading-based chapter splitting.
        
        When Kodansha EPUBs merge all content into one/few files with ### N markers,
        this method splits them into proper chapters.
        
        Args:
            chapters: List of converted chapters (may have merged content)
            output_dir: Directory containing markdown files
            heading_patterns: Regex patterns for heading detection
            
        Returns:
            New list of ConvertedChapter objects after splitting
        """
        import re
        
        # Create splitter with publisher-specific patterns
        splitter = KodanshaSplitter(heading_patterns=heading_patterns)
        
        # First pass: identify which chapters need splitting and calculate offsets
        split_info = []  # List of (chapter_index, num_split_chapters)
        total_offset = 0
        
        for idx, chapter in enumerate(chapters):
            chapter_path = output_dir / chapter.filename
            
            if not chapter_path.exists():
                continue
            
            content = chapter_path.read_text(encoding='utf-8')
            
            if splitter.should_split(content, min_chapters=2):
                num_chapters = splitter.detect_chapters(content)
                split_info.append((idx, num_chapters))
                # Offset is (new chapters - 1) because we're replacing 1 chapter with N
                total_offset += num_chapters - 1
        
        if not split_info:
            return chapters  # Nothing to split
        
        # Second pass: renumber subsequent chapters to make room
        # We need to rename files from the end to avoid conflicts
        for split_idx, num_new in reversed(split_info):
            # Chapters after split_idx need to be renumbered
            chapters_to_rename = []
            for i in range(len(chapters) - 1, split_idx, -1):
                old_path = output_dir / chapters[i].filename
                if old_path.exists():
                    # Calculate new chapter number
                    old_num = i + 1  # 1-indexed
                    # Add offset for all splits before this point
                    offset = sum(n - 1 for idx, n in split_info if idx < i)
                    new_num = old_num + offset + (num_new - 1)
                    
                    new_filename = f"CHAPTER_{new_num:02d}.md"
                    new_path = output_dir / new_filename
                    
                    chapters_to_rename.append((old_path, new_path, chapters[i], new_filename))
            
            # Rename from highest to lowest to avoid conflicts
            for old_path, new_path, chapter_obj, new_filename in chapters_to_rename:
                if old_path != new_path:
                    old_path.rename(new_path)
                    chapter_obj.filename = new_filename
        
        # Third pass: perform the actual splits
        new_chapters = []
        current_chapter_num = 1
        
        for idx, chapter in enumerate(chapters):
            chapter_path = output_dir / chapter.filename
            
            if not chapter_path.exists():
                new_chapters.append(chapter)
                current_chapter_num += 1
                continue
            
            content = chapter_path.read_text(encoding='utf-8')
            
            # Check if this chapter needs splitting
            if not splitter.should_split(content, min_chapters=2):
                # Update filename to current number (may have shifted)
                new_filename = f"CHAPTER_{current_chapter_num:02d}.md"
                if chapter.filename != new_filename:
                    old_path = output_dir / chapter.filename
                    new_path = output_dir / new_filename
                    if old_path.exists() and old_path != new_path:
                        old_path.rename(new_path)
                    chapter.filename = new_filename
                new_chapters.append(chapter)
                current_chapter_num += 1
                continue
            
            # Split the chapter
            print(f"     [KODANSHA SPLIT] {chapter.filename} has multiple chapter markers")
            split_chapters = splitter.split_chapters(content, base_chapter_num=current_chapter_num)
            
            if not split_chapters:
                new_chapters.append(chapter)
                current_chapter_num += 1
                continue
            
            # Backup original file BEFORE creating new files (to avoid overwrite)
            backup_path = chapter_path.with_suffix('.md.backup')
            chapter_path.rename(backup_path)
            print(f"     [BACKUP] {chapter.filename} -> {backup_path.name}")
            
            # Create new chapter files
            for split_ch in split_chapters:
                new_filename = f"CHAPTER_{current_chapter_num:02d}.md"
                new_path = output_dir / new_filename
                
                # Write content with proper header
                with open(new_path, 'w', encoding='utf-8') as f:
                    f.write(split_ch.content)
                
                # Create ConvertedChapter object
                new_chapter = ConvertedChapter(
                    filename=new_filename,
                    title=split_ch.title,
                    content=split_ch.content,
                    illustrations=split_ch.illustrations,
                    word_count=split_ch.word_count,
                    paragraph_count=len([p for p in split_ch.content.split('\n\n') if p.strip()]),
                    is_pre_toc_content=False,
                    source_files=list(getattr(chapter, "source_files", []) or []),
                    raw_group_index=getattr(chapter, "raw_group_index", None),
                    raw_group_title=getattr(chapter, "raw_group_title", None),
                    split_strategy=getattr(chapter, "split_strategy", None),
                )
                new_chapters.append(new_chapter)
                print(f"       -> {new_filename}: '{split_ch.title}' ({split_ch.word_count} words)")
                current_chapter_num += 1
        
        return new_chapters

    def _split_inline_afterword_chapters(
        self,
        chapters: List[ConvertedChapter],
        output_dir: Path,
    ) -> Tuple[List[ConvertedChapter], int]:
        """Split inline afterword sections into standalone chapters when detected."""
        heading_pattern = re.compile(
            r'^\s*#{1,6}\s*(あとがき|後書き|後書|後記|Afterword|AFTERWORD)\s*$',
            re.IGNORECASE,
        )
        illustration_pattern = re.compile(r'!\[illustration\]\(([^)]+)\)')

        def _extract_illustrations(markdown_text: str) -> List[str]:
            return [m.group(1) for m in illustration_pattern.finditer(markdown_text or "")]

        split_count = 0
        rewritten: List[ConvertedChapter] = []

        for chapter in chapters:
            title = str(getattr(chapter, "title", "") or "")
            chapter_id = self._generate_chapter_id(title, 1, getattr(chapter, "filename", ""))
            if chapter_id == "afterword":
                rewritten.append(chapter)
                continue

            chapter_path = output_dir / chapter.filename
            if not chapter_path.exists():
                rewritten.append(chapter)
                continue

            content = chapter_path.read_text(encoding='utf-8')
            lines = content.splitlines()

            split_line_index = None
            for idx, line in enumerate(lines):
                if idx == 0:
                    continue
                if heading_pattern.match(line):
                    split_line_index = idx
                    break

            if split_line_index is None:
                rewritten.append(chapter)
                continue

            before_text = "\n".join(lines[:split_line_index]).strip()
            after_text = "\n".join(lines[split_line_index:]).strip()
            if not before_text or not after_text:
                rewritten.append(chapter)
                continue

            # Rewrite current chapter as pre-afterword section.
            with open(chapter_path, 'w', encoding='utf-8') as f:
                f.write(before_text + "\n")

            chapter.content = before_text
            chapter.word_count = len(re.findall(r'\S+', before_text))
            chapter.paragraph_count = len([p for p in before_text.split('\n\n') if p.strip() and not p.startswith('#')])
            chapter.illustrations = _extract_illustrations(before_text)
            rewritten.append(chapter)

            # Create standalone afterword chapter from inline heading onward.
            afterword_filename = chapter.filename.replace('.md', '_AFTERWORD.md')
            afterword_path = output_dir / afterword_filename
            dedupe_idx = 2
            while afterword_path.exists():
                afterword_filename = chapter.filename.replace('.md', f'_AFTERWORD_{dedupe_idx}.md')
                afterword_path = output_dir / afterword_filename
                dedupe_idx += 1

            with open(afterword_path, 'w', encoding='utf-8') as f:
                f.write(after_text + "\n")

            marker_match = heading_pattern.match(lines[split_line_index] or "")
            marker_title = marker_match.group(1) if marker_match else "Afterword"

            rewritten.append(
                ConvertedChapter(
                    filename=afterword_filename,
                    title=marker_title,
                    content=after_text,
                    illustrations=_extract_illustrations(after_text),
                    word_count=len(re.findall(r'\S+', after_text)),
                    paragraph_count=len([p for p in after_text.split('\n\n') if p.strip() and not p.startswith('#')]),
                    is_pre_toc_content=getattr(chapter, 'is_pre_toc_content', False),
                    source_files=list(getattr(chapter, 'source_files', []) or []),
                    raw_group_index=getattr(chapter, 'raw_group_index', None),
                    raw_group_title=getattr(chapter, 'raw_group_title', None),
                    split_strategy=getattr(chapter, 'split_strategy', None),
                )
            )
            split_count += 1

        return rewritten, split_count

    def _detect_pre_toc_content(
        self,
        spine_order: List[str],
        toc_filenames: List[str],
        content_dir: Path,
        skip_patterns: List[str],
        config: Dict[str, Any]
    ) -> List[str]:
        """
        Detect content files that appear before the first TOC entry.
        
        These are often opening hooks or narrative setup that don't appear
        in the table of contents but are crucial to the story.
        
        This is EXCEPTIONALLY RARE - see config.py for settings.
        
        Args:
            spine_order: Ordered list of spine filenames
            toc_filenames: List of filenames that are in TOC
            content_dir: Directory containing XHTML files
            skip_patterns: Patterns for files to skip
            config: Pre-TOC detection configuration
            
        Returns:
            List of filenames for pre-TOC content
        """
        from bs4 import BeautifulSoup
        
        if not toc_filenames:
            return []
        
        # Find first TOC entry in spine order
        first_toc_index = None
        for idx, filename in enumerate(spine_order):
            if filename in toc_filenames:
                first_toc_index = idx
                break
        
        if first_toc_index is None or first_toc_index == 0:
            return []
        
        # Check files before first TOC entry
        pre_toc_content = []
        for filename in spine_order[:first_toc_index]:
            lower_name = filename.lower()
            
            # Skip navigation/special files
            if any(skip in lower_name for skip in skip_patterns):
                continue
            
            # Skip patterns from config (fmatter, kuchie, etc.)
            if any(pattern in lower_name for pattern in config["exclude_patterns"]):
                continue
            
            # Check if file has actual text content
            xhtml_path = self._find_xhtml_file(content_dir, filename)
            if xhtml_path and self._has_story_content(xhtml_path, config):
                pre_toc_content.append(filename)
        
        return pre_toc_content
    
    def _has_story_content(self, xhtml_path: Path, config: Dict[str, Any]) -> bool:
        """
        Check if XHTML file contains story text (not just images/credits).
        
        Args:
            xhtml_path: Path to XHTML file
            config: Pre-TOC detection configuration
            
        Returns:
            True if file has meaningful story content
        """
        from bs4 import BeautifulSoup
        
        try:
            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            soup = BeautifulSoup(content, 'xml')
            body = soup.find('body')
            
            if not body:
                return False
            
            # Check if it's just an SVG image wrapper
            if body.find('svg'):
                text = body.get_text(strip=True)
                if not text:
                    return False  # Image-only page
            
            # Get text content
            text = body.get_text(strip=True)

            # Skip non-story boilerplate pages (caution/credits/colophon/legal notices).
            if self._is_non_content_body(body, text):
                return False

            # Check if this is a TOC page (目次 = Table of Contents in Japanese)
            # TOC pages have "目次" and multiple internal chapter links
            if '目次' in text or 'Contents' in text or 'Table of Contents' in text:
                # Count internal chapter links (href to other xhtml files)
                anchors = body.find_all('a')
                internal_links = [a for a in anchors if a.get('href', '').endswith('.xhtml') or '#' in a.get('href', '')]

                # If we have 3+ internal chapter links, it's almost certainly a TOC
                if len(internal_links) >= 3:
                    return False  # This is a TOC page, not story content

            # Get markers from config
            credit_markers = config["credit_markers"]
            min_sentences = config["min_sentences_after_credits"]
            min_length = config["min_text_length"]
            dialog_markers = config["story_markers"]["dialog"]
            pronouns = config["story_markers"]["pronouns"]

            # Filter out credit lines and short metadata
            if any(marker in text for marker in credit_markers):
                # Check if there's substantial text beyond credits
                sentences = [s for s in text.split('。') if len(s) > 10]
                return len(sentences) >= min_sentences
            
            # Check for dialog or narrative
            has_dialog = any(marker in text for marker in dialog_markers)
            has_narrative = any(pronoun in text for pronoun in pronouns)
            
            # If has dialog or narrative, it's story content
            if has_dialog or has_narrative:
                return True
            
            # Otherwise, check length
            return len(text) >= min_length
            
        except Exception as e:
            return False
    
    def _find_xhtml_file(self, content_dir: Path, filename: str) -> Optional[Path]:
        """Find XHTML file in content directory or subdirectories."""
        # Direct path
        direct = content_dir / filename
        if direct.exists():
            return direct

        # Try common subdirectories
        for sub in ['xhtml', 'XHTML', 'text', 'Text', 'OEBPS', 'OPS']:
            path = content_dir / sub / filename
            if path.exists():
                return path

        # Search recursively
        for path in content_dir.rglob(filename):
            return path

        return None

    def _extract_illustration_from_file(self, xhtml_path: Path) -> Optional[str]:
        """Extract illustration filename from an illustration-only XHTML file."""
        try:
            from bs4 import BeautifulSoup

            with open(xhtml_path, 'r', encoding='utf-8') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'xml')

            # Check for SVG with image
            svg = soup.find('svg')
            if svg:
                image = svg.find('image')
                if image:
                    # BeautifulSoup stores xlink:href as literal 'xlink:href' key
                    href = (
                        image.get('href') or
                        image.get('xlink:href') or  # BeautifulSoup literal key
                        image.get('{http://www.w3.org/1999/xlink}href', '')
                    )
                    if href:
                        return Path(href).name

            # Check for img tag
            img = soup.find('img')
            if img:
                src = img.get('src', '')
                if src:
                    return Path(src).name

        except Exception:
            pass

        return None

    def _make_markdown_filename(self, xhtml_filename: str, index: int, part_suffix: str = "") -> str:
        """
        Generate clean markdown filename using sequential chapter index.
        
        IMPORTANT: Uses `index` parameter (sequential chapter count) instead of
        EPUB page numbers to avoid off-by-N errors when EPUBs have pre-content
        pages (credits, kuchie, etc.) that aren't chapters.
        
        Args:
            xhtml_filename: Original XHTML filename (e.g., p-003.xhtml)
            index: Sequential chapter number (1-based)
            part_suffix: Optional suffix for chapter parts (e.g., "_PART_01")
            
        Returns:
            Markdown filename (e.g., CHAPTER_01.md or CHAPTER_01_PART_01.md)
        """
        # Always use sequential index for consistent chapter numbering
        # This avoids issues where p-003.xhtml is the first chapter but
        # would incorrectly become CHAPTER_03.md instead of CHAPTER_01.md
        return f"CHAPTER_{index:02d}{part_suffix}.md"

    def _generate_chapter_id(self, title: str, index: int, filename: str = "") -> str:
        """
        Generate semantic chapter ID from title.
        
        Detects chapter type (interlude, epilogue, etc.) and generates
        appropriate ID for manifest consistency.
        
        Args:
            title: Chapter title from TOC
            index: Sequential index (fallback)
            filename: Source filename (optional)
            
        Returns:
            Semantic chapter ID (e.g., 'interlude_01', 'epilogue', 'chapter_05')
        """
        import re
        
        # Priority: Filename-based ID (to ensure stability)
        if filename:
            # Matches: CHAPTER_01.md, 01_chapter.md
            match = re.search(r'CHAPTER_(\d+)', filename, re.IGNORECASE)
            if match:
                return f"chapter_{int(match.group(1)):02d}"

        title_lower = title.lower()
        
        # Detect interludes
        if 'interlude' in title_lower or '間章' in title:
            match = re.search(r'(\d+)', title)
            num = int(match.group(1)) if match else 1
            return f"interlude_{num:02d}"
        
        # Detect epilogue (English, katakana, kanji 終章)
        if 'epilogue' in title_lower or 'エピローグ' in title or '終章' in title:
            return "epilogue"

        # Detect prologue (English, katakana, kanji 序章)
        if 'prologue' in title_lower or 'プロローグ' in title or '序章' in title:
            return "prologue"
        
        # Detect afterword
        if (
            'afterword' in title_lower
            or 'あとがき' in title
            or '後書き' in title
            or '後書' in title
            or '後記' in title
            or '跋' in title
        ):
            return "afterword"
        
        # Detect colophon
        if 'colophon' in title_lower or '奥付' in title:
            return "colophon"
        
        # Extract chapter number from title
        # Matches: "Chapter 5", "第5章", "5", etc.
        match = re.search(r'chapter\s*(\d+)', title_lower)
        if match:
            num = int(match.group(1))
            return f"chapter_{num:02d}"
        
        # Japanese chapter pattern: 第N章
        match = re.search(r'第(\d+)章', title)
        if match:
            num = int(match.group(1))
            return f"chapter_{num:02d}"
        
        # Numeric pattern at start
        match = re.match(r'^(\d+)', title)
        if match:
            num = int(match.group(1))
            return f"chapter_{num:02d}"
        
        # Fallback: sequential numbering
        return f"chapter_{index+1:02d}"

    def _update_markdown_illustrations(self, jp_dir: Path, filename_mapping: Dict[str, str]):
        """
        Update illustration references in markdown files using mapped filenames.
        
        This primarily handles cover/kuchie remapping. Illustration entries now
        keep original EPUB filenames, so most illustration mappings are identity.
        
        Also removes illustration placeholders for filtered files (e.g., m*.jpg stylized titles)
        that were excluded by the image pattern filter.
        
        Args:
            jp_dir: Directory containing Japanese markdown files
            filename_mapping: Dict mapping original filename -> normalized filename
        """
        import re
        from src.Deepseek.common.config import ILLUSTRATION_PLACEHOLDER_PATTERN
        
        updated_count = 0
        removed_count = 0
        
        for md_file in jp_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding='utf-8')
                original_content = content
                
                # Find all illustration placeholders
                for match in re.finditer(ILLUSTRATION_PLACEHOLDER_PATTERN, content):
                    original_filename = match.group(1)
                    old_placeholder = match.group(0)
                    
                    # Check if we have a mapping for this filename
                    if original_filename in filename_mapping:
                        # File was processed - replace with mapped output name
                        mapped_filename = filename_mapping[original_filename]
                        new_placeholder = f"[ILLUSTRATION: {mapped_filename}]"
                        content = content.replace(old_placeholder, new_placeholder)
                        updated_count += 1
                    else:
                        # File was filtered out (e.g., m*.jpg stylized titles) - remove placeholder
                        # Remove the placeholder and any surrounding blank lines
                        content = content.replace(f"\n{old_placeholder}\n", "\n")
                        content = content.replace(old_placeholder, "")
                        removed_count += 1
                
                # Write back if changed
                if content != original_content:
                    md_file.write_text(content, encoding='utf-8')
                    
            except Exception as e:
                print(f"     [WARN] Failed to update illustrations in {md_file.name}: {e}")

        if removed_count > 0:
            print(f"     Removed {removed_count} filtered illustration placeholders (stylized titles, etc.)")
        
        return updated_count

    def _sync_manifest_filenames(self, manifest: Manifest, filename_mapping: Dict[str, str]):
        """
        Sync manifest asset filenames with mapped output names.

        Updates both the global assets section and per-chapter illustration lists
        to use output filenames instead of source EPUB filenames.

        Args:
            manifest: Manifest object to update (modified in-place)
            filename_mapping: Dict mapping original filename -> normalized filename
        """
        if not filename_mapping:
            return

        # Update cover filename if mapped.
        # Guardrail: keep explicit cover.* names from being remapped to kuchie/k###.
        # Spine kuchie extraction can include original cover assets for traceability,
        # but that must not overwrite canonical manifest cover when cover.jpg exists.
        if "cover" in manifest.assets and manifest.assets["cover"]:
            current_cover = manifest.assets["cover"]
            mapped_cover = filename_mapping.get(current_cover, current_cover)
            if (
                re.search(r"cover", str(current_cover), re.IGNORECASE)
                and re.match(r"^(?:kuchie-\d{3}|k\d{3})", str(mapped_cover), re.IGNORECASE)
            ):
                mapped_cover = current_cover
            manifest.assets["cover"] = mapped_cover

        # Update global assets
        # Kuchie now contains metadata dicts, so update the 'file' field
        if "kuchie" in manifest.assets and isinstance(manifest.assets["kuchie"], list):
            # Check if kuchie is list of dicts (new format) or strings (old format)
            if manifest.assets["kuchie"] and isinstance(manifest.assets["kuchie"][0], dict):
                # New format: list of dicts with metadata
                for kuchie_meta in manifest.assets["kuchie"]:
                    if "file" in kuchie_meta:
                        # Already normalized during extraction, no mapping needed
                        pass
            else:
                # Old format: list of strings (fallback for compatibility)
                manifest.assets["kuchie"] = [
                    filename_mapping.get(f, f) for f in manifest.assets["kuchie"]
                ]

        if "illustrations" in manifest.assets:
            manifest.assets["illustrations"] = [
                filename_mapping.get(f, f) for f in manifest.assets["illustrations"]
            ]

        # Update per-chapter illustration lists
        for chapter in manifest.chapters:
            if "illustrations" in chapter:
                chapter["illustrations"] = [
                    filename_mapping.get(f, f) for f in chapter["illustrations"]
                ]

    def _extract_kuchie_from_spine(
        self,
        spine: Spine,
        content_dir: Path,
        publisher_profile: Optional[PublisherProfile],
        volume_acts: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Extract kuchie (color plates) from spine order - THE AUTHORITATIVE SOURCE.
        
        This replaces pattern-based kuchie detection with spine-based extraction.
        The OPF spine defines the canonical reading order, which we preserve here.
        
        Supports multi-act EPUBs: when volume_acts is provided, extracts kuchie
        for ALL acts (front matter of each act), not just the initial front matter.
        
        Why spine-based?
        - OPF spine is the authoritative source of reading order
        - Pattern matching breaks on non-sequential filenames (P000a→P003)
        - Publisher-agnostic: works for any EPUB structure
        - Preserves traceability: original filename + spine position
        
        Args:
            spine: Parsed spine with reading order
            content_dir: Extracted EPUB content directory
            publisher_profile: Optional publisher profile for validation
            volume_acts: Optional list of act metadata from _detect_volume_acts.
                        If provided, extracts kuchie for all acts.
            
        Returns:
            List of kuchie dicts with metadata: 
            [{"file": "kuchie-001.jpg", "original": "P000a.jpg", "spine_index": 2, "act": 1}, ...]
        """
        # Multi-act dispatch: extract kuchie for each act's front matter region
        if volume_acts:
            return self._extract_kuchie_multi_act(
                spine,
                content_dir,
                volume_acts,
                publisher_profile=publisher_profile,
            )
        
        from lxml import etree
        
        kuchie_list = []
        chapter_started = False
        fmatter_section_seen = False  # Track whether we've entered explicit fmatter-* section
        
        print(f"     Scanning {len(spine.items)} spine items for kuchie pages (front matter only)...")
        
        # Iterate through spine in reading order
        for idx, spine_item in enumerate(spine.items):
            # Stop kuchie extraction once we hit chapter content
            # Kuchi-e are ONLY in the front matter (before chapters start)
            if chapter_started:
                break
            
            # Skip non-linear items
            if not spine_item.linear:
                continue
                
            # Only process XHTML files
            if not spine_item.href.endswith(('.xhtml', '.html', '.htm')):
                continue
                
            xhtml_path = content_dir / spine_item.href
            if not xhtml_path.exists():
                continue

            is_frontmatter_page = self._is_frontmatter_spine_page(spine_item)
            
            # Detect whether current page is explicitly an fmatter-* page
            idref_lower = str(getattr(spine_item, "idref", "") or "").lower()
            href_lower = str(getattr(spine_item, "href", "") or "").lower()
            is_fmatter_page = "fmatter" in idref_lower or "fmatter" in href_lower
            
            if is_fmatter_page:
                fmatter_section_seen = True
            
            # Boundary guard: once we've exited the explicit fmatter-* section,
            # encountering a TOC/caution/nav/colophon page signals end of kuchie zone.
            # These structural pages always follow colorplates in Japanese LN EPUBs.
            if fmatter_section_seen and is_frontmatter_page and not is_fmatter_page:
                chapter_started = True
                break
            
            # Quick size check: kuchie pages are typically small (< 5KB XHTML wrapper)
            # Chapter content files are much larger (10KB+)
            file_size = xhtml_path.stat().st_size
            if file_size > 10000 and not is_frontmatter_page:  # 10KB threshold
                # This is likely chapter content - stop extracting kuchie
                chapter_started = True
                break
            
            # Check if this is an image-only page (kuchie candidate)
            try:
                tree = etree.parse(str(xhtml_path))
                root = tree.getroot()
                
                # Find body element
                body = root.find(".//{http://www.w3.org/1999/xhtml}body")
                if body is None:
                    body = root.find(".//body")
                
                if body is None:
                    continue
                
                # Extract text content (excluding <img> alt text)
                # Optimized: only get first 200 chars worth of text to check
                text_parts = []
                char_count = 0
                for element in body.iter():
                    if char_count > 200:  # Stop early if we already have enough text
                        break
                    if element.tag.endswith(('img', 'image')):
                        continue
                    if element.text:
                        text_parts.append(element.text.strip())
                        char_count += len(element.text.strip())
                    if element.tail:
                        text_parts.append(element.tail.strip())
                        char_count += len(element.tail.strip())
                
                text_content = ' '.join(text_parts).strip()
                
                # Check if this is substantial chapter content (not kuchie)
                # If we encounter a page with >200 chars of text, chapters have started
                if len(text_content) > 200 and not is_frontmatter_page:
                    chapter_started = True
                    break
                
                # Find image references
                img_elements = body.findall(".//{http://www.w3.org/1999/xhtml}img")
                if not img_elements:
                    img_elements = body.findall(".//img")
                
                # Also check for SVG images
                svg_images = body.findall(".//{http://www.w3.org/2000/svg}image")
                if svg_images:
                    img_elements.extend(svg_images)
                
                # Kuchie detection: image-only page (minimal/no text)
                # Allow up to 50 chars for titles/labels/whitespace
                if img_elements and len(text_content) < 50:
                    # Extract image filename
                    img_src = None
                    for img in img_elements:
                        src = img.get('src') or img.get('{http://www.w3.org/1999/xlink}href')
                        if src:
                            # Resolve relative path from XHTML location
                            xhtml_dir = Path(spine_item.href).parent
                            img_path = (xhtml_dir / src).as_posix()
                            
                            # Normalize path (remove ../)
                            img_path_parts = []
                            for part in img_path.split('/'):
                                if part == '..':
                                    if img_path_parts:
                                        img_path_parts.pop()
                                elif part and part != '.':
                                    img_path_parts.append(part)
                            img_src = '/'.join(img_path_parts)
                            break
                    
                    if img_src:
                        # Title-page images are usually chapter-opening/branding art,
                        # not frontmatter color-plate kuchie.
                        idref_lower = str(getattr(spine_item, "idref", "") or "").lower()
                        href_lower = str(getattr(spine_item, "href", "") or "").lower()
                        if "titlepage" in idref_lower or "titlepage" in href_lower:
                            continue

                        # Extract original image filename (without path)
                        original_filename = Path(img_src).name
                        
                        # Skip known non-kuchie patterns
                        # - white.jpg: blank separator pages
                        # - logo.png/publisher logo: title page logos
                        # - Small images (< 20KB): likely icons/logos, not color plates
                        skip_patterns = ['white', 'logo', 'blank', 'separator', 'allcover']
                        if any(pattern in original_filename.lower() for pattern in skip_patterns):
                            continue
                        
                        # Check actual image file size
                        # Note: Some publishers (e.g., Gagaga Bunko) use small decorative images
                        # as chapter headers/ornaments that should be included as kuchie
                        img_file_path = content_dir / img_src
                        if img_file_path.exists():
                            img_size = img_file_path.stat().st_size
                            # Lowered threshold to 5KB to capture decorative chapter images
                            # Very small images (< 5KB) are usually gaiji or icons
                            if img_size < 5000:  # < 5KB, likely gaiji/icon
                                continue
                        
                        # Generate sequential kuchie filename
                        kuchie_number = len(kuchie_list) + 1
                        extension = Path(original_filename).suffix
                        normalized_filename = self._format_kuchie_filename(
                            kuchie_number,
                            extension,
                            publisher_profile,
                        )
                        
                        # KUCHIE SCHEMA (v3.5) - CANONICAL FIELDS
                        # ========================================
                        # "file": normalized destination filename (Builder uses this)
                        # "original": source filename from EPUB
                        # "image_path": full relative path to source image
                        # "spine_index": position in OPF spine
                        # "spine_href": XHTML file reference in spine
                        # 
                        # NOTE: Builder expects "file" field. Legacy manifests may have
                        # only "image_path" - Builder has fallback for this.
                        kuchie_list.append({
                            "file": normalized_filename,
                            "original": original_filename,
                            "spine_index": idx,
                            "spine_href": spine_item.href,
                            "image_path": img_src,
                        })
                        
            except Exception as e:
                # Silently continue on parse errors
                continue
        
        return self._normalize_kuchie_japanese_order(
            spine,
            kuchie_list,
            publisher_profile=publisher_profile,
        )

    def _extract_illustrations_from_spine(
        self,
        spine: Spine,
        content_dir: Path,
        excluded_originals: Set[str],
        publisher_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extract illustration image references directly from spine XHTML pages.

        This is the authoritative path for illustration copy ordering and avoids
        filename-pattern regex classification for illustration assets.
        """
        from lxml import etree

        profile_manager = get_profile_manager()
        excluded_lower = {str(name).lower() for name in excluded_originals}
        valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        cover_name_pattern = re.compile(
            r"^(?:all)?cover(?:[-_].*)?\.(?:jpe?g|png|webp|gif)$",
            re.IGNORECASE,
        )
        seen = set()
        illustrations: List[Dict[str, Any]] = []

        for idx, spine_item in enumerate(spine.items):
            if not spine_item.linear:
                continue
            if not str(spine_item.href).lower().endswith((".xhtml", ".html", ".htm")):
                continue
            if self._is_frontmatter_spine_page(spine_item):
                continue

            xhtml_path = content_dir / spine_item.href
            if not xhtml_path.exists():
                continue

            try:
                tree = etree.parse(str(xhtml_path))
                root = tree.getroot()

                body = root.find(".//{http://www.w3.org/1999/xhtml}body")
                if body is None:
                    body = root.find(".//body")
                if body is None:
                    continue

                refs: List[str] = []
                for img in body.findall(".//{http://www.w3.org/1999/xhtml}img"):
                    src = img.get("src")
                    if src:
                        refs.append(src)
                for img in body.findall(".//img"):
                    src = img.get("src")
                    if src:
                        refs.append(src)
                for img in body.findall(".//{http://www.w3.org/2000/svg}image"):
                    src = img.get("{http://www.w3.org/1999/xlink}href") or img.get("href")
                    if src:
                        refs.append(src)

                for src in refs:
                    resolved = self._resolve_image_path(spine_item.href, src)
                    original = Path(resolved).name.strip()
                    if not original:
                        continue

                    lower_name = original.lower()
                    if lower_name in excluded_lower:
                        continue
                    if lower_name in seen:
                        continue
                    if "allcover" in lower_name:
                        continue
                    if cover_name_pattern.match(original) or "hyoushi" in lower_name:
                        continue
                    if Path(original).suffix.lower() not in valid_exts:
                        continue
                    if profile_manager.is_excluded_image(original, publisher_name):
                        continue

                    seen.add(lower_name)
                    illustrations.append(
                        {
                            "file": original,
                            "original": original,
                            "spine_index": idx,
                            "spine_href": spine_item.href,
                            "image_path": resolved,
                        }
                    )
            except Exception:
                continue

        return illustrations

    def _normalize_kuchie_japanese_order(
        self,
        spine: Spine,
        kuchie_list: List[Dict[str, Any]],
        publisher_profile: Optional[PublisherProfile] = None,
    ) -> List[Dict[str, Any]]:
        """
        Normalize kuchie ordering to original Japanese reading order.

        The Librarian is the single source of truth for kuchie ordering. Builder
        should consume this order as-is and never re-interpret by output direction.

        Kuchie order is LOCKED to the OPF spine order for every EPUB, RTL or not.
        The spine is the authoritative narrative order as authored; kuchi-e are a
        visual sequence independent of text reading direction. The former
        "flip to LTR" option reversed the illustration sequence in the output
        (an inversion) and is removed — no prompt, no flip. The builder refuses
        to re-reverse for the same reason (src/builder/agent.py _detect_kuchie_images).
        """
        if not kuchie_list:
            return []

        ordered = [dict(item) for item in kuchie_list]
        page_progression = (spine.page_progression or "ltr").lower()

        if page_progression == "rtl":
            # RTL EPUB: keep spine order exactly as authored. Reversing it here
            # inverted the illustration order in the output.
            print()
            print("  ┌─────────────────────────────────────────────────────────────────┐")
            print("  │  [INFO] RTL EPUB detected — kuchie order locked to spine order    │")
            print("  └─────────────────────────────────────────────────────────────────┘")
            print("  This EPUB uses page-progression-direction=rtl (Japanese right-to-left).")
            print("  Kuchie order is preserved exactly as authored in the spine — NOT")
            print("  reversed. (A flip would invert the illustration sequence.)")
            for k in ordered:
                print(f"    [spine {k.get('spine_index', '?'):>2}] {k.get('original', '?')}")

        # Renumber normalized filenames to match final manifest order.
        for idx, item in enumerate(ordered, 1):
            current_file = str(item.get("file", "") or "")
            ext = Path(current_file).suffix or Path(str(item.get("original", ""))).suffix or ".jpg"
            item["file"] = self._format_kuchie_filename(
                idx,
                ext,
                publisher_profile,
            )

        return ordered

    def _validate_toc_completeness(self, toc: TableOfContents, spine: Spine) -> tuple[float, set]:
        """
        Validate if TOC covers all content files in spine.
        
        Args:
            toc: Parsed table of contents
            spine: Parsed spine with reading order
            
        Returns:
            Tuple of (coverage_ratio, missing_files_set)
            - coverage_ratio: 0.0 to 1.0 indicating percentage of spine files in TOC
            - missing_files: Set of filenames in spine but not in TOC
        """
        # Get normalized filenames from TOC
        toc_files = set()
        for np in toc.get_flat_list():
            # Normalize: xhtml/p-007.xhtml#toc-001 -> p-007.xhtml
            filename = np.content_src.split('#')[0]
            if '/' in filename:
                filename = filename.split('/')[-1]
            toc_files.add(filename)
        
        # Get content files from spine (exclude special files)
        skip_patterns = ['cover', 'nav', 'toc', 'colophon', 'copyright', 'titlepage']
        spine_content = set()
        for item in spine.items:
            if not item.linear or item.is_illustration:
                continue
            filename = item.href.split('/')[-1] if '/' in item.href else item.href
            lower_name = filename.lower()
            if any(skip in lower_name for skip in skip_patterns):
                continue
            spine_content.add(filename)
        
        # Calculate coverage
        if not spine_content:
            return 1.0, set()  # No content files to check
        
        covered_files = toc_files & spine_content
        missing_files = spine_content - toc_files
        coverage = len(covered_files) / len(spine_content)
        
        return coverage, missing_files

    def _validate_toc_alignment(self, toc: TableOfContents, spine: Spine) -> tuple[float, set]:
        """
        Validate whether TOC entries themselves map to spine content files.

        This catches genuinely broken TOCs while allowing chapter-boundary TOCs
        where continuation XHTML files are intentionally absent from TOC.

        Returns:
            Tuple of (alignment_ratio, toc_missing_in_spine)
            - alignment_ratio: 0.0 to 1.0 coverage of TOC content files in spine
            - toc_missing_in_spine: TOC files that do not exist in spine content
        """
        # Normalize TOC files
        toc_files = set()
        for np in toc.get_flat_list():
            filename = np.content_src.split('#')[0]
            if '/' in filename:
                filename = filename.split('/')[-1]
            toc_files.add(filename)

        skip_patterns = ['cover', 'nav', 'toc', 'colophon', 'copyright', 'titlepage']
        toc_content = set()
        for filename in toc_files:
            lower_name = filename.lower()
            if any(skip in lower_name for skip in skip_patterns):
                continue
            toc_content.add(filename)

        spine_content = set()
        for item in spine.items:
            if not item.linear or item.is_illustration:
                continue
            filename = item.href.split('/')[-1] if '/' in item.href else item.href
            lower_name = filename.lower()
            if any(skip in lower_name for skip in skip_patterns):
                continue
            spine_content.add(filename)

        if not toc_content:
            return 0.0, set()

        aligned = toc_content & spine_content
        missing = toc_content - spine_content
        return len(aligned) / len(toc_content), missing

    def _detect_volume_acts(
        self,
        spine: Spine,
        toc: TableOfContents,
        content_dir: Path
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Detect multi-act/multi-volume structure from spine analysis.
        
        Identifies act boundaries by detecting tobira (title page) patterns
        that appear mid-spine after chapter content has begun. This is
        characteristic of merged multi-volume Japanese light novel EPUBs
        (e.g., Shueisha Dash X Bunko "Act 1 + Act 2" releases).
        
        Detection signals:
        - Spine idrefs containing 'tobira' keyword (most reliable)
        - Multiple tobira groups separated by content pages
        - TOC entries matching act naming patterns (幕, Act, Part)
        
        Reference: 25d9 (魔弾の王と戦姫) spine structure:
        - p-tobira-001/001-2 → Act 1 header
        - p-fmatter-001..006 → Act 1 kuchie (6 plates)
        - p-001..p-028 → Act 1 content
        - p-tobira-002 → Act 2 header (mid-spine boundary!)
        - p-fmatter-007..008 → Act 2 kuchie (2 plates)
        - p-029..p-054 → Act 2 content
        
        Args:
            spine: Parsed spine with reading order
            toc: Parsed table of contents
            content_dir: Extracted EPUB content directory
            
        Returns:
            List of act dicts, or None if single-volume.
            Each dict contains:
            {
                "act_number": int,
                "title": str,
                "tobira_spine_indices": [int],
                "kuchie_spine_indices": [int],
                "first_content_spine_index": int,
                "toc_index": int,  # Index into TOC flat list
            }
        """
        # Phase 1: Find tobira pages in spine by idref pattern
        tobira_entries = []
        for idx, item in enumerate(spine.items):
            if 'tobira' in item.idref.lower():
                tobira_entries.append((idx, item))
        
        if len(tobira_entries) < 2:
            return None  # Need at least 2 tobira references for multi-act
        
        # Phase 2: Group consecutive tobira pages into act boundaries
        # Consecutive tobira pages (e.g., tobira-001, tobira-001-2) = same act header
        # Tobira separated by content pages = different act boundary
        tobira_groups = []
        current_group = [tobira_entries[0]]
        skip_idrefs = {'cover', 'caution', 'titlepage', 'colophon'}
        
        for i in range(1, len(tobira_entries)):
            prev_idx = tobira_entries[i - 1][0]
            curr_idx = tobira_entries[i][0]
            
            # Check if content pages exist between consecutive tobira entries
            has_content_between = False
            for j in range(prev_idx + 1, curr_idx):
                item = spine.items[j]
                idref_lower = item.idref.lower()
                if (item.linear
                        and 'fmatter' not in idref_lower
                        and 'tobira' not in idref_lower
                        and not any(s in idref_lower for s in skip_idrefs)):
                    has_content_between = True
                    break
            
            if has_content_between:
                tobira_groups.append(current_group)
                current_group = [tobira_entries[i]]
            else:
                current_group.append(tobira_entries[i])
        
        tobira_groups.append(current_group)
        
        if len(tobira_groups) < 2:
            return None  # All tobira pages in same group = single volume
        
        # Phase 3: Build act metadata
        # Cross-reference TOC entries with tobira spine items for act titles
        toc_flat = toc.get_flat_list()
        toc_map = {}  # filename -> (label, toc_index)
        for np_idx, np in enumerate(toc_flat):
            filename = np.content_src.split('#')[0]
            if '/' in filename:
                filename = filename.split('/')[-1]
            toc_map[filename] = (np.label, np_idx)
        
        acts = []
        for act_num, group in enumerate(tobira_groups, 1):
            last_tobira_idx = group[-1][0]
            tobira_indices = [idx for idx, _ in group]
            
            # Find act title and TOC index from TOC by matching tobira href
            act_title = ""
            act_toc_index = -1
            for _, tobira_item in group:
                filename = tobira_item.href.split('/')[-1] if '/' in tobira_item.href else tobira_item.href
                if filename in toc_map:
                    act_title, act_toc_index = toc_map[filename]
                    break
            
            if not act_title:
                act_title = f"Act {act_num}"
            
            # Find fmatter (kuchie) pages following this tobira group
            kuchie_indices = []
            scan_idx = last_tobira_idx + 1
            while scan_idx < len(spine.items):
                item = spine.items[scan_idx]
                if 'fmatter' in item.idref.lower() or item.is_illustration:
                    kuchie_indices.append(scan_idx)
                    scan_idx += 1
                else:
                    break
            
            # First content page after kuchie
            first_content_idx = scan_idx
            
            acts.append({
                "act_number": act_num,
                "title": act_title,
                "tobira_spine_indices": tobira_indices,
                "kuchie_spine_indices": kuchie_indices,
                "first_content_spine_index": first_content_idx,
                "toc_index": act_toc_index,
            })
        
        return acts

    def _extract_kuchie_multi_act(
        self,
        spine: Spine,
        content_dir: Path,
        volume_acts: List[Dict[str, Any]],
        publisher_profile: Optional[PublisherProfile] = None,
    ) -> List[Dict[str, Any]]:
        """
        Extract normalized frontmatter kuchie for ACT 1 only in a multi-act EPUB.
        
        Rationale:
        - Act 1 kuchie should remain regular frontmatter color plates.
        - Act 2+ tobira/fmatter images should stay inline in chapter flow to avoid
          duplicate insertion later in Builder (seen in dual-volume 25d9).
        
        Args:
            spine: Parsed spine with reading order
            content_dir: Extracted EPUB content directory
            volume_acts: Act boundary metadata from _detect_volume_acts
            
        Returns:
            List of Act 1 kuchie dicts with act tags:
            [{"file": "kuchie-001.jpg", ..., "act": 1}, ...]
        """
        from lxml import etree
        
        kuchie_list = []
        
        for act in volume_acts:
            act_num = act['act_number']
            if act_num != 1:
                continue
            # Scan both tobira pages (may contain title card images) and fmatter pages
            scan_indices = act['tobira_spine_indices'] + act['kuchie_spine_indices']
            
            for spine_idx in scan_indices:
                if spine_idx >= len(spine.items):
                    continue
                
                spine_item = spine.items[spine_idx]
                xhtml_path = content_dir / spine_item.href
                if not xhtml_path.exists():
                    continue
                
                try:
                    tree = etree.parse(str(xhtml_path))
                    root = tree.getroot()
                    
                    body = root.find(".//{http://www.w3.org/1999/xhtml}body")
                    if body is None:
                        body = root.find(".//body")
                    if body is None:
                        continue
                    
                    # Check text content - skip pages with substantial text (chapter pages)
                    text_parts = []
                    char_count = 0
                    for elem in body.iter():
                        if char_count > 200:
                            break
                        tag_name = elem.tag.split('}')[-1] if '}' in str(elem.tag) else str(elem.tag)
                        if tag_name in ('img', 'image', 'svg'):
                            continue
                        if elem.text:
                            text_parts.append(elem.text.strip())
                            char_count += len(elem.text.strip())
                        if elem.tail:
                            text_parts.append(elem.tail.strip())
                            char_count += len(elem.tail.strip())
                    
                    text_content = ' '.join(text_parts).strip()
                    if len(text_content) > 200:
                        continue  # Too much text, likely a chapter page
                    
                    # Find image references
                    img_src = None
                    
                    # Check <img> elements
                    for ns_prefix in ['{http://www.w3.org/1999/xhtml}', '']:
                        for img in body.findall(f".//{ns_prefix}img"):
                            src = img.get('src')
                            if src:
                                img_src = self._resolve_image_path(spine_item.href, src)
                                break
                        if img_src:
                            break
                    
                    # Check SVG <image> elements
                    if not img_src:
                        for img in body.findall(".//{http://www.w3.org/2000/svg}image"):
                            src = img.get('{http://www.w3.org/1999/xlink}href') or img.get('href')
                            if src:
                                img_src = self._resolve_image_path(spine_item.href, src)
                                break
                    
                    if not img_src:
                        continue
                    
                    original_filename = Path(img_src).name
                    
                    # Skip known non-kuchie patterns
                    skip_patterns = ['white', 'logo', 'blank', 'separator', 'allcover']
                    if any(p in original_filename.lower() for p in skip_patterns):
                        continue
                    
                    # Check image file size (skip tiny icons < 5KB)
                    img_file_path = content_dir / img_src
                    if img_file_path.exists() and img_file_path.stat().st_size < 5000:
                        continue
                    
                    # Generate sequential kuchie filename
                    kuchie_number = len(kuchie_list) + 1
                    extension = Path(original_filename).suffix
                    normalized_filename = self._format_kuchie_filename(
                        kuchie_number,
                        extension,
                        publisher_profile,
                    )
                    
                    # Determine type based on spine idref
                    is_tobira = spine_idx in act['tobira_spine_indices']
                    kuchie_type = "tobira" if is_tobira else "color_plate"
                    
                    kuchie_list.append({
                        "file": normalized_filename,
                        "original": original_filename,
                        "spine_index": spine_idx,
                        "spine_href": spine_item.href,
                        "image_path": img_src,
                        "act": act_num,
                        "type": kuchie_type,
                    })
                    
                except Exception:
                    continue
        
        return self._normalize_kuchie_japanese_order(
            spine,
            kuchie_list,
            publisher_profile=publisher_profile,
        )

    def _resolve_image_path(self, xhtml_href: str, img_src: str) -> str:
        """Resolve relative image path from XHTML location to content-dir-relative path."""
        from pathlib import PurePosixPath
        xhtml_dir = PurePosixPath(xhtml_href).parent
        resolved = []
        for part in (xhtml_dir / img_src).parts:
            if part == '..':
                if resolved:
                    resolved.pop()
            elif part != '.':
                resolved.append(part)
        return '/'.join(resolved)

    def _validate_references_in_chapters(
        self,
        chapters: List['ConvertedChapter'],
        source_dir: Path
    ):
        """
        Validate real-world references in converted chapters using Phase 1.55 Reference Validator.

        This detects and logs:
        - Author names (デボラ・ザック → Devora Zack)
        - Book titles (『シングルタスク』→ Singletasking)
        - Celebrity/person names (タ○ソン → Mike Tyson)
        - Brand names (LIME → LINE, MgRonald's → McDonald's)
        - Place names (ニューヨーク → New York)

        Args:
            chapters: List of converted chapters
            source_dir: Directory where source markdown files are saved
        """
        if not REFERENCE_VALIDATOR_AVAILABLE:
            print("     [SKIP] Reference Validator not available (optional feature)")
            return

        try:
            validator = ReferenceValidator(enable_wikipedia=False)  # Skip Wikipedia per user request

            # Create .context folder for validation reports (not in JP source folder)
            context_dir = source_dir.parent / '.context'
            context_dir.mkdir(parents=True, exist_ok=True)
            # Clean legacy per-chapter reference reports so output remains unified-only.
            for legacy in context_dir.glob("*.references.json"):
                try:
                    legacy.unlink()
                except Exception:
                    pass
            for legacy in context_dir.glob("CHAPTER_*.json"):
                name = legacy.name
                if "_SUMMARY.json" in name or "_VOLUME_CONTEXT.json" in name:
                    continue
                try:
                    payload = json.loads(legacy.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if (
                    isinstance(payload, dict)
                    and {"file_path", "total_entities_detected", "entities"}.issubset(payload.keys())
                ):
                    try:
                        legacy.unlink()
                    except Exception:
                        pass

            total_entities = 0
            total_obfuscated = 0
            chapter_reports: List[Tuple[str, Dict[str, Any]]] = []

            for chapter in chapters:
                # Read chapter markdown file
                chapter_path = source_dir / chapter.filename

                if not chapter_path.exists():
                    continue

                # Validate references
                report = validator.validate_file(chapter_path)
                chapter_reports.append((chapter_path.name, report.to_dict()))

                if report.total_entities_detected > 0:
                    print(f"       {chapter.filename}: {report.total_entities_detected} entities "
                          f"({report.obfuscated_entities} obfuscated)")

                    # Log high-confidence entities that need correction
                    for entity in report.entities:
                        if entity.is_obfuscated and entity.confidence >= 0.95:
                            print(f"         → {entity.detected_term} → {entity.real_name} "
                                  f"({entity.entity_type}, {entity.confidence:.2f})")

                    total_entities += report.total_entities_detected
                    total_obfuscated += report.obfuscated_entities

            if total_entities > 0:
                print(f"     Total: {total_entities} real-world references detected "
                      f"({total_obfuscated} need correction)")
                if compile_reference_payloads:
                    output_path = context_dir / "reference_registry.json"
                    compiled = compile_reference_payloads(
                        chapter_reports,
                        output_path=output_path,
                        min_confidence=0.70,
                    )
                    print(
                        f"     Unified reference registry: {output_path.name} "
                        f"({compiled.get('unique_entities', 0)} unique entities, "
                        f"{compiled.get('unique_deobfuscation_terms', 0)} deobfuscation mappings)"
                    )
            else:
                print("     No real-world references detected")

        except Exception as e:
            print(f"     [WARNING] Reference validation failed: {e}")
            # Don't fail the entire pipeline if reference validation fails
            import traceback
            traceback.print_exc()

    def _find_cover_from_spine_kuchie(self, spine_kuchie: List[Dict[str, Any]]) -> Optional[str]:
        """
        Identify cover candidate from spine-derived kuchie metadata.

        Only returns a value when explicit cover markers are present.
        If no explicit marker is found, caller should apply orientation fallback.
        """
        if not spine_kuchie:
            return None

        explicit_markers = ("cover", "hyoushi", "表紙")
        for item in spine_kuchie:
            original = str(item.get("original", "") or "")
            if any(marker.lower() in original.lower() for marker in explicit_markers):
                filename = str(item.get("file", "") or "").strip()
                if filename and "allcover" not in filename.lower():
                    return filename

        return None

    def _select_cover_asset_filename(
        self,
        image_catalog: Dict[str, List],
        spine: Spine,
        spine_kuchie: List[Dict[str, Any]],
    ) -> Optional[str]:
        """
        Resolve manifest cover with spine-aware priority and orientation fallback.

        Order:
        1) Explicit cover extracted from EPUB metadata/patterns.
        2) Cover identified directly from spine-derived kuchie originals.
        3) Kuchie fallback by source reading orientation:
           - LTR source -> first kuchie
           - RTL source -> last kuchie
        """
        explicit_cover = None
        cover_candidates = []
        for item in image_catalog.get("cover", []) or []:
            filename = getattr(item, "filename", None)
            if not filename:
                continue
            normalized = str(filename).strip()
            if not normalized:
                continue
            if "allcover" in normalized.lower():
                continue
            cover_candidates.append(normalized)

        # Strict preference: canonical cover.jpg when available.
        for candidate in cover_candidates:
            if candidate.lower() == "cover.jpg":
                explicit_cover = "cover.jpg"
                break
        if explicit_cover:
            return explicit_cover

        spine_cover = self._find_cover_from_spine_kuchie(spine_kuchie)
        if spine_cover:
            return spine_cover

        if not spine_kuchie:
            return None

        page_progression = (spine.page_progression or "ltr").lower()
        if page_progression == "rtl":
            return spine_kuchie[-1].get("file")
        return spine_kuchie[0].get("file")

    def _write_context_placeholder(
        self,
        context_path: Path,
        *,
        volume_id: str,
        opf_metadata: Dict[str, Any],
        target_lang: str,
    ) -> None:
        """Write the 17-block context shell owned by the preparation agents."""
        block_owners = (
            ("validation_audit", "metadata_gate"),
            ("volume_identity", "metadata_processor"),
            ("world_setting", "metadata_processor"),
            ("character_roster", "metadata_processor"),
            ("name_map", "metadata_processor"),
            ("relationship_graph", "metadata_processor"),
            ("verbatim_anchors", "series_continuity"),
            ("character_attribute_anchors", "metadata_processor"),
            ("voice_fingerprints", "metadata_processor"),
            ("cultural_glossary", "metadata_processor"),
            ("eps_arc_tracker", "metadata_processor"),
            ("scene_plans", "scene_planner"),
            ("eps_signals", "metadata_processor"),
            ("chapter_signals", "translation_signal_agent"),
            ("illustration_context", "visual_analysis"),
            ("translation_brief", "translation_brief_agent"),
            ("translation_inheritance", "safety_fallback"),
        )

        root = ET.Element(
            "mtls_project_context",
            {
                "schema_version": "1.0",
                "volume_id": volume_id,
                "target_language": target_lang,
                "generated_by": "librarian",
            },
        )
        payload = ET.SubElement(root, "opf_metadata", {"format": "json"})
        payload.text = json.dumps(opf_metadata, ensure_ascii=False, indent=2)
        for block_name, owner in block_owners:
            block = ET.SubElement(root, block_name, {"status": "pending", "owner": owner})
            ET.SubElement(block, "pending").text = "Awaiting owning agent."
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(context_path, encoding="utf-8", xml_declaration=True)

    def _build_manifest(
        self,
        volume_id: str,
        epub_path: Path,
        toc: TableOfContents,
        spine: Spine,
        chapters: List[ConvertedChapter],
        image_catalog: Dict[str, List],
        spine_kuchie: List[Dict[str, Any]],  # Add spine-based kuchie metadata
        source_lang: str,
        target_lang: str,
        recovery_info: Optional[Dict[str, Any]] = None,
        volume_acts: Optional[List[Dict[str, Any]]] = None,
    ) -> Manifest:
        """Build complete manifest from extraction results."""
        now = datetime.now().isoformat()

        # Get TOC entries for metadata
        toc_entries = toc.get_flat_list()

        # Build chapter entries with semantic IDs and TOC order
        chapter_entries = []
        used_ids = set()  # Track used IDs to prevent duplicates
        for i, ch in enumerate(chapters):
            # Get corresponding TOC entry for level info
            toc_entry = toc_entries[i] if i < len(toc_entries) else None

            # Generate semantic ID from title
            chapter_id = self._generate_chapter_id(ch.title, i, ch.filename)

            # Ensure ID is unique by adding suffix if needed
            if chapter_id in used_ids:
                # Find unique suffix
                suffix = 2
                while f"{chapter_id}_{suffix}" in used_ids:
                    suffix += 1
                chapter_id = f"{chapter_id}_{suffix}"
            used_ids.add(chapter_id)
            
            entry = ChapterEntry(
                id=chapter_id,
                source_file=ch.filename,
                translated_file=ch.filename.replace('.md', f'_{target_lang.upper()}.md'),
                word_count=ch.word_count,
                toc_order=i,  # Explicit canonical order
                toc_level=toc_entry.level if toc_entry else 0,  # Nesting level
                is_pre_toc_content=ch.is_pre_toc_content,
                source_files=list(getattr(ch, "source_files", []) or []),
                raw_group_index=getattr(ch, "raw_group_index", None),
                raw_group_title=getattr(ch, "raw_group_title", None),
                split_strategy=getattr(ch, "split_strategy", None),
            )
            chapter_entries.append(entry.to_dict())

        # Build asset references with spine-based kuchie metadata
        illustration_assets = [img.filename for img in image_catalog["illustrations"]]
        # Preserve order while removing duplicates.
        illustration_assets = list(dict.fromkeys(illustration_assets))
        cover_asset = self._select_cover_asset_filename(
            image_catalog=image_catalog,
            spine=spine,
            spine_kuchie=spine_kuchie,
        )

        assets = {
            "cover": cover_asset,
            "kuchie": spine_kuchie,  # Use spine-based kuchie with full metadata
            "illustrations": illustration_assets,
        }

        # Build pipeline state with recovery information
        librarian_state = PipelineState(
            status="completed",
            timestamp=now,
            chapters_completed=len(chapters),
            chapters_total=len(chapters),
        ).to_dict()
        
        # Add recovery diagnostics if spine fallback was used
        if recovery_info and recovery_info.get("used_spine_fallback"):
            librarian_state["recovery_method"] = "spine_fallback"
            librarian_state["recovery_reasons"] = recovery_info["recovery_reasons"]
            librarian_state["toc_entries"] = recovery_info["toc_entries"]
            librarian_state["spine_content_files"] = recovery_info["spine_content_files"]
            librarian_state["toc_coverage"] = round(recovery_info["toc_coverage"], 2)
            if recovery_info["missing_from_toc"]:
                librarian_state["missing_from_toc"] = recovery_info["missing_from_toc"][:10]  # Limit to 10 for brevity
            split_chapters = [c for c in chapter_entries if c.get("split_strategy") == "text_page_boundary"]
            if split_chapters:
                librarian_state["spine_split_strategy"] = "text_page_boundary"
                librarian_state["spine_split_chapters"] = len(split_chapters)
                raw_group_indices = {
                    c.get("raw_group_index")
                    for c in chapter_entries
                    if c.get("raw_group_index") is not None
                }
                if raw_group_indices:
                    librarian_state["spine_raw_group_count"] = len(raw_group_indices)
        
        pipeline_state = {
            "librarian": librarian_state,
            "translator": PipelineState(
                status="pending",
                chapters_total=len(chapters),
            ).to_dict(),
            "critics": PipelineState(status="pending").to_dict(),
            "builder": PipelineState(status="pending").to_dict(),
        }

        # Build volume_structure for multi-act EPUBs
        volume_structure_data = {}
        if volume_acts and len(volume_acts) >= 2:
            # Map acts to chapter ranges using TOC indices
            toc_flat = toc.get_flat_list()
            act_details = []
            for i, act in enumerate(volume_acts):
                toc_idx = act.get('toc_index', 0)
                # Next act's toc_index is the boundary, or end of chapters
                if i + 1 < len(volume_acts):
                    next_toc_idx = volume_acts[i + 1].get('toc_index', len(chapters))
                else:
                    next_toc_idx = len(chapters)
                
                # Get chapter IDs for this act
                first_ch_id = chapter_entries[toc_idx]['id'] if toc_idx < len(chapter_entries) else ''
                last_ch_idx = min(next_toc_idx - 1, len(chapter_entries) - 1)
                last_ch_id = chapter_entries[last_ch_idx]['id'] if last_ch_idx >= 0 else ''
                
                # Get kuchie files for this act
                act_kuchie = [k['file'] for k in spine_kuchie if k.get('act') == act['act_number']]
                
                act_details.append({
                    "act_number": act['act_number'],
                    "first_chapter_id": first_ch_id,
                    "last_chapter_id": last_ch_id,
                    "first_chapter_index": toc_idx,
                    "last_chapter_index": last_ch_idx,
                    "chapter_count": next_toc_idx - toc_idx,
                    "kuchie_files": act_kuchie,
                })
            
            volume_structure_data = {
                "is_multi_act": True,
                "act_count": len(volume_acts),
                "acts": act_details,
            }

        return Manifest(
            version="1.0",
            volume_id=volume_id,
            created_at=now,
            runtime_config={
                "source_language": source_lang,
                "target_language": target_lang,
                "page_progression_direction": spine.page_progression,
                "source_epub": str(epub_path.absolute()),
                "source_chapters_dir": "JP",
                "translated_chapters_dir": target_lang.upper(),
            },
            pipeline_state=pipeline_state,
            chapters=chapter_entries,
            assets=assets,
            volume_structure=volume_structure_data,
        )


def _build_volume_number(
    series_index: Any,
    series_name: Any,
) -> List[Dict[str, Any]]:
    """Build the top-level volume_number array from OPF series metadata.

    Rules:
    - If series_index is a number, emit one entry with scheme="series".
    - If series_name is set but series_index is null, emit one entry with value=None
      so downstream knows this is a series volume without a resolved number.
    - If neither is set, return [] (standalone volume).

    Args:
        series_index: Raw series_index value from OPF (float, int, str, or None).
        series_name:  Raw series string from OPF (str or None).

    Returns:
        List of volume-number entry dicts.
    """
    entries: List[Dict[str, Any]] = []

    raw_index = series_index
    # Normalise: "2.0" → 2, 2.0 → 2, None / "" → None
    int_value: Optional[int] = None
    if raw_index is not None and str(raw_index).strip() not in ("", "None"):
        try:
            float_val = float(raw_index)
            int_value = int(float_val) if float_val == int(float_val) else int(float_val)
        except (TypeError, ValueError):
            int_value = None

    has_series = bool(series_name and str(series_name).strip())

    if int_value is not None:
        entries.append({
            "scheme": "series",
            "value": int_value,
            "display": f"Vol. {int_value}",
            "source": "opf",
        })
    elif has_series:
        # Known series but index unresolved — emit a placeholder so the artifact
        # records series affiliation without a concrete number.
        entries.append({
            "scheme": "series",
            "value": None,
            "display": None,
            "source": "opf",
        })
    # else: standalone — return []

    return entries


def run_librarian(
    epub_path: Path,
    volume_id: Optional[str] = None,
    work_base: Optional[Path] = None,
    source_lang: str = "ja",
    target_lang: str = "en",
    validate_references: bool = False,
    force_rerun: bool = False,
) -> Manifest:
    """
    Main entry point for Librarian agent.

    Args:
        epub_path: Path to source EPUB file
        volume_id: Optional custom volume ID
        work_base: Optional custom working directory
        source_lang: Source language code
        target_lang: Target language code
        validate_references: Run Phase 1.55 real-world reference validator (default: False)
        force_rerun: Auto-continue re-extractions into a NEW derived volume
            directory (old output preserved) instead of prompting

    Returns:
        Manifest object with extraction results
    """
    agent = LibrarianAgent(work_base)
    return agent.process_epub(epub_path, volume_id, source_lang, target_lang, validate_references=validate_references, force_rerun=force_rerun)


# CLI interface
if __name__ == "__main__":
    import argparse
    from src.Deepseek.common.config import ensure_utf8_console
    from .config import get_target_language

    # Runs as its own subprocess (see mcp/servers/librarian_server.py's
    # run_module) with the same Windows charmap-stdout crash risk as
    # scripts/mtl.py — this print()s JP titles too. Safe to touch stdout
    # here: this module is never imported in-process by the MCP server,
    # only ever run standalone via `-m src.utility.librarian.agent`.
    ensure_utf8_console()

    # Get default target language from config.yaml
    default_target = get_target_language()

    parser = argparse.ArgumentParser(
        description="Librarian Agent - Extract and catalog EPUB content"
    )
    parser.add_argument("epub", type=Path, help="Path to source EPUB file")
    parser.add_argument("--volume-id", "-v", type=str, help="Custom volume ID")
    parser.add_argument("--work-dir", "-w", type=Path, help="Working directory")
    parser.add_argument("--source-lang", "-s", type=str, default="ja", help="Source language code")
    parser.add_argument("--target-lang", "-t", type=str, default=default_target,
                        help=f"Target language code (default: {default_target} from config.yaml)")
    parser.add_argument("--ref-validate", dest="ref_validate", action="store_true", default=False,
                        help="Run Phase 1.55 real-world reference validator after extraction (default: off)")
    parser.add_argument("--force-rerun", dest="force_rerun", action="store_true", default=False,
                        help="Re-extracting over a workspace with translations proceeds automatically "
                             "into a NEW derived volume directory (old output preserved) without "
                             "prompting. Used by frontends/launchers where a prompt cannot be answered.")

    args = parser.parse_args()

    manifest = run_librarian(
        epub_path=args.epub,
        volume_id=args.volume_id,
        work_base=args.work_dir,
        source_lang=args.source_lang,
        target_lang=args.target_lang,
        validate_references=args.ref_validate,
        force_rerun=args.force_rerun,
    )

    print(f"\nManifest saved to: {manifest.volume_id}/manifest.json")
