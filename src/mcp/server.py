"""Main MCP server entry point for DeepSeek_MTLS.

Slimmed from the main pipeline's 8 tool servers / ~42 tools / 8 resource
groups down to 6 tool servers (librarian, prep, translator, qc, bible,
builder), 20 tools, and 2 resources (persona + phase routing — see
src/mcp/harness.py). No prompt templates. The main MTLS skills this client
would otherwise need (Prep, Translator, QC, Bible Writer) are reified as MCP
tools instead of markdown documents the IDE agent reads and manually
executes — one tool call per phase, no manual orchestration.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from src.mcp.harness import register_harness_resources
from src.mcp.mcp_config import default_mcp_config
from src.mcp.servers.bible_server import register_bible_tools
from src.mcp.servers.builder_server import register_builder_tools
from src.mcp.servers.librarian_server import register_librarian_tools
from src.mcp.servers.prep_server import register_prep_tools
from src.mcp.servers.qc_server import register_qc_tools
from src.mcp.servers.translator_server import register_translator_tools

MCP_INSTRUCTIONS = (
    "DeepSeek_MTLS lightweight pipeline tools: "
    "Phase 1 librarian extraction, "
    "Phase 1.P unified DeepSeek prep (fills context.xml, no Gemini/main-pipeline dependency), "
    "Phase 2 DeepSeek V4 Pro translation, "
    "QC gate (filesystem-only sanity checks), "
    "Bible Writer (cross-volume series continuity), "
    "Phase 4 builder packaging. "
    "This is the lightweight standalone client, not the full MTL Studio pipeline — "
    "no metadata-phase RAG modules, no vector stores, no 3-model QC fan-out."
)


def create_mcp_server() -> Any:
    """Build and register the DeepSeek_MTLS FastMCP server."""
    cfg = default_mcp_config()

    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:
        raise RuntimeError(
            "Missing MCP dependency. Install with: "
            f"`{sys.executable} -m pip install mcp`"
        ) from exc

    try:
        mcp = FastMCP(cfg.server_name, instructions=MCP_INSTRUCTIONS)
    except TypeError:
        mcp = FastMCP(cfg.server_name)

    register_librarian_tools(mcp, cfg)
    register_prep_tools(mcp, cfg)
    register_translator_tools(mcp, cfg)
    register_qc_tools(mcp, cfg)
    register_bible_tools(mcp, cfg)
    register_builder_tools(mcp, cfg)

    # Resources (not tools) — persona propagation + phase->tool routing.
    # See src/mcp/harness.py.
    register_harness_resources(mcp, cfg)

    return mcp


def main() -> None:
    """Entrypoint used by `python -m src.mcp.server`."""
    # stderr ONLY, never stdout — stdout is the JSON-RPC channel the stdio
    # transport owns; touching its encoding is not this function's call to
    # make. stderr carries this process's own logging (prep/translator/qc/
    # bible all log via `logger.info`, which can embed JP chapter titles or
    # character names) plus the print() a few lines down — same Windows
    # charmap crash as scripts/mtl.py, different stream.
    from src.common.config import ensure_utf8_console
    ensure_utf8_console(("stderr",))

    try:
        mcp = create_mcp_server()
    except Exception as exc:
        print(f"[MCP] Failed to initialize server: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    transport = str(os.getenv("MCP_TRANSPORT", "stdio")).strip().lower()
    if transport in {"", "stdio"}:
        mcp.run()
        return

    if transport in {"http", "streamable-http"}:
        host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_HTTP_PORT", "8765"))
        try:
            mcp.run(transport="streamable-http", host=host, port=port)
            return
        except TypeError:
            mcp.run(transport="streamable-http")
            return

    if transport == "sse":
        mcp.run(transport="sse")
        return

    mcp.run()


if __name__ == "__main__":
    main()
