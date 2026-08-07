"""
Dry-run prompt inspection.

DeepSeekClient.generate(dry_run=True) (deepseek_client.py) assembles the
exact request payload (model, system, messages, thinking, output_config,
max_tokens) and returns it in LLMResponse.provider_metadata["payload"]
instead of sending it — see that method's docstring for why conversation-
manager processing is skipped in that mode too, not just the network call
(prepare_turn() can itself trigger a real, billed checkpoint-summarization
call). This module turns that payload into a readable .md file.

Per-project, like every other telemetry artifact this pipeline writes now
(LOG/, THINKING/) — WORK/<volume_id>/DRY_RUN/<run-stamp>/<chapter_id>.md.
Run-stamped the same way token_telemetry.py stamps token_log.md: established
once per process per volume, reused for every chapter in that run, so one
translate run's dry-run output collects into one folder and a genuinely new
run (new process) gets its own rather than overwriting the last.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from src.Deepseek.common.atomic_io import atomic_write_text

_session_stamp_cache: Dict[str, str] = {}
_session_stamp_lock = threading.Lock()


def _session_stamp_for_volume(volume_id: str) -> str:
    if volume_id in _session_stamp_cache:
        return _session_stamp_cache[volume_id]
    with _session_stamp_lock:
        if volume_id in _session_stamp_cache:
            return _session_stamp_cache[volume_id]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
        _session_stamp_cache[volume_id] = stamp
        return stamp


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(_render_block(b) for b in content if isinstance(b, dict))
    return repr(content)


def _render_block(block: Dict[str, Any]) -> str:
    btype = block.get("type", "?")
    if btype == "text":
        return str(block.get("text", ""))
    if btype == "thinking":
        text = str(block.get("thinking", ""))
        return f"_[thinking block, {len(text)} chars]_\n\n{text}"
    if btype == "tool_use":
        return (
            f"**Tool call:** `{block.get('name')}`\n```json\n"
            f"{json.dumps(block.get('input', {}), ensure_ascii=False, indent=2)}\n```"
        )
    if btype == "tool_result":
        return f"**Tool result:**\n```\n{block.get('content', '')}\n```"
    return f"```json\n{json.dumps(block, ensure_ascii=False, indent=2)}\n```"


def _render_message(msg: Dict[str, Any]) -> str:
    role = msg.get("role", "?")
    return f"### {role}\n\n{_content_to_text(msg.get('content', ''))}\n"


def write_dry_run_prompt(
    *, work_dir: Path, volume_id: str, chapter_id: str, payload: Dict[str, Any],
) -> Path:
    """Render `payload` (DeepSeekClient.generate()'s exact request kwargs)
    to a markdown file under WORK/<volume_id>/DRY_RUN/<run-stamp>/ and
    return its path."""
    from src.Deepseek.common.token_telemetry import count_tokens

    model = str(payload.get("model", ""))
    system_text = str(payload.get("system") or "")
    messages: List[Dict[str, Any]] = payload.get("messages") or []
    thinking = payload.get("thinking")
    output_config = payload.get("output_config")
    max_tokens = payload.get("max_tokens")

    combined_text = system_text + "\n".join(_content_to_text(m.get("content", "")) for m in messages if isinstance(m, dict))
    estimated_input_tokens = count_tokens(combined_text, model)

    thinking_line = (
        f"enabled, budget={thinking.get('budget_tokens')}" if isinstance(thinking, dict) else "disabled"
    )
    if isinstance(output_config, dict) and output_config.get("effort"):
        thinking_line += f", effort={output_config['effort']}"

    lines = [
        f"# Dry Run — {chapter_id}",
        "",
        f"- **Volume:** {volume_id}",
        f"- **Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- **Model:** {model}",
        f"- **Max output tokens:** {max_tokens}",
        f"- **Thinking:** {thinking_line}",
        f"- **Estimated input tokens (local tiktoken approximation):** {estimated_input_tokens:,}",
        "",
        "No API call was made — this is the exact payload DeepSeekClient.generate() "
        "assembled, captured right before the network call would have fired. "
        "Multi-turn conversation history is deliberately NOT assembled even when "
        "conversation mode is enabled: doing so for real risks triggering a real, "
        "billed checkpoint-summarization call mid-preparation, which would defeat "
        "the one guarantee dry-run exists to make. This is the raw single-turn "
        "system+user payload only.",
        "",
        "## system",
        "",
        system_text,
        "",
        "## messages",
        "",
    ]
    lines.extend(_render_message(m) for m in messages if isinstance(m, dict))

    stamp = _session_stamp_for_volume(volume_id)
    out_path = work_dir / "DRY_RUN" / stamp / f"{chapter_id}.md"
    atomic_write_text(out_path, "\n".join(lines) + "\n")
    return out_path
