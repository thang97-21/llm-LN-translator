import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import type { CapabilitySpec, ChapterLog, PhaseStatus, PhaseStatusValue, RunHandle, SortMode, VolumeDetail, VolumeSummary } from './types.js';

// core/ lives at mtls-menu-ts/src/core. The menu package is two levels up;
// the Python pipeline it operates is its parent, not the menu directory.
export const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const pipelineRoot = path.resolve(packageRoot, '..');
export const workRoot = path.join(pipelineRoot, 'work');
export const inputRoot = path.join(pipelineRoot, 'raw');
export const mtlScript = path.join(pipelineRoot, 'scripts', 'mtl.py');

const phaseMap: ReadonlyArray<readonly [string, string]> = [['librarian', 'P1'], ['prep', 'P1.P'], ['translator', 'P2'], ['builder', 'P4']];
const asRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const asString = (value: unknown, fallback = ''): string => typeof value === 'string' && value.trim() ? value : fallback;
const asNumber = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) ? value : 0;

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
    if (!existsSync(manifestPath)) return [];
    try {
      const manifest = asRecord(JSON.parse(readFileSync(manifestPath, 'utf8')));
      const metadata = asRecord(manifest.metadata); const metadataEn = asRecord(manifest.metadata_en); const pipeline = asRecord(manifest.pipeline_state);
      const chapters = Array.isArray(manifest.chapters) ? manifest.chapters : [];
      const translatedCount = chapters.filter((chapter) => { const row = asRecord(chapter); return row.translation_status === 'completed' || row.state === 'DONE'; }).length;
      const title = asString(metadataEn.title_en, asString(metadata.title, entry.name));
      const phases: PhaseStatus[] = phaseMap.map(([key, label]) => ({ key, label, status: normalizePhaseStatus(asString(asRecord(pipeline[key]).status, 'pending')) }));
      return [{ id: entry.name, title, author: asString(metadataEn.author_en, asString(metadata.author, 'unknown')), series: deriveSeries(title), hasEnTitle: Boolean(asString(metadataEn.title_en)), updatedAt: statSync(manifestPath).mtimeMs, chapterCount: chapters.length, translatedCount, phases }];
    } catch { return []; }
  }).sort((a, b) => b.updatedAt - a.updatedAt);
}

export function loadEpubs(): string[] { return listFiles(inputRoot, '.epub'); }
function listFiles(dir: string, suffix: string): string[] { if (!existsSync(dir)) return []; try { return readdirSync(dir, { withFileTypes: true }).filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(suffix)).map((entry) => path.join(dir, entry.name)).sort((a, b) => a.localeCompare(b)); } catch { return []; } }
function listMarkdown(dir: string): string[] { return listFiles(dir, '.md'); }

export function listChapters(volumeId: string): string[] { return listMarkdown(path.join(workRoot, volumeId, 'JP')).map((file) => path.basename(file, '.md')); }

export function loadVolumeDetail(id: string): VolumeDetail { return loadVolumeDetailFrom(workRoot, id); }

export function loadVolumeDetailFrom(root: string, id: string): VolumeDetail {
  const base = path.join(root, id); const empty: VolumeDetail = { id, loaded: false, jpChapters: 0, enChapters: 0, enWords: 0, qcReports: 0, totalInputTokens: 0, totalOutputTokens: 0, successCount: 0, failCount: 0, aiIsmTotal: 0, lastError: null, logChapters: [] };
  if (!existsSync(base)) return empty;
  const jp = listMarkdown(path.join(base, 'JP')); const en = listMarkdown(path.join(base, 'EN'));
  let enWords = 0; for (const file of en) { try { enWords += readFileSync(file, 'utf8').trim().split(/\s+/).filter(Boolean).length; } catch { /* unreadable artifacts are simply absent from the view */ } }
  let qcReports = 0; try { const qc = path.join(base, 'QC'); qcReports = existsSync(qc) ? readdirSync(qc, { withFileTypes: true }).filter((entry) => entry.isFile()).length : 0; } catch { /* no QC yet */ }
  const logChapters: ChapterLog[] = []; let totalInputTokens = 0; let totalOutputTokens = 0; let successCount = 0; let failCount = 0; let aiIsmTotal = 0; let lastError: string | null = null;
  try {
    const log = asRecord(JSON.parse(readFileSync(path.join(base, 'translation_log.json'), 'utf8'))); const rows = Array.isArray(log.chapters) ? log.chapters : [];
    for (const raw of rows) { const row = asRecord(raw); const quality = asRecord(row.quality); const inputTokens = asNumber(row.input_tokens); const outputTokens = asNumber(row.output_tokens); const success = row.success === true; const error = typeof row.error === 'string' ? row.error : null; const aiIsmCount = typeof quality.ai_ism_count === 'number' ? quality.ai_ism_count : null; totalInputTokens += inputTokens; totalOutputTokens += outputTokens; success ? successCount += 1 : failCount += 1; aiIsmTotal += aiIsmCount ?? 0; if (error) lastError = error; logChapters.push({ chapterId: asString(row.chapter_id, '?'), inputTokens, outputTokens, success, error, passed: typeof quality.passed === 'boolean' ? quality.passed : null, aiIsmCount }); }
  } catch { /* translation log is optional */ }
  return { id, loaded: Boolean(jp.length || en.length || qcReports || logChapters.length), jpChapters: jp.length, enChapters: en.length, enWords, qcReports, totalInputTokens, totalOutputTokens, successCount, failCount, aiIsmTotal, lastError, logChapters };
}

export function fuzzyMatch(query: string, target: string): boolean { const q = query.trim().toLowerCase(); if (!q) return true; let index = 0; for (const char of target.toLowerCase()) if (char === q[index]) index += 1; return index === q.length; }
export function filterVolumes(volumes: readonly VolumeSummary[], query: string): VolumeSummary[] { return volumes.filter((volume) => fuzzyMatch(query, `${volume.id} ${volume.title} ${volume.author} ${volume.series}`)); }
export function sortVolumes(volumes: readonly VolumeSummary[], mode: SortMode): VolumeSummary[] { const copy = [...volumes]; if (mode === 'series') return copy.sort((a, b) => a.series.localeCompare(b.series) || b.updatedAt - a.updatedAt); if (mode === 'progress') return copy.sort((a, b) => (a.chapterCount ? a.translatedCount / a.chapterCount : 0) - (b.chapterCount ? b.translatedCount / b.chapterCount : 0)); return copy.sort((a, b) => b.updatedAt - a.updatedAt); }

export function pythonCommand(): string { const override = process.env.DEEPSEEK_MTLS_PYTHON; if (override && existsSync(override)) return override; const venv = path.join(pipelineRoot, 'venv', 'Scripts', 'python.exe'); return existsSync(venv) ? venv : 'python'; }

export function runCliCapability(spec: CapabilitySpec, argv: readonly string[], onText: (text: string, source: 'cli', severity?: 'info' | 'warning' | 'error' | 'success') => void, onDone: (code: number | null, cancelled: boolean) => void): RunHandle {
  if (spec.route.transport !== 'cli') throw new Error(`${spec.id} is not a CLI capability.`);
  const child = spawn(pythonCommand(), [mtlScript, ...argv], { cwd: pipelineRoot, env: process.env, shell: false }); let cancelled = false; let settled = false;
  const append = (source: 'stdout' | 'stderr') => (chunk: Buffer): void => onText(chunk.toString('utf8'), 'cli', source === 'stderr' ? 'warning' : 'info');
  child.stdout.on('data', append('stdout')); child.stderr.on('data', append('stderr'));
  child.on('error', (error) => { onText(`Failed to launch: ${error.message}`, 'cli', 'error'); if (!settled) { settled = true; onDone(1, cancelled); } });
  child.on('close', (code) => { if (!settled) { settled = true; onDone(code, cancelled); } });
  return { cancel: () => { if (!settled) { cancelled = true; child.kill(); } }, write: (data) => { if (!settled && child.stdin.writable) child.stdin.write(data); } };
}
