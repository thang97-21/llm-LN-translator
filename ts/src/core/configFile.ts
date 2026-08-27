import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { CONFIG_FIELDS, type ConfigFieldSpec } from './configSchema.js';
import { pipelineRoot } from './mtls.js';

export const CONFIG_PATH = path.join(pipelineRoot, 'config.yaml');

type IndexedLine = { lineIndex: number; rawValue: string };

function detectEol(text: string): string { return text.includes('\r\n') ? '\r\n' : '\n'; }

// Walks the WHOLE file (unlike the Dashboard's translator:-only indexer in
// mtls.ts) mapping every leaf scalar to its line index and current raw
// value, keyed by the same dot-path CONFIG_FIELDS uses. Same indent-stack
// approach, just not gated to a single top-level section.
function indexConfigFile(lines: readonly string[]): Map<string, IndexedLine> {
  const index = new Map<string, IndexedLine>();
  const parents: Array<{ indent: number; key: string }> = [];
  lines.forEach((rawLine, lineIndex) => {
    const trimmed = rawLine.trim();
    if (!trimmed || trimmed.startsWith('#')) return;
    const indent = rawLine.length - rawLine.trimStart().length;
    const match = /^(?:[-]\s+)?([^:#]+):(?:\s*(.*))?$/.exec(trimmed);
    if (!match) return;
    const key = match[1]?.trim() ?? '';
    if (!key) return;
    const rawValue = (match[2] ?? '').replace(/\s+#.*$/, '').trim();
    while (parents.length && indent <= parents[parents.length - 1]!.indent) parents.pop();
    const fullPath = [...parents.map((parent) => parent.key), key].join('.');
    if (!rawValue) parents.push({ indent, key });
    else index.set(fullPath, { lineIndex, rawValue });
  });
  return index;
}

export type ConfigFieldState = ConfigFieldSpec & { rawValue: string; lineIndex: number | null };

export function loadConfigFields(): ConfigFieldState[] {
  if (!existsSync(CONFIG_PATH)) return CONFIG_FIELDS.map((spec) => ({ ...spec, rawValue: '', lineIndex: null }));
  try {
    const lines = readFileSync(CONFIG_PATH, 'utf8').split(/\r?\n/);
    const index = indexConfigFile(lines);
    return CONFIG_FIELDS.map((spec) => {
      const found = index.get(spec.path);
      return { ...spec, rawValue: found?.rawValue ?? '', lineIndex: found?.lineIndex ?? null };
    });
  } catch {
    return CONFIG_FIELDS.map((spec) => ({ ...spec, rawValue: '', lineIndex: null }));
  }
}

export type SaveResult = { ok: true } | { ok: false; error: string };

// Rewrites exactly one line's value, keeping its indentation, key text, and
// trailing inline comment untouched — config.yaml's comments explain fields
// the schema doesn't quote verbatim, so a save can never be allowed to eat
// one. Re-reads and re-indexes from disk on every call instead of trusting
// a cached line number, since a prior save in this same session already
// shifted nothing (single-line replace) but an external hand-edit might.
export function saveConfigField(fieldPath: string, newRawValue: string): SaveResult {
  if (!existsSync(CONFIG_PATH)) return { ok: false, error: 'config.yaml not found.' };
  try {
    const text = readFileSync(CONFIG_PATH, 'utf8');
    const eol = detectEol(text);
    const lines = text.split(/\r?\n/);
    const index = indexConfigFile(lines);
    const found = index.get(fieldPath);
    if (!found) return { ok: false, error: `Could not locate "${fieldPath}" in config.yaml.` };
    const original = lines[found.lineIndex] ?? '';
    const match = /^(\s*(?:-\s+)?[^:#]+:\s*)([^#]*?)(\s*#.*)?$/.exec(original);
    if (!match) return { ok: false, error: `Could not parse the existing line for "${fieldPath}".` };
    const [, prefix, , commentSuffix] = match;
    lines[found.lineIndex] = `${prefix}${newRawValue}${commentSuffix ?? ''}`;
    writeFileSync(CONFIG_PATH, lines.join(eol), 'utf8');
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}
