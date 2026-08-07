"""
Minimal context.xml loader.

The lightweight client does not build context.xml — no metadata phases, no
RAG modules, no JIT injection, no validation. The user supplies a pre-built
context.xml (produced by the full MTLS pipeline's prep phases, or written by
hand) and this just reads it.
"""

from pathlib import Path
from typing import Optional


def load_context_xml(work_dir: Path) -> Optional[str]:
    """
    Read WORK/<volume_id>/context.xml verbatim.

    Returns None (not an empty string) when the file is missing — callers
    decide whether that's fatal or just a "translate with inline prompt
    guidance only" warning (see PLANNING.md "Further Considerations" #1).
    """
    context_xml_path = Path(work_dir) / "context.xml"
    if not context_xml_path.exists():
        return None
    return context_xml_path.read_text(encoding="utf-8")
