"""QC phase MCP tools — 1 tool, direct binding to src.qc.agent.run_qc.

Filesystem-only, zero API calls, <5s per volume. See src/qc/agent.py.
"""

from __future__ import annotations

from src.mcp.mcp_config import MCPConfig
from src.mcp.runtime import resolve_volume_dir
from src.qc.agent import run_qc


def register_qc_tools(mcp: object, cfg: MCPConfig) -> None:
    """Register the QC gate tool."""

    @mcp.tool()  # type: ignore[attr-defined]
    def qc_volume(volume_id: str) -> dict:
        resolve_volume_dir(volume_id, cfg)  # validates the volume exists under work/
        return run_qc(volume_id)
