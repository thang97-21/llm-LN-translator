"""
Dry-run prompt inspection shared by every Phase 2 provider.

Each provider assembles its exact request payload and returns it in
``LLMResponse.provider_metadata["payload"]`` instead of sending it. Every
provider drops conversation history inside its client before rendering a
preview, so a dry run remains credential-free and single-turn.

Nothing here assumes one wire shape. DeepSeek has a plain ``system`` string,
Qwen has cache-bearing system blocks, and OpenAI Responses has developer and
user items under ``input``. This module renders all three without changing the
payload a provider would send.

Per-project, like every other telemetry artifact this pipeline writes now
(LOG/, THINKING/) — WORK/<volume_id>/DRY_RUN/<run-stamp>/<chapter_id>.md.
Run-stamped the same way token_telemetry.py stamps token_log.md: established
once per process per volume, reused for every chapter in that run, so one
translate run's dry-run output collects into one folder and a genuinely new
run (new process) gets its own rather than overwriting the last.
"""

from __future__ import annotations

import hashlib
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
    if btype in {"text", "input_text", "output_text"}:
        return str(block.get("text", ""))
    if btype in {"thinking", "reasoning"}:
        text = str(block.get("thinking") or block.get("text") or "")
        return f"_[reasoning block, {len(text)} chars]_\n\n{text}"
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


def _cache_preview(payload: Dict[str, Any]) -> tuple[str, str, int]:
    options = payload.get("prompt_cache_options") or (
        (payload.get("extra_body") or {}).get("prompt_cache_options")
    ) or {}
    mode = str(options.get("mode") or "disabled")
    cache_key = str(payload.get("prompt_cache_key") or "")
    key_digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:12] if cache_key else "-"
    breakpoint_count = 0
    for item in payload.get("input") or []:
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("prompt_cache_breakpoint"):
                breakpoint_count += 1
    return mode, key_digest, breakpoint_count


def write_dry_run_prompt(
    *, work_dir: Path, volume_id: str, chapter_id: str, payload: Dict[str, Any],
    provider: str = "deepseek",
) -> Path:
    """Render `payload` (the client's exact request kwargs) to a markdown file
    under WORK/<volume_id>/DRY_RUN/<run-stamp>/ and return its path.

    Shared by both provider routes, so nothing here may assume DeepSeek's
    payload shape. `provider` only selects which client is named in the
    header — it must not change what is rendered.
    """
    from src.Deepseek.common.token_telemetry import count_tokens

    client_names = {"qwen": "QwenClient", "openai": "OpenAIClient"}
    client_name = client_names.get(str(provider).lower(), "DeepSeekClient")
    model = str(payload.get("model", ""))
    input_items: List[Dict[str, Any]] = payload.get("input") or []
    developer_items = [item for item in input_items if item.get("role") == "developer"]
    system_text = "\n".join(_content_to_text(item.get("content", "")) for item in developer_items)
    messages: List[Dict[str, Any]] = (
        [item for item in input_items if item.get("role") != "developer"]
        if input_items
        else payload.get("messages") or []
    )
    if not system_text:
        system_text = _content_to_text(payload.get("system") or "")
    thinking = payload.get("thinking")
    reasoning = payload.get("reasoning")
    output_config = payload.get("output_config")
    max_tokens = payload.get("max_tokens", payload.get("max_output_tokens"))
    cache_mode, cache_key_digest, breakpoint_count = _cache_preview(payload)

    combined_text = system_text + "\n".join(_content_to_text(m.get("content", "")) for m in messages if isinstance(m, dict))
    estimated_input_tokens = count_tokens(combined_text, model)

    if isinstance(reasoning, dict):
        thinking_line = f"mode={reasoning.get('mode', 'standard')}, effort={reasoning.get('effort', 'unspecified')}"
    else:
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
        f"- **Cache mode:** {cache_mode}",
        f"- **Prompt cache key SHA-256:** {cache_key_digest}",
        f"- **Explicit cache breakpoints:** {breakpoint_count}",
        f"- **Estimated input tokens (local tiktoken approximation):** {estimated_input_tokens:,}",
        "",
        f"No API call was made — this is the exact payload {client_name}.generate() "
        "assembled, captured immediately before the network call. Conversation "
        "history is deliberately absent even when conversation mode is enabled: "
        "a preview must represent one current-source turn, not frozen state from "
        "a previous real run.",
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
