"""Convert VS Code/Copilot agent assets into Claude, Codex, and Grok formats."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_AGENTS = ROOT / ".github" / "agents"
SOURCE_ROOT_AGENT = ROOT / ".github" / "mirei-orchestrator.agent.md"
SOURCE_SKILLS = ROOT / ".github" / "skills"

AGENT_TARGETS = {
    "claude": ROOT / ".claude" / "agents",
    "grok": ROOT / ".grok" / "agents",
    "codex": ROOT / ".codex" / "agents",
}
SKILL_TARGETS = {
    "portable": ROOT / ".agents" / "skills",
    "claude": ROOT / ".claude" / "skills",
    "grok": ROOT / ".grok" / "skills",
}

TOOL_MAP = {
    "read": "Read",
    "search": "Grep",
    "edit": "Edit",
    "write": "Write",
    "execute": "Bash",
    "agent": "Agent",
    "todo": "TodoWrite",
    "web": "WebSearch",
    "browser": "WebFetch",
    "vscode/askQuestions": "AskUserQuestion",
    "agent/runSubagent": "Agent",
}


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + len("\n---") :].lstrip("\n")
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+):(?:[ \t]*(.*))?$", line)
        if match:
            current_key = match.group(1)
            fields[current_key] = match.group(2) or ""
        elif current_key and line.startswith((" ", "\t")):
            fields[current_key] += "\n" + line.strip()
    return fields, body


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
        return value[1:-1]
    return value


def parse_list(value: str) -> list[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        return [unquote(item) for item in value[1:-1].split(",") if item.strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


def source_agents() -> list[Path]:
    files = sorted(SOURCE_AGENTS.rglob("*.agent.md"))
    if SOURCE_ROOT_AGENT.exists():
        files.append(SOURCE_ROOT_AGENT)
    return files


def agent_name(path: Path) -> str:
    stem = path.name
    if stem.endswith(".agent.md"):
        stem = stem[: -len(".agent.md")]
    if path.parent == SOURCE_AGENTS / "qc_agents":
        return f"qc-agents-{stem}"
    return stem


def portable_body(body: str, target: str, fields: dict[str, str]) -> str:
    skill_root = {
        "portable": ".agents/skills",
        "claude": ".claude/skills",
        "grok": ".grok/skills",
        "codex": ".agents/skills",
    }[target]
    reference_root = {
        "portable": ".agents/agent-reference",
        "claude": ".claude/agents/reference",
        "grok": ".grok/agents/reference",
        "codex": ".codex/agent-reference",
    }[target]
    body = body.replace(".github/skills/", f"{skill_root}/")
    body = body.replace(".github/agents/reference/", f"{reference_root}/")
    body = body.replace(".github/agents/", f"{reference_root.rsplit('/', 1)[0]}/")

    source_tools = parse_list(fields.get("tools", ""))
    delegates = parse_list(fields.get("agents", ""))
    metadata = [
        "\n## Portable agent metadata\n",
        "This definition was converted from a VS Code/Copilot agent manifest. "
        "The instruction body is authoritative; host-specific tool and model hints "
        "are recorded here without requiring Copilot-only syntax.\n",
    ]
    if source_tools:
        metadata.append(f"- Source tool hints: `{', '.join(source_tools)}`\n")
    if fields.get("model"):
        metadata.append(f"- Source model hint: `{unquote(fields['model'])}`\n")
    if fields.get("reasoning_effort"):
        metadata.append(
            f"- Source reasoning hint: `{unquote(fields['reasoning_effort'])}`\n"
        )
    if delegates:
        metadata.append(
            "- Delegated agent definitions: "
            + ", ".join(f"`{name}`" for name in delegates)
            + ". Ask the host to spawn these named agents when the workflow requires delegation.\n"
        )
    if fields.get("user-invocable", "").strip().lower() == "false":
        metadata.append(
            "- Intended role: normally delegated by an orchestrator; it remains explicitly invocable by name.\n"
        )
    metadata.append(
        f"- Portable skill root: `{skill_root}/`; supporting references remain relative to this project.\n"
    )
    return body.rstrip() + "\n" + "".join(metadata)


def native_description(fields: dict[str, str]) -> str:
    description = (
        unquote(fields.get("description", ""))
        .replace("\n", " ")
        .replace("DeepSeek_MTLS", "CLI Agents")
        .strip()
    )
    if description.startswith("CLI Agents"):
        return description
    return f"CLI Agents — {description}"


def claude_frontmatter(name: str, fields: dict[str, str]) -> str:
    description = native_description(fields)
    source_tools = parse_list(fields.get("tools", ""))
    tools: list[str] = []
    for source_tool in source_tools:
        mapped = TOOL_MAP.get(source_tool)
        if mapped and mapped not in tools:
            tools.append(mapped)
    lines = ["---", f"name: {name}", f"description: {json.dumps(description)}"]
    if tools:
        lines.append("tools: " + ", ".join(tools))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def grok_frontmatter(name: str, fields: dict[str, str]) -> str:
    description = native_description(fields)
    source_tools = parse_list(fields.get("tools", ""))
    tools: list[str] = []
    for source_tool in source_tools:
        mapped = TOOL_MAP.get(source_tool)
        if mapped and mapped not in tools:
            tools.append(mapped)
    lines = ["---", f"name: {name}", f"description: {json.dumps(description)}"]
    if tools:
        lines.append("tools: " + ", ".join(tools))
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def codex_agent(path: Path, fields: dict[str, str], body: str) -> str:
    name = agent_name(path)
    description = native_description(fields)
    instructions = portable_body(body, "codex", fields)
    source_tools = parse_list(fields.get("tools", ""))
    if source_tools and not any(item in {"edit", "write", "execute"} for item in source_tools):
        sandbox = "read-only"
    else:
        sandbox = "workspace-write"
    return (
        f"name = {json.dumps(name)}\n"
        f"description = {json.dumps(description)}\n"
        f"sandbox_mode = {json.dumps(sandbox)}\n"
        f"developer_instructions = {json.dumps(instructions, ensure_ascii=False)}\n"
    )


def copy_tree(source: Path, target: Path, replacements: dict[str, str]) -> int:
    count = 0
    if target.exists():
        shutil.rmtree(target)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if item.is_file() and item.name == "SKILL.md" and relative.parent.name == "references":
            relative = relative.with_name("reference-guide.md")
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".txt"}:
            text = item.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            destination.write_text(text, encoding="utf-8", newline="\n")
        else:
            shutil.copy2(item, destination)
        count += 1
    return count


def main() -> None:
    if not SOURCE_AGENTS.exists() or not SOURCE_SKILLS.exists():
        raise SystemExit("Expected .github/agents and .github/skills to exist")

    for target in AGENT_TARGETS.values():
        target.mkdir(parents=True, exist_ok=True)
    for target in SKILL_TARGETS.values():
        target.mkdir(parents=True, exist_ok=True)

    agents = source_agents()
    for path in agents:
        fields, body = split_frontmatter(path.read_text(encoding="utf-8"))
        name = agent_name(path)
        if not fields.get("description"):
            raise SystemExit(f"Agent has no description: {path}")
        for target_name, target_root in AGENT_TARGETS.items():
            if target_name == "codex":
                rendered = codex_agent(path, fields, body)
                destination = target_root / f"{name}.toml"
            else:
                rendered = (
                    (claude_frontmatter(name, fields) if target_name == "claude" else grok_frontmatter(name, fields))
                    + portable_body(body, target_name, fields)
                )
                destination = target_root / f"{name}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8", newline="\n")

    replacements = {
        ".github/skills/": ".agents/skills/",
        ".github/agents/reference/": ".agents/agent-reference/",
    }
    skill_files = copy_tree(SOURCE_SKILLS, SKILL_TARGETS["portable"], replacements)
    copy_tree(
        SOURCE_SKILLS,
        SKILL_TARGETS["claude"],
        {".github/skills/": ".claude/skills/", ".github/agents/reference/": ".claude/agents/reference/"},
    )
    copy_tree(
        SOURCE_SKILLS,
        SKILL_TARGETS["grok"],
        {".github/skills/": ".grok/skills/", ".github/agents/reference/": ".grok/agents/reference/"},
    )

    for target_name, reference_root in {
        "claude": ROOT / ".claude" / "agents" / "reference",
        "grok": ROOT / ".grok" / "agents" / "reference",
        "codex": ROOT / ".codex" / "agent-reference",
        "portable": ROOT / ".agents" / "agent-reference",
    }.items():
        copy_tree(
            SOURCE_AGENTS / "reference",
            reference_root,
            {
                ".github/skills/": {
                    "claude": ".claude/skills/",
                    "grok": ".grok/skills/",
                    "codex": ".agents/skills/",
                    "portable": ".agents/skills/",
                }[target_name],
            },
        )

    print(f"converted_agents={len(agents)}")
    print(f"copied_skill_files={skill_files}")
    print("skill_roots=.agents/skills,.claude/skills,.grok/skills")
    print("agent_roots=.claude/agents,.codex/agents,.grok/agents")


if __name__ == "__main__":
    main()
