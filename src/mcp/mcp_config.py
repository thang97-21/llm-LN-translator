"""Runtime configuration for the DeepSeek_MTLS MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable

from src.common.config import (
    INPUT_DIR,
    OUTPUT_DIR,
    PIPELINE_ROOT,
    WORK_DIR,
)

__version__ = "1.0.0"


@dataclass(frozen=True)
class MCPConfig:
    """Resolved MCP runtime configuration — 3 tool servers, no bible/RAG/chroma."""

    server_name: str
    version: str
    pipeline_root: Path
    input_dir: Path
    work_dir: Path
    output_dir: Path
    prompt_dir: Path
    config_path: Path
    max_output_tokens: int
    allow_write_tools_without_confirmation: bool
    allowed_directories: tuple[Path, ...]

    @property
    def scripts_dir(self) -> Path:
        return self.pipeline_root / "scripts"


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolved_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(path.resolve() for path in paths)


def default_mcp_config() -> MCPConfig:
    """Build default runtime config from src.common.config constants + environment."""
    prompt_dir = PIPELINE_ROOT / "src" / "prompt"
    config_path = PIPELINE_ROOT / "config.yaml"
    max_output_tokens = int(os.getenv("MAX_MCP_OUTPUT_TOKENS", "25000"))
    allow_writes = _env_bool("MCP_ALLOW_WRITE_WITHOUT_CONFIRMATION", False)

    allowed = _resolved_paths(
        [
            INPUT_DIR,
            WORK_DIR,
            OUTPUT_DIR,
            prompt_dir,
        ]
    )

    return MCPConfig(
        server_name="deepseek-mtls",
        version=__version__,
        pipeline_root=PIPELINE_ROOT.resolve(),
        input_dir=INPUT_DIR.resolve(),
        work_dir=WORK_DIR.resolve(),
        output_dir=OUTPUT_DIR.resolve(),
        prompt_dir=prompt_dir.resolve(),
        config_path=config_path.resolve(),
        max_output_tokens=max_output_tokens,
        allow_write_tools_without_confirmation=allow_writes,
        allowed_directories=allowed,
    )


def validate_path(path: Path, cfg: MCPConfig) -> bool:
    """Return True if path is inside one of the allowed MCP roots."""
    try:
        resolved = path.resolve()
    except Exception:
        return False

    for allowed in cfg.allowed_directories:
        try:
            resolved.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def redact_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Redact obvious API key fields from a config dict copy."""
    if not isinstance(config, dict):
        return {}
    redacted: Dict[str, Any] = {}
    secret_markers = {"api_key", "token", "secret", "password"}
    for key, value in config.items():
        key_lower = str(key).lower()
        if any(marker in key_lower for marker in secret_markers):
            redacted[key] = "***REDACTED***"
            continue
        if isinstance(value, dict):
            redacted[key] = redact_config(value)
        elif isinstance(value, list):
            redacted[key] = [redact_config(v) if isinstance(v, dict) else v for v in value]
        else:
            redacted[key] = value
    return redacted
