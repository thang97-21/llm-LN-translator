"""Validate the native agent and skill trees generated from .github assets."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_AGENTS = ROOT / ".github" / "agents"
SOURCE_ROOT_AGENT = ROOT / ".github" / "mirei-orchestrator.agent.md"
SOURCE_SKILLS = ROOT / ".github" / "skills"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):(?:[ \t]*(.*))?$", line)
        if match:
            fields[match.group(1)] = match.group(2) or ""
    return fields, text[end + len("\n---") :].lstrip("\n")


def frontmatter_value(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def expected_agent_names() -> set[str]:
    paths = sorted(SOURCE_AGENTS.rglob("*.agent.md"))
    if SOURCE_ROOT_AGENT.exists():
        paths.append(SOURCE_ROOT_AGENT)
    names: set[str] = set()
    for path in paths:
        name = path.name[: -len(".agent.md")] if path.name.endswith(".agent.md") else path.stem
        if path.parent == SOURCE_AGENTS / "qc_agents":
            name = f"qc-agents-{name}"
        names.add(name)
    return names


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)


def main() -> int:
    failures: list[str] = []
    expected_agents = expected_agent_names()
    expected_skills = {path.name for path in SOURCE_SKILLS.iterdir() if path.is_dir()}

    for client in ("claude", "grok"):
        root = ROOT / f".{client}" / "agents"
        paths = {path.stem for path in root.glob("*.md")}
        if paths != expected_agents:
            fail(f"{client} agent names differ: expected {len(expected_agents)}, got {len(paths)}", failures)
        for path in root.glob("*.md"):
            fields, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            if not fields.get("name") or not fields.get("description"):
                fail(f"{path}: missing native name or description", failures)
            description = frontmatter_value(fields.get("description", ""))
            if "DeepSeek_MTLS" in description:
                fail(f"{path}: native description still uses DeepSeek_MTLS branding", failures)
            if not description.startswith("CLI Agents"):
                fail(f"{path}: native description is missing CLI Agents branding", failures)
            forbidden = {"user-invocable", "reasoning_effort", "agents"} & set(fields)
            if forbidden:
                fail(f"{path}: Copilot-only frontmatter remains: {sorted(forbidden)}", failures)
            if ".github/skills/" in path.read_text(encoding="utf-8"):
                fail(f"{path}: stale .github skill path", failures)

    codex_root = ROOT / ".codex" / "agents"
    codex_paths = {path.stem for path in codex_root.glob("*.toml")}
    if codex_paths != expected_agents:
        fail(f"codex agent names differ: expected {len(expected_agents)}, got {len(codex_paths)}", failures)
    for path in codex_root.glob("*.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            fail(f"{path}: invalid TOML: {exc}", failures)
            continue
        missing = {"name", "description", "developer_instructions"} - set(data)
        if missing:
            fail(f"{path}: missing {sorted(missing)}", failures)
        description = data.get("description", "")
        if "DeepSeek_MTLS" in description:
            fail(f"{path}: native description still uses DeepSeek_MTLS branding", failures)
        if not description.startswith("CLI Agents"):
            fail(f"{path}: native description is missing CLI Agents branding", failures)
        if ".github/" in path.read_text(encoding="utf-8"):
            fail(f"{path}: stale .github path", failures)

    for root_name in (".agents/skills", ".claude/skills", ".grok/skills"):
        root = ROOT / root_name
        actual = {path.parent.name for path in root.glob("*/SKILL.md")}
        if actual != expected_skills:
            fail(f"{root_name}: expected {len(expected_skills)} root skills, got {len(actual)}", failures)
        if any(path.name == "SKILL.md" and path.parent.name == "references" for path in root.rglob("SKILL.md")):
            fail(f"{root_name}: nested references/SKILL.md would be discovered as a skill", failures)
        for path in root.glob("*/SKILL.md"):
            fields, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            if not fields.get("name") or not fields.get("description"):
                fail(f"{path}: missing skill name or description", failures)
            text = path.read_text(encoding="utf-8")
            if ".github/skills/" in text or ".github/agents/reference/" in text:
                fail(f"{path}: stale source path", failures)

    config = ROOT / ".codex" / "config.toml"
    try:
        agents_config = tomllib.loads(config.read_text(encoding="utf-8")).get("agents", {})
        if agents_config.get("enabled") is not True:
            fail(".codex/config.toml: agents.enabled is not true", failures)
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        fail(f".codex/config.toml: invalid or missing: {exc}", failures)

    if failures:
        print(json.dumps({"status": "failed", "failures": failures}, indent=2))
        return 1

    print(json.dumps({
        "status": "passed",
        "agents": len(expected_agents),
        "skills": len(expected_skills),
        "native_agent_roots": [".claude/agents", ".codex/agents", ".grok/agents"],
        "skill_roots": [".agents/skills", ".claude/skills", ".grok/skills"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
