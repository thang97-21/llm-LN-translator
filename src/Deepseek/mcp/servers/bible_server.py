"""Bible Writer MCP tools — 1 tool, direct binding to src.utility.bible.agent.run_write_bible.

See src/bible/agent.py. Merges one volume's populated context.xml into the
cumulative series bible under bibles/<series_id>/.
"""

from __future__ import annotations

from typing import Optional

from src.utility.bible.agent import BibleError, run_write_bible
from src.Deepseek.mcp.mcp_config import MCPConfig
from src.Deepseek.mcp.runtime import MCPRuntimeError, resolve_volume_dir


def register_bible_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register the Bible Writer tool."""

    @mcp.tool()  # type: ignore[attr-defined]
    def write_bible(volume_id: str, series_id: Optional[str] = None) -> dict:
        resolve_volume_dir(volume_id, cfg)  # validates the volume exists under work/
        try:
            receipt = run_write_bible(volume_id, series_id=series_id)
        except BibleError as exc:
            raise MCPRuntimeError(str(exc)) from exc
        receipt["ok"] = True
        return receipt
