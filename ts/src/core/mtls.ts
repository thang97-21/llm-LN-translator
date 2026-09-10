import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { manifestSchema, parseBoundary, translationLogSchema } from './boundary.js';
import { asChapterRef, asVolumeId } from './types.js';
import { resolveVolumeIdentity } from './volumeIdentity.js';
import type { CapabilitySpec, ChapterLog, ChapterRef, ConfigLine, PhaseStatus, PhaseStatusValue, RunHandle, SortMode, VolumeDetail, VolumeId, VolumeSummary } from './types.js';

// core/ lives at ts/src/core. The menu package is two levels up;
// the Python pipeline it operates is its parent, not the menu directory.
export const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const pipelineRoot = path.resolve(packageRoot, '..');
export const workRoot = path.join(pipelineRoot, 'work');
export const inputRoot = path.join(pipelineRoot, 'raw');
export const mtlScript = path.join(pipelineRoot, 'scripts', 'mtl.py');

const phaseMap: ReadonlyArray<readonly [string, string]> = [['librarian', 'P1'], ['prep', 'P1.P'], ['translator', 'P2'], ['builder', 'P4']];
const asString = (value: unknown, fallback = ''): string => typeof value === 'string' && value.trim() ? value : fallback;

function deriveSeries(title: string): string { return title.replace(/【[^】]*】|\([^)]*\)|（[^）]*）/g, ' ').replace(/\s+/g, ' ').trim() || title; }

export function normalizePhaseStatus(raw: string): PhaseStatusValue {
  switch (raw.trim().toLowerCase()) {
    case 'completed': case 'done': case 'success': return 'completed';
    case 'built': return 'built'; case 'reviewed': return 'reviewed';
    case 'running': case 'in_progress': case 'in progress': return 'running';
    case 'pending': case 'not run': case 'not_run': case '': return 'pending';
    case 'failed': case 'error': return 'failed'; default: return 'unknown';
  }
}

export function loadVolumes(): VolumeSummary[] { return loadVolumesFrom(workRoot); }

export function loadVolumesFrom(root: string): VolumeSummary[] {
  if (!existsSync(root)) return [];
  return readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory()).flatMap((entry) => {
    const manifestPath = path.join(root, entry.name, 'manifest.json');
    if (!existsSync(manifestPath)) {
      // A project folder can exist before Phase 1 (librarian) ever writes its
      // manifest.json, or after that file is lost/renamed — same "surface it,
      // don't vanish it" principle as the corrupt-manifest branch below.
      // context.xml / the extracted OPF resolve an identity independently of
      // manifest.json, so this still names the book instead of the directory,
      // and every phase reads as 'pending' (accurate: no manifest means no
      // phase has recorded state yet, unlike a corrupt manifest where we
      // genuinely cannot tell).
      let mtime = 0; try { mtime = statSync(path.join(root, entry.name)).mtimeMs; } catch { /* unreadable */ }
      const degraded = resolveVolumeIdentity(path.join(root, entry.name), undefined, undefined);
      const degradedTitle = degraded.title || entry.name;
      return [{ id: asVolumeId(entry.name), title: degradedTitle, author: degraded.author || 'unknown', publisher: degraded.publisher || 'unknown', series: degraded.series || deriveSeries(degradedTitle), hasEnTitle: degraded.hasEnglishTitle, updatedAt: mtime, chapterCount: 0, translatedCount: 0, phases: phaseMap.map(([key, label]) => ({ key, label, status: 'pending' as const })), manifestError: `${manifestPath}: no manifest.json found` }];
    }
    const parsed = parseBoundary(manifestSchema, readFileSync(manifestPath, 'utf8'), manifestPath);
    if (!parsed.ok) {
      // A corrupt manifest used to vanish silently (catch → []). That made a
      // Python-side schema drift indistinguishable from "no volumes yet."
      // The volume directory exists, so surface it as a degraded entry —
      // the Inspector shows the parse error where a title would be.
      let mtime = 0; try { mtime = statSync(manifestPath).mtimeMs; } catch { /* unreadable */ }
      // A corrupt manifest costs us its counts, not the volume's identity:
      // context.xml and the extracted OPF are written independently of it, so
      // a degraded entry still names the book instead of its directory.
      const degraded = resolveVolumeIdentity(path.join(root, entry.name), undefined, undefined);
      const degradedTitle = degraded.title || entry.name;
      return [{ id: asVolumeId(entry.name), title: degradedTitle, author: degraded.author || 'unknown', publisher: degraded.publisher || 'unknown', series: degraded.series || deriveSeries(degradedTitle), hasEnTitle: degraded.hasEnglishTitle, updatedAt: mtime, chapterCount: 0, translatedCount: 0, phases: phaseMap.map(([key, label]) => ({ key, label, status: 'unknown' as const })), manifestError: parsed.error }];
    }
    const manifest = parsed.data;
    const metadata = manifest.metadata ?? {}; const metadataEn = manifest.metadata_en ?? {}; const pipeline = manifest.pipeline_state ?? {};
    const chapters = manifest.chapters ?? [];
    const translatedCount = chapters.filter((chapter) => chapter.translation_status === 'completed' || chapter.state === 'DONE').length;
    // Identity is resolved per field across context.xml → manifest → the
    // extracted OPF rather than read out of the manifest alone; see
    // volumeIdentity.ts for why no single artifact can answer for a volume in
    // every pipeline state. Everything else on this record stays manifest-only.
    const identity = resolveVolumeIdentity(path.join(root, entry.name), metadata, metadataEn);
    const title = identity.title || entry.name;
    const phases: PhaseStatus[] = phaseMap.map(([key, label]) => {
      const phase = (pipeline as Record<string, { status?: string } | undefined>)[key];
      return { key, label, status: normalizePhaseStatus(asString(phase?.status, 'pending')) };
    });
    return [{ id: asVolumeId(entry.name), title, author: identity.author || 'unknown', publisher: identity.publisher || 'unknown', series: identity.series || deriveSeries(title), hasEnTitle: identity.hasEnglishTitle, updatedAt: statSync(manifestPath).mtimeMs, chapterCount: chapters.length, translatedCount, phases }];
  }).sort((a, b) => b.updatedAt - a.updatedAt);
}

export function loadEpubs(): string[] { return listFiles(inputRoot, '.epub'); }
// Grouped instead of flattened: a "Caching · Cache Monitor · Enabled: Enabled"
// chain repeated on every sibling line is unreadable past a dozen entries,
// and this config has ~50. Each nesting level renders once as its own
// section header, children just indent under it, and a gap line separates
// top-level sections so "Retry" doesn't visually bleed into "Streaming".
export function loadRuntimeConfigLines(): ConfigLine[] {
  const configPath = path.join(pipelineRoot, 'config.yaml');
  const unavailable: ConfigLine[] = [{ kind: 'value', depth: 0, label: 'Translator runtime configuration', value: 'unavailable', boolState: null }];
  if (!existsSync(configPath)) return unavailable;
  try {
    const lines = readFileSync(configPath, 'utf8').replace(/\r/g, '').split('\n');
    const result: ConfigLine[] = [];
    let inTranslator = false;
    let translatorIndent = 0;
    let sawAny = false;
    const parents: Array<{ indent: number }> = [];
    // Show the active provider menu, not a stale amalgam of every route.
    // Scoped to the translation block: prep.provider (the prep-model route)
    // sits at the same indent earlier in the file and would shadow this match.
    const joined = lines.join('\n');
    const translationStart = joined.indexOf('\ntranslation:');
    const providerMatch = /^\s*provider:\s*(deepseek|qwen|openai|anthropic)\s*(?:#.*)?$/m.exec(translationStart >= 0 ? joined.slice(translationStart) : joined);
    const menuKey = `${providerMatch?.[1] ?? 'deepseek'}:`;
    for (const rawLine of lines) {
      const trimmed = rawLine.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const indent = rawLine.length - rawLine.trimStart().length;
      if (!inTranslator) {
        if (trimmed === menuKey) { inTranslator = true; translatorIndent = indent; }
        continue;
      }
      if (indent <= translatorIndent) break;
      const match = /^(?:[-]\s+)?([^:#]+):(?:\s*(.*))?$/.exec(trimmed);
      if (!match) continue;
      const key = match[1]?.trim() ?? '';
      if (!key || key === 'api_key_env') continue;
      const rawValue = (match[2] ?? '').replace(/\s+#.*$/, '').trim();
      while (parents.length && indent <= parents[parents.length - 1]!.indent) parents.pop();
      const depth = parents.length;
      const label = humanizeConfigKey(key);
      if (!rawValue) {
        if (depth === 0 && sawAny) result.push({ kind: 'gap' });
        result.push({ kind: 'group', depth, label });
        parents.push({ indent });
      } else {
        const { text, boolState } = humanizeConfigValue(key, rawValue);
        result.push({ kind: 'value', depth, label, value: text, boolState });
      }
      sawAny = true;
    }
    return result.length ? result : unavailable;
  } catch (error) {
    return [{ kind: 'value', depth: 0, label: 'Unable to read Translator configuration', value: error instanceof Error ? error.message : String(error), boolState: null }];
  }
}

const CONFIG_LABELS: Readonly<Record<string, string>> = {
  enabled: 'Status',
  master_prompt: 'Primary translation prompt',
  master_prompt_v2: 'Continuity translation prompt',
  http_timeout_seconds: 'Request timeout',
  max_output_tokens: 'Maximum output tokens',
  top_p: 'Sampling nucleus',
  budget_tokens: 'Thinking token budget',
  recent_verbatim_chapters: 'Recent verbatim chapters',
  include_exact_jp_task: 'Include exact Japanese source',
  persistence_file: 'Conversation ledger',
  checkpoint_trigger_ratio: 'Checkpoint trigger',
  checkpoint_max_output_tokens: 'Checkpoint maximum output',
  checkpoint_stuck_guard_turns: 'Checkpoint stuck guard',
  soft_notice_ratio: 'Soft notice threshold',
  tool_snip_ratio: 'Tool trim threshold',
  checkpoint_force_ratio: 'Forced checkpoint threshold',
  fail_closed_on_checkpoint_error: 'Stop on checkpoint error',
  warn_threshold_cache_hit_ratio: 'Cache warning threshold',
  warn_threshold_breakpoint_success_rate: 'Breakpoint success threshold',
  warn_threshold_prefix_recovery: 'Prefix recovery threshold',
  min_repeat_calls_before_warning: 'Minimum repeat calls',
  thinking_analytics: 'Thinking analytics',
  concurrent_chapters: 'Concurrent chapters',
  max_concurrent: 'Maximum concurrent chapters',
  scene_break_formatting: 'Scene-break formatting',
  cjk_cleanup: 'CJK cleanup',
  salvage_reasoning_leaked_answer: 'Recover leaked reasoning answers',
  max_retries: 'Maximum retries',
  base_delay_ms: 'Retry base delay',
  max_delay_ms: 'Retry maximum delay',
  jitter_factor: 'Retry jitter',
  max_529_retries: 'Maximum overload retries',
  anthropic_version: 'API version',
  display: 'Thinking display',
  poll_seconds: 'Poll interval (seconds)',
  completion_window: 'Completion window',
};

function humanizeConfigKey(key: string): string {
  return CONFIG_LABELS[key] ?? key.replace(/_/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

// Keep endpoint labels meaningful across the three provider-specific transports;
// unknown custom endpoints remain visible verbatim rather than being mislabeled.
function humanizeConfigValue(key: string, value: string): { text: string; boolState: 'on' | 'off' | null } {
  if (key === 'endpoint') {
    if (value.includes('api.openai.com')) return { text: 'OpenAI Responses', boolState: null };
    if (value.includes('dashscope')) return { text: 'Qwen Anthropic Messages', boolState: null };
    if (value.includes('api.deepseek.com/anthropic')) return { text: 'DeepSeek Anthropic Messages', boolState: null };
    if (value.includes('api.deepseek.com')) return { text: 'DeepSeek OpenAI-compatible', boolState: null };
    if (value.includes('api.anthropic.com')) return { text: 'Anthropic Messages', boolState: null };
    return { text: value, boolState: null };
  }
  if (value === 'true') return { text: 'Enabled', boolState: 'on' };
  if (value === 'false') return { text: 'Disabled', boolState: 'off' };
  if (value === 'on') return { text: 'On', boolState: 'on' };
  if (value === 'off') return { text: 'Off', boolState: 'off' };
  if (value === 'deepseek-v4-pro') return { text: 'DeepSeek-V4-Pro-0813', boolState: null };
  if (value === 'deepseek-v4-flash') return { text: 'DeepSeek-V4-Flash-0731', boolState: null };
  if (value === 'gpt-6-astra') return { text: 'GPT-6 Astra', boolState: null };
  if (value === 'gpt-5.6-sol') return { text: 'GPT-5.6 Sol', boolState: null };
  if (value === 'gpt-5.6-terra') return { text: 'GPT-5.6 Terra', boolState: null };
  if (value === 'gpt-5.6-luna') return { text: 'GPT-5.6 Luna', boolState: null };
  if (value === 'claude-sonnet-5') return { text: 'Claude Sonnet 5', boolState: null };
  if (value === 'claude-opus-5') return { text: 'Claude Opus 5', boolState: null };
  if (value === 'claude-fable-5-1') return { text: 'Claude Fable 5.1', boolState: null };
  // Retired 2026-09-03, kept so an unmigrated config still renders a readable
  // label instead of a bare id; the client normalises it to claude-fable-5-1.
  if (value === 'claude-fable-5' || value === 'claude-fable-5.1') return { text: 'Claude Fable 5.1', boolState: null };
  return { text: value, boolState: null };
}
function listFiles(dir: string, suffix: string): string[] { if (!existsSync(dir)) return []; try { return readdirSync(dir, { withFileTypes: true }).filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(suffix)).map((entry) => path.join(dir, entry.name)).sort((a, b) => a.localeCompare(b)); } catch { return []; } }
function listMarkdown(dir: string): string[] { return listFiles(dir, '.md'); }

export function listChapters(volumeId: VolumeId): ChapterRef[] { return listMarkdown(path.join(workRoot, volumeId, 'JP')).map((file) => asChapterRef(path.basename(file, '.md'))); }

export function loadVolumeDetail(id: VolumeId): VolumeDetail { return loadVolumeDetailFrom(workRoot, id); }

export function loadVolumeDetailFrom(root: string, id: VolumeId): VolumeDetail {
  const base = path.join(root, id); const empty: VolumeDetail = { id, loaded: false, jpChapters: 0, enChapters: 0, enWords: 0, qcReports: 0, totalInputTokens: 0, totalOutputTokens: 0, successCount: 0, failCount: 0, aiIsmTotal: 0, lastError: null, logChapters: [] };
  if (!existsSync(base)) return empty;
  const jp = listMarkdown(path.join(base, 'JP')); const en = listMarkdown(path.join(base, 'EN'));
  let enWords = 0; for (const file of en) { try { enWords += readFileSync(file, 'utf8').trim().split(/\s+/).filter(Boolean).length; } catch { /* unreadable artifacts are simply absent from the view */ } }
  let qcReports = 0; try { const qc = path.join(base, 'QC'); qcReports = existsSync(qc) ? readdirSync(qc, { withFileTypes: true }).filter((entry) => entry.isFile()).length : 0; } catch { /* no QC yet */ }
  const logChapters: ChapterLog[] = []; let totalInputTokens = 0; let totalOutputTokens = 0; let successCount = 0; let failCount = 0; let aiIsmTotal = 0; let lastError: string | null = null;
  try {
    const logRaw = readFileSync(path.join(base, 'translation_log.json'), 'utf8');
    const logParsed = parseBoundary(translationLogSchema, logRaw, path.join(base, 'translation_log.json'));
    // The log is optional (a volume that never translated has none), but a
    // PRESENT log that fails schema is worth one console line — same
    // legible-corruption principle as the manifest, quieter because the
    // detail view has a lastError slot to carry it.
    if (!logParsed.ok) { lastError = logParsed.error; }
    const rows = logParsed.ok ? logParsed.data.chapters ?? [] : [];
    for (const row of rows) { const quality = row.quality ?? {}; const inputTokens = row.input_tokens ?? 0; const outputTokens = row.output_tokens ?? 0; const success = row.success === true; const error = typeof row.error === 'string' ? row.error : null; const aiIsmCount = typeof quality.ai_ism_count === 'number' ? quality.ai_ism_count : null; totalInputTokens += inputTokens; totalOutputTokens += outputTokens; success ? successCount += 1 : failCount += 1; aiIsmTotal += aiIsmCount ?? 0; if (error) lastError = error; logChapters.push({ chapterId: asChapterRef(asString(row.chapter_id, '?')), inputTokens, outputTokens, success, error, passed: typeof quality.passed === 'boolean' ? quality.passed : null, aiIsmCount }); }
  } catch { /* translation log is optional */ }
  return { id, loaded: Boolean(jp.length || en.length || qcReports || logChapters.length), jpChapters: jp.length, enChapters: en.length, enWords, qcReports, totalInputTokens, totalOutputTokens, successCount, failCount, aiIsmTotal, lastError, logChapters };
}

export function fuzzyMatch(query: string, target: string): boolean { const q = query.trim().toLowerCase(); if (!q) return true; let index = 0; for (const char of target.toLowerCase()) if (char === q[index]) index += 1; return index === q.length; }
export function filterVolumes(volumes: readonly VolumeSummary[], query: string): VolumeSummary[] { return volumes.filter((volume) => fuzzyMatch(query, `${volume.id} ${volume.title} ${volume.author} ${volume.publisher} ${volume.series}`)); }
export function sortVolumes(volumes: readonly VolumeSummary[], mode: SortMode): VolumeSummary[] { const copy = [...volumes]; if (mode === 'series') return copy.sort((a, b) => a.series.localeCompare(b.series) || b.updatedAt - a.updatedAt); if (mode === 'progress') return copy.sort((a, b) => (a.chapterCount ? a.translatedCount / a.chapterCount : 0) - (b.chapterCount ? b.translatedCount / b.chapterCount : 0)); return copy.sort((a, b) => b.updatedAt - a.updatedAt); }

// The project .venv is DEVELOPMENT AND TESTING ONLY and is deliberately never
// used by the runtime — the pipeline runs on the machine's system Python, where
// its dependencies are installed. Pin a specific interpreter with
// LLM_TRANSLATOR_PYTHON; do NOT re-add a project-venv probe here, or the TUI and
// the CLI launchers will silently disagree about which interpreter runs a phase.
// The bare fallback differs by platform: PEP 394 only guarantees `python3` on
// macOS/Linux — plenty of current distros ship no `python` symlink at all —
// while the python.org Windows installer only ever produces `python.exe`,
// never `python3.exe`.
export function pythonCommand(): string { const override = process.env.LLM_TRANSLATOR_PYTHON; if (override && existsSync(override)) return override; return process.platform === 'win32' ? 'python' : 'python3'; }

export function runCliCapability(spec: CapabilitySpec, argv: readonly string[], onText: (text: string, source: 'cli', severity?: 'info' | 'warning' | 'error' | 'success') => void, onDone: (code: number | null, cancelled: boolean) => void): RunHandle {
  if (spec.route.transport !== 'cli') throw new Error(`${spec.id} is not a CLI capability.`);
  const child = spawn(pythonCommand(), [mtlScript, ...argv], { cwd: pipelineRoot, env: process.env, shell: false }); let cancelled = false; let settled = false;
  const append = (source: 'stdout' | 'stderr') => (chunk: Buffer): void => onText(chunk.toString('utf8'), 'cli', source === 'stderr' ? 'warning' : 'info');
  child.stdout.on('data', append('stdout')); child.stderr.on('data', append('stderr'));
  child.on('error', (error) => { onText(`Failed to launch: ${error.message}`, 'cli', 'error'); if (!settled) { settled = true; onDone(1, cancelled); } });
  child.on('close', (code) => { if (!settled) { settled = true; onDone(code, cancelled); } });
  return { cancel: () => { if (!settled) { cancelled = true; child.kill(); } }, write: (data) => { if (!settled && child.stdin.writable) child.stdin.write(data); } };
}
