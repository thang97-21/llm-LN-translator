"""Prep phase MCP tools — one tool bound to ``run_prep``.

No subprocess, no main-pipeline dependency. The active default is a persisted
multi-turn prep conversation: one JSON-node response per generated block,
followed by deterministic atomic context.xml assembly.
"""

from __future__ import annotations

from typing import Optional

from src.Deepseek.mcp.mcp_config import MCPConfig
from src.Deepseek.mcp.runtime import MCPRuntimeError, resolve_volume_dir
from src.utility.prep.agent import PrepError, run_prep


def register_prep_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register the Phase 1.P prep tool."""

    @mcp.tool()  # type: ignore[attr-defined]
    def prep_volume(
        volume_id: str,
        series_id: Optional[str] = None,
        force_rerun: bool = False,
    ) -> dict:
        """force_rerun re-asks every multi-turn block rather than reusing a
        completed artifact — the way to redo a block that parsed but reads
        badly. Left false, a repeat call costs only the outstanding turns."""
        resolve_volume_dir(volume_id, cfg)  # validates the volume exists under work/
        try:
            receipt = run_prep(volume_id, series_id=series_id, force_rerun=force_rerun)
        except PrepError as exc:
            raise MCPRuntimeError(str(exc)) from exc
        receipt["schema"] = "PrepReceipt"
        receipt["ok"] = True
        return receipt
