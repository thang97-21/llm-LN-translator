"""
MCP harness — persona propagation + phase-to-tool routing.

Two MCP resources so any connecting agent gets the same operating context a
local Claude Code session gets automatically from CLAUDE.md:

  persona://instructions   -> this project's CLAUDE.md, verbatim
  routing://phases          -> {phase_name: mcp_tool_name} map
  routing://phases/{phase}  -> single phase's route (or an error payload)

"Subagent routing" here means resolving a phase name (extract/prep/translate/
qc/bible/build) to the one MCP tool that owns it — this client has a single
model tier (DeepSeek V4 Pro/Flash) and no multi-LLM-agent orchestration layer
to route between. It exists so a caller that only knows "I want to run prep"
doesn't have to hardcode the tool name `prep_volume` — it can ask the harness.
"""

from __future__ import annotations

from typing import Dict, Optional

from src.Deepseek.common.config import PIPELINE_ROOT, get_config_section
from src.Deepseek.mcp.mcp_config import MCPConfig

_persona_cache: Optional[str] = None

DEFAULT_PHASE_ROUTES: Dict[str, str] = {
    "extract": "extract_epub",
    "prep": "prep_volume",
    "translate": "run_translator",
    "qc": "qc_volume",
    "bible": "write_bible",
    "build": "package_epub",
}


def load_persona() -> str:
    """Read this project's CLAUDE.md, cached after the first read.

    Empty string (not an error) when CLAUDE.md is absent — a harness that
    hard-fails the whole MCP server over a missing persona file would be a
    worse outcome than just not having persona text to hand out.
    """
    global _persona_cache
    if _persona_cache is None:
        claude_md = PIPELINE_ROOT / "CLAUDE.md"
        _persona_cache = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    return _persona_cache


def get_phase_routes() -> Dict[str, str]:
    """
    Phase name -> MCP tool name. config.yaml's `phase_routing` section can
    override or extend DEFAULT_PHASE_ROUTES (e.g. pointing "translate" at a
    future `translate_volume_parallel` tool without touching this module).
    Unset phases fall back to the built-in default.
    """
    overrides = get_config_section("phase_routing")
    routes = dict(DEFAULT_PHASE_ROUTES)
    if isinstance(overrides, dict):
        routes.update({str(k): str(v) for k, v in overrides.items() if v})
    return routes


def route_phase(phase: str) -> str:
    """Resolve a phase name to its MCP tool name. Raises KeyError if unknown."""
    routes = get_phase_routes()
    key = str(phase or "").strip().lower()
    if key not in routes:
        raise KeyError(f"Unknown phase {phase!r} — known phases: {', '.join(sorted(routes))}")
    return routes[key]


def register_harness_resources(mcp: object, cfg: MCPConfig) -> None:
    """Register the persona + phase-routing MCP resources."""
    _ = cfg  # unused — resources here need no path/allowlist validation

    @mcp.resource("persona://instructions")  # type: ignore[attr-defined]
    def get_persona() -> str:
        return load_persona()

    @mcp.resource("routing://phases")  # type: ignore[attr-defined]
    def get_phase_routing() -> dict:
        return get_phase_routes()

    @mcp.resource("routing://phases/{phase}")  # type: ignore[attr-defined]
    def get_phase_route(phase: str) -> dict:
        try:
            return {"phase": phase, "tool": route_phase(phase)}
        except KeyError as exc:
            return {"phase": phase, "error": str(exc)}
