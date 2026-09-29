"""Validate the native agent and skill trees generated from .github assets."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SOURCE_AGENTS = ROOT / ".github" / "agents"
SOURCE_ROOT_AGENT = ROOT / ".github" / "mirei-orchestrator.agent.md"
SOURCE_SKILLS = ROOT / ".github" / "skills"
EVALUATOR = "translation-evaluator"
OBSOLETE = {"mtl-quality-evaluator", "qc-structural", "qc-names", "qc-linguistic", "qc-prose", "qc-prose-character", "qc-prose-narrator", "qc-prose-emotional-peak"}


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
        wanted = expected_skills if root_name != ".grok/skills" else expected_skills - {EVALUATOR}
        if actual != wanted:
            fail(f"{root_name}: expected {sorted(wanted)}, got {sorted(actual)}", failures)
        if any(path.name == "SKILL.md" and path.parent.name == "references" for path in root.rglob("SKILL.md")):
            fail(f"{root_name}: nested references/SKILL.md would be discovered as a skill", failures)
        for path in root.glob("*/SKILL.md"):
            fields, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            if not fields.get("name") or not fields.get("description"):
                fail(f"{path}: missing skill name or description", failures)
            text = path.read_text(encoding="utf-8")
            if ".github/skills/" in text or ".github/agents/reference/" in text:
                fail(f"{path}: stale source path", failures)

    authoritative = ROOT / ".agents" / "skills" / EVALUATOR
    claude_entry = ROOT / ".claude" / "skills" / EVALUATOR / "SKILL.md"
    vscode_entry = SOURCE_SKILLS / EVALUATOR / "SKILL.md"
    for entry in (authoritative / "SKILL.md", claude_entry, vscode_entry):
        if not entry.is_file():
            fail(f"missing evaluator entrypoint: {entry}", failures)
            continue
        fields, body = split_frontmatter(entry.read_text(encoding="utf-8"))
        if frontmatter_value(fields.get("name", "")) != EVALUATOR or not fields.get("description"):
            fail(f"{entry}: invalid evaluator frontmatter", failures)
        if any(key in fields for key in ("model", "agents", "thinkingBudget")):
            fail(f"{entry}: model/delegation pinned in frontmatter", failures)
        if entry != authoritative / "SKILL.md" and "../../../.agents/skills/translation-evaluator/SKILL.md" not in body:
            fail(f"{entry}: does not load authoritative skill", failures)
    optional_samples = authoritative / "references" / "golden-samples"
    optional_corpus = authoritative / "references" / "Seven_Seas_REPO"
    evaluator_docs = [authoritative / "SKILL.md", claude_entry, vscode_entry, *sorted((authoritative / "references").glob("*.md"))]
    for doc in evaluator_docs:
        if not doc.is_file():
            continue
        for link in re.findall(r"\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
            target = unquote(link.split("#", 1)[0].strip().strip("<>"))
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                continue
            resolved = (doc.parent / target).resolve()
            if not resolved.exists() and not (resolved.is_relative_to(optional_samples.resolve()) or resolved.is_relative_to(optional_corpus.resolve())):
                fail(f"{doc}: broken relative link {target}", failures)
    index = optional_samples / "golden-samples-index.md"
    if not index.is_file():
        fail(f"missing shared golden-sample index: {index}", failures)
    elif any(path.name != index.name for path in optional_samples.glob("*.md")) and not all((index.parent / target).is_file() for target in re.findall(r"\]\(([^)#]+\.md)\)", index.read_text(encoding="utf-8"))):
        fail(f"{index}: broken sample link", failures)
    if optional_corpus.is_dir() and not any(optional_corpus.iterdir()):
        fail(f"empty optional raw corpus: {optional_corpus}", failures)
    for root in (ROOT / ".claude" / "skills", ROOT / ".grok" / "skills"):
        if any(root.rglob("Seven_Seas_REPO")):
            fail(f"duplicate raw corpus under {root}", failures)
    for root in (ROOT / ".agents", ROOT / ".claude", ROOT / ".codex", ROOT / ".grok"):
        for name in OBSOLETE:
            if any(root.rglob(f"{name}.md")) or any(root.rglob(f"{name}.toml")) or any(root.rglob(f"{name}/SKILL.md")):
                fail(f"{root}: obsolete evaluator asset {name}", failures)
    for root in (ROOT / ".agents" / "agent-reference", ROOT / ".claude" / "agents" / "reference", ROOT / ".grok" / "agents" / "reference", ROOT / ".codex" / "agent-reference"):
        if root.exists():
            fail(f"duplicate agent reference tree: {root}", failures)

    # Host-level agent enablement is a user setting, not an asset invariant.
    config = ROOT / ".codex" / "config.toml"
    if config.exists():
        try:
            tomllib.loads(config.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            fail(f".codex/config.toml: invalid TOML: {exc}", failures)

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
