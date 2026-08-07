"""Builder phase MCP tools with plan-aligned contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from src.builder.markdown_to_xhtml import convert_paragraphs_to_xhtml
from src.builder.merge_translated_shards_to_spine import merge_translated_shards_to_spine
from src.builder.device_profiles import PROFILE_NAMES, UnknownProfileError, resolve_profile
from src.builder.image_optimizer import optimize_image_to

from src.Deepseek.mcp.mcp_config import MCPConfig
from src.Deepseek.mcp.runtime import ensure_allowed_path, load_manifest, resolve_volume_dir, run_module


def register_builder_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register Phase 4 builder tools."""

    @mcp.tool()  # type: ignore[attr-defined]
    def markdown_to_xhtml(
        md_content: str,
        chapter_id: str = "",
        skip_illustrations: bool = False,
    ) -> dict:
        paragraphs = _split_markdown_paragraphs(md_content or "")
        xhtml = convert_paragraphs_to_xhtml(paragraphs, skip_illustrations=bool(skip_illustrations))
        return {
            "schema": "XHTMLContent",
            "chapter_id": chapter_id,
            "paragraph_count": len(paragraphs),
            "xhtml": xhtml,
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def generate_opf(manifest: Dict[str, Any] | None = None, volume_id: str = "") -> dict:
        resolved_manifest = manifest if isinstance(manifest, dict) else {}
        if not resolved_manifest:
            if not volume_id:
                return {"schema": "OPFDocument", "error": "Either manifest or volume_id is required"}
            resolved_manifest = load_manifest(volume_id, cfg)
        metadata = resolved_manifest.get("metadata", {}) if isinstance(resolved_manifest, dict) else {}
        chapters = resolved_manifest.get("chapters", []) if isinstance(resolved_manifest, dict) else []
        return {
            "schema": "OPFDocument",
            "mode": "manifest_preview",
            "volume_id": volume_id or resolved_manifest.get("volume_id", ""),
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "language": metadata.get("target_language", "en"),
            "chapter_count": len(chapters),
            "spine_items": [item.get("translated_file", "") for item in chapters if isinstance(item, dict)],
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def generate_nav(manifest: Dict[str, Any] | None = None, volume_id: str = "") -> dict:
        resolved_manifest = manifest if isinstance(manifest, dict) else {}
        if not resolved_manifest:
            if not volume_id:
                return {"schema": "NAVDocument", "error": "Either manifest or volume_id is required"}
            resolved_manifest = load_manifest(volume_id, cfg)
        manifest = resolved_manifest
        chapters = manifest.get("chapters", []) if isinstance(manifest, dict) else []
        entries = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            entries.append(
                {
                    "id": chapter.get("id", ""),
                    "title": chapter.get("title", ""),
                    "target": chapter.get("translated_file", ""),
                }
            )
        return {
            "schema": "NAVDocument",
            "mode": "manifest_preview",
            "volume_id": volume_id or manifest.get("volume_id", ""),
            "toc_entries": entries,
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def merge_translated_shards(volume_id: str, target_language: str = "en", apply_manifest: bool = False) -> dict:
        volume_dir = resolve_volume_dir(volume_id, cfg)
        manifest = load_manifest(volume_id, cfg)
        merged_manifest, diagnostics = merge_translated_shards_to_spine(
            work_dir=volume_dir,
            manifest=manifest,
            target_language=target_language,
            apply_manifest=apply_manifest,
        )
        return {
            "schema": "MergeResult",
            "diagnostics": diagnostics,
            "canonical_chapter_count": len(merged_manifest.get("chapters", [])),
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def optimize_image(image_path: str, max_width: int = 1600, profile: str = "") -> dict:
        path = Path(image_path).expanduser()
        if not path.is_absolute():
            path = (cfg.pipeline_root / path).resolve()
        ensure_allowed_path(path, cfg)
        if not path.exists():
            return {
                "schema": "OptimizedImage",
                "ok": False,
                "error": f"Image not found: {path}",
            }

        original_size = path.stat().st_size

        # A named profile supersedes max_width entirely: it also carries colour
        # depth, JPEG quality and baseline encoding, none of which the bare
        # max_width path has ever known about.
        if profile:
            try:
                resolved = resolve_profile(profile)
            except UnknownProfileError as exc:
                return {"schema": "OptimizedImage", "ok": False, "error": str(exc)}

            result = optimize_image_to(path, path.parent, resolved)
            if result.renamed:
                # The emitted file has a different extension (gif/webp -> jpg).
                # Drop the undecodable original rather than leaving both, since
                # this tool's contract is to replace the image in place.
                path.unlink(missing_ok=True)
            return {
                "schema": "OptimizedImage",
                "ok": True,
                "path": str(path.parent / result.emitted_name),
                "profile": resolved.name,
                "action": result.action,
                "renamed_from": result.source_name if result.renamed else "",
                "original_size_bytes": result.source_bytes,
                "optimized_size_bytes": result.emitted_bytes,
                "width": result.width,
                "height": result.height,
                "undersized_for_panel": result.undersized,
            }
        with Image.open(path) as image:
            width, height = image.size
            if width > max_width > 0:
                ratio = max_width / float(width)
                new_size = (int(width * ratio), int(height * ratio))
                resized = image.resize(new_size, Image.Resampling.LANCZOS)
                resized.save(path, optimize=True)
                final_size = path.stat().st_size
                return {
                    "schema": "OptimizedImage",
                    "ok": True,
                    "path": str(path),
                    "original_size_bytes": original_size,
                    "optimized_size_bytes": final_size,
                    "width": new_size[0],
                    "height": new_size[1],
                }
            image.save(path, optimize=True)
            final_size = path.stat().st_size
            return {
                "schema": "OptimizedImage",
                "ok": True,
                "path": str(path),
                "original_size_bytes": original_size,
                "optimized_size_bytes": final_size,
                "width": width,
                "height": height,
            }

    @mcp.tool()  # type: ignore[attr-defined]
    def package_epub(
        volume_id: str,
        output_filename: str = "",
        skip_qc: bool = False,
        include_header_illustrations: bool = False,
        dry_run: bool = False,
        profile: str = "",
        emit_xtc: str = "",
    ) -> dict:
        if profile:
            try:
                resolve_profile(profile)
            except UnknownProfileError as exc:
                # Validate before spawning the subprocess: a typo should cost a
                # message, not a build's worth of output and an argparse dump.
                return {
                    "schema": "PackageResult",
                    "ok": False,
                    "volume_id": volume_id,
                    "error": str(exc),
                    "valid_profiles": list(PROFILE_NAMES),
                }

        args = [volume_id]
        if output_filename:
            args.extend(["--output", output_filename])
        if skip_qc:
            args.append("--skip-qc")
        if include_header_illustrations:
            args.append("--include-header-illustrations")
        if dry_run:
            args.append("--dry-run")
        if profile:
            args.extend(["--profile", profile])
        if emit_xtc:
            args.extend(["--emit-xtc", emit_xtc])
        execution = run_module("src.builder.agent", args, cfg)
        return {
            "schema": "PackageResult",
            "ok": bool(execution.get("ok", False)),
            "volume_id": volume_id,
            "dry_run": dry_run,
            "profile": profile,
            "emit_xtc": emit_xtc,
            "execution": execution,
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def run_builder(
        volume_id: str,
        output_filename: str = "",
        skip_qc: bool = False,
        include_header_illustrations: bool = False,
        dry_run: bool = False,
        profile: str = "",
        emit_xtc: str = "",
    ) -> dict:
        result = package_epub(
            volume_id=volume_id,
            output_filename=output_filename,
            skip_qc=skip_qc,
            include_header_illustrations=include_header_illustrations,
            dry_run=dry_run,
            profile=profile,
            emit_xtc=emit_xtc,
        )
        result["schema"] = "BuildResult"
        return result


def _split_markdown_paragraphs(content: str) -> List[str]:
    """Split markdown body into paragraph units for XHTML conversion."""
    if not content.strip():
        return []
    lines = content.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    buffer: List[str] = []
    for line in lines:
        if not line.strip():
            if buffer:
                out.append("\n".join(buffer).strip())
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        out.append("\n".join(buffer).strip())
    return out
