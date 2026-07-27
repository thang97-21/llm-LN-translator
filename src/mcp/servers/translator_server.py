"""Translator phase MCP tools — 2 tools only, calling the bare DeepSeekTranslator in-process.

Everything the original translator_server.py exposed beyond this (parallel
dispatch, Koji Fox / VN voice validators, series bible RAG, volume context
aggregation) belongs to modules deliberately excluded from the lightweight
client (see PLANNING.md Phase 4 "DELIBERATELY REMOVED" and the exclusion
inventory). There is no subprocess CLI to shell out to here — the bare
translator has no argparse entry point of its own, so these tools call
DeepSeekTranslator directly.
"""

from __future__ import annotations

from typing import List, Optional

from src.mcp.mcp_config import MCPConfig
from src.mcp.runtime import MCPRuntimeError, load_manifest, resolve_volume_dir
from src.translator.agent import DeepSeekTranslator


def register_translator_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register Phase 2 tools."""

    @mcp.tool()  # type: ignore[attr-defined]
    def translate_chapter(volume_id: str, chapter_id: str, thinking_log: Optional[bool] = None) -> dict:
        volume_dir = resolve_volume_dir(volume_id, cfg)
        jp_dir = volume_dir / "JP"
        chapter_path = _resolve_jp_chapter(jp_dir, chapter_id)

        translator = DeepSeekTranslator(work_dir=volume_dir, volume_id=volume_id, thinking_log_enabled=thinking_log)
        # translate_and_persist_chapter writes the EN file AND marks it
        # completed in manifest.json in one step — do not duplicate that
        # write-then-forget-the-manifest logic here again.
        output_path = translator.translate_and_persist_chapter(chapter_path, {"chapter_id": chapter_path.stem})

        return {
            "schema": "TranslatedChapter",
            "ok": True,
            "volume_id": volume_id,
            "chapter_id": chapter_path.stem,
            "output_path": str(output_path),
        }

    @mcp.tool()  # type: ignore[attr-defined]
    def run_translator(
        volume_id: str,
        chapters: Optional[List[str]] = None,
        thinking_log: Optional[bool] = None,
    ) -> dict:
        volume_dir = resolve_volume_dir(volume_id, cfg)
        jp_dir = volume_dir / "JP"
        if not jp_dir.is_dir():
            raise MCPRuntimeError(f"No JP/ directory for volume {volume_id!r} at {jp_dir}")

        chapter_files = sorted(jp_dir.glob("*.md"))
        if chapters:
            wanted = {str(c).strip() for c in chapters}
            chapter_files = [f for f in chapter_files if f.stem in wanted]

        translator = DeepSeekTranslator(work_dir=volume_dir, volume_id=volume_id, thinking_log_enabled=thinking_log)
        results = translator.translate_all(chapter_files)

        manifest = {}
        try:
            manifest = load_manifest(volume_id, cfg)
        except Exception:
            manifest = {}

        return {
            "schema": "TranslationReport",
            "ok": True,
            "volume_id": volume_id,
            "chapter_count": len(results),
            "output_paths": {chapter_id: str(path) for chapter_id, path in results.items()},
            "pipeline_state": manifest.get("pipeline_state", {}) if isinstance(manifest, dict) else {},
        }


def _resolve_jp_chapter(jp_dir, chapter_id: str):
    """Resolve a JP chapter markdown file by id/stem, tolerant of a .md suffix."""
    raw = str(chapter_id or "").strip()
    if not raw:
        raise MCPRuntimeError("chapter_id is required")
    stem = raw[:-3] if raw.lower().endswith(".md") else raw
    for candidate in (jp_dir / raw, jp_dir / f"{stem}.md"):
        if candidate.exists():
            return candidate
    matches = [p for p in sorted(jp_dir.glob("*.md")) if stem.lower() in p.stem.lower()]
    if matches:
        return matches[0]
    raise MCPRuntimeError(f"Chapter file not found: {jp_dir}/{chapter_id}")
