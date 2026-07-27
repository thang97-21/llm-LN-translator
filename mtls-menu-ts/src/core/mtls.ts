import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import type {
  ChapterLog,
  CommandSpec,
  PhaseStatus,
  PhaseStatusValue,
  RunHandle,
  RunState,
  SortMode,
  VolumeDetail,
  VolumeSummary,
} from './types.js';

// This module lives at src/core/mtls.ts, so climb two levels to reach the package root.
// Unlike the main pipeline's mtls-menu-ts (one level further up, in a sibling
// directory), DeepSeek_MTLS's TUI lives inside the client it drives — the
// package root IS the pipeline root.
export const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const pipelineRoot = packageRoot;
export const workRoot = path.join(pipelineRoot, 'work');
export const inputRoot = path.join(pipelineRoot, 'raw');
export const mtlScript = path.join(pipelineRoot, 'scripts', 'mtl.py');

// `as const satisfies` keeps the literal id/kind/risk types (so downstream code
// gets a precise union) while still validating every entry against CommandSpec.
// Stripped to what this client actually has — no metadata-phase RAG modules,
// no visual/scene-planner phases, no batch flag (DeepSeek has no Batch API),
// no multimodal flag (no Phase 1.6/vision here). `prep` here is the lightweight
// unified-DeepSeek-call context.xml builder (src/prep/agent.py), not the main
// pipeline's 7-phase metadata prep. `qc` is the filesystem-only sanity gate
// (src/qc/agent.py), not the 3-model fan-out evaluator.
export const commands = [
  {
    id: 'extract',
    label: 'Extract EPUB',
    detail: 'Phase 1: extract an EPUB into work/ and initialize manifest artifacts.',
    argv: ['extract'],
    kind: 'epub',
    risk: 'medium',
  },
  {
    id: 'prep',
    label: 'Prep Volume',
    detail: 'Unified DeepSeek call: fills every context.xml block. No Gemini, no main-pipeline dependency.',
    argv: ['prep'],
    kind: 'volume',
    risk: 'medium',
  },
  {
    id: 'translate',
    label: 'Translate Volume',
    detail: 'Phase 2: DeepSeek V4 Pro translation for the selected volume.',
    argv: ['translate'],
    kind: 'volume',
    risk: 'high',
  },
  {
    id: 'qc',
    label: 'QC Volume',
    detail: 'Filesystem-only sanity gate: completeness, truncation, token outliers, name drift. Zero API cost.',
    argv: ['qc'],
    kind: 'volume',
    risk: 'low',
  },
  {
    id: 'build',
    label: 'Build EPUB',
    detail: 'Phase 4: package the final translated EPUB output.',
    argv: ['build'],
    kind: 'volume',
    risk: 'medium',
  },
  {
    id: 'run',
    label: 'Full Pipeline',
    detail: 'extract -> prep -> translate -> qc -> build in one pass.',
    argv: ['run'],
    kind: 'epub',
    risk: 'high',
  },
  {
    id: 'list',
    label: 'List Volumes',
    detail: 'Print canonical Python volume listing.',
    argv: ['list'],
    kind: 'none',
    risk: 'low',
  },
  {
    id: 'status',
    label: 'Status',
    detail: 'Show pipeline state + chapter completion summary for the selected volume.',
    argv: ['status'],
    kind: 'volume',
    risk: 'low',
  },
  {
    id: 'legacy',
    label: 'Legacy Python CLI',
    detail: 'Unmount this shell and hand control to scripts/mtl.py.',
    argv: [],
    kind: 'legacy',
    risk: 'low',
  },
] as const satisfies readonly CommandSpec[];

// QC is a gate, not a pipeline phase — it runs as a standalone tool/command,
// not in this strip (matches PLANNING.md Step 8.2).
const phaseMap: ReadonlyArray<readonly [string, string, string]> = [
  ['librarian', 'P1', 'librarian'],
  ['prep', 'P1.P', 'prep'],
  ['translator', 'P2', 'translator'],
  ['builder', 'P4', 'builder'],
];

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

// Best-effort series key: drop edition/bonus brackets and trailing volume markers
// so sequels of the same series group together.
function deriveSeries(title: string): string {
  const stripped = title
    .replace(/【[^】]*】/g, ' ')
    .replace(/\([^)]*\)/g, ' ')
    .replace(/（[^）]*）/g, ' ')
    .replace(/\b[Vv]ol\.?\s*\d+\b/g, ' ')
    .replace(/[:：]?\s*(?:第)?\s*\d+\s*(?:巻)?\s*$/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return stripped || title.trim();
}

// Collapse the open-ended set of manifest status strings into the closed
// PhaseStatusValue union so the view layer can render it exhaustively.
export function normalizePhaseStatus(raw: string): PhaseStatusValue {
  switch (raw.trim().toLowerCase()) {
    case 'completed':
    case 'done':
    case 'success':
      return 'completed';
    case 'built':
      return 'built';
    case 'reviewed':
      return 'reviewed';
    case 'running':
    case 'in_progress':
    case 'in progress':
      return 'running';
    case 'pending':
    case 'not run':
    case 'not_run':
    case '':
      return 'pending';
    case 'failed':
    case 'error':
      return 'failed';
    default:
      return 'unknown';
  }
}

export function loadVolumes(): VolumeSummary[] {
  if (!existsSync(workRoot)) {
    return [];
  }

  return readdirSync(workRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const volumePath = path.join(workRoot, entry.name);
      const manifestPath = path.join(volumePath, 'manifest.json');
      if (!existsSync(manifestPath)) {
        return null;
      }

      try {
        const manifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as unknown;
        const root = asRecord(manifest);
        const metadataEn = asRecord(root.metadata_en);
        const metadata = asRecord(root.metadata);
        const pipelineState = asRecord(root.pipeline_state);
        const chapters = Array.isArray(root.chapters) ? root.chapters : [];
        const translatedCount = chapters.filter((chapter) => {
          const item = asRecord(chapter);
          return item.translation_status === 'completed' || item.state === 'DONE';
        }).length;

        const phases: PhaseStatus[] = phaseMap.map(([stateKey, label, fallbackKey]) => {
          const state = asRecord(pipelineState[stateKey] ?? pipelineState[fallbackKey]);
          return {
            key: stateKey,
            label,
            status: normalizePhaseStatus(stringValue(state.status, 'pending')),
          };
        });

        const enTitle = stringValue(metadataEn.title_en);
        const title = enTitle || stringValue(metadata.title, entry.name);

        return {
          id: entry.name,
          title,
          author: stringValue(metadataEn.author_en, stringValue(metadata.author, 'unknown')),
          series: deriveSeries(title),
          hasEnTitle: Boolean(enTitle),
          updatedAt: statSync(manifestPath).mtimeMs,
          chapterCount: chapters.length,
          translatedCount,
          phases,
        } satisfies VolumeSummary;
      } catch {
        return null;
      }
    })
    .filter((item): item is VolumeSummary => item !== null)
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

export function loadEpubs(): string[] {
  if (!existsSync(inputRoot)) {
    return [];
  }

  return readdirSync(inputRoot, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith('.epub'))
    .map((entry) => path.join(inputRoot, entry.name))
    .sort((a, b) => a.localeCompare(b));
}

function listMarkdown(dir: string): string[] {
  if (!existsSync(dir)) {
    return [];
  }
  try {
    return readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith('.md'))
      .map((entry) => path.join(dir, entry.name));
  } catch {
    return [];
  }
}

// Lazily mine one volume's on-disk artifacts (translation_log.json + EN/JP/QC
// folders) for the detail pane. Read-only; never touches the manifest.
export function loadVolumeDetail(id: string): VolumeDetail {
  const base = path.join(workRoot, id);
  const empty: VolumeDetail = {
    id,
    loaded: false,
    jpChapters: 0,
    enChapters: 0,
    enWords: 0,
    qcReports: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    successCount: 0,
    failCount: 0,
    aiIsmTotal: 0,
    lastError: null,
    logChapters: [],
  };
  if (!existsSync(base)) {
    return empty;
  }

  const jpFiles = listMarkdown(path.join(base, 'JP'));
  const enFiles = listMarkdown(path.join(base, 'EN'));

  let enWords = 0;
  for (const file of enFiles) {
    try {
      const words = readFileSync(file, 'utf8').split(/\s+/).filter((word) => word.length > 0);
      enWords += words.length;
    } catch {
      // Skip unreadable chapter.
    }
  }

  let qcReports = 0;
  const qcPath = path.join(base, 'QC');
  if (existsSync(qcPath)) {
    try {
      qcReports = readdirSync(qcPath, { withFileTypes: true }).filter((entry) => entry.isFile()).length;
    } catch {
      qcReports = 0;
    }
  }

  const logChapters: ChapterLog[] = [];
  let totalInputTokens = 0;
  let totalOutputTokens = 0;
  let successCount = 0;
  let failCount = 0;
  let aiIsmTotal = 0;
  let lastError: string | null = null;

  const logPath = path.join(base, 'translation_log.json');
  if (existsSync(logPath)) {
    try {
      const parsed = asRecord(JSON.parse(readFileSync(logPath, 'utf8')));
      const rows = Array.isArray(parsed.chapters) ? parsed.chapters : [];
      for (const raw of rows) {
        const item = asRecord(raw);
        const quality = asRecord(item.quality);
        const inputTokens = numberValue(item.input_tokens);
        const outputTokens = numberValue(item.output_tokens);
        const success = item.success === true;
        const error = typeof item.error === 'string' ? item.error : null;
        const passed = typeof quality.passed === 'boolean' ? quality.passed : null;
        const aiIsmCount = typeof quality.ai_ism_count === 'number' ? quality.ai_ism_count : null;

        totalInputTokens += inputTokens;
        totalOutputTokens += outputTokens;
        if (success) {
          successCount += 1;
        } else {
          failCount += 1;
        }
        if (aiIsmCount) {
          aiIsmTotal += aiIsmCount;
        }
        if (error) {
          lastError = error;
        }
        logChapters.push({
          chapterId: stringValue(item.chapter_id, '?'),
          inputTokens,
          outputTokens,
          success,
          error,
          passed,
          aiIsmCount,
        });
      }
    } catch {
      // Leave log-derived fields at their zero defaults.
    }
  }

  return {
    id,
    loaded: jpFiles.length > 0 || enFiles.length > 0 || logChapters.length > 0 || qcReports > 0,
    jpChapters: jpFiles.length,
    enChapters: enFiles.length,
    enWords,
    qcReports,
    totalInputTokens,
    totalOutputTokens,
    successCount,
    failCount,
    aiIsmTotal,
    lastError,
    logChapters,
  };
}

// Case-insensitive subsequence match — the query chars must appear in order.
export function fuzzyMatch(query: string, target: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) {
    return true;
  }
  const t = target.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti += 1) {
    if (t[ti] === q[qi]) {
      qi += 1;
    }
  }
  return qi === q.length;
}

export function filterVolumes(volumes: VolumeSummary[], query: string): VolumeSummary[] {
  if (!query.trim()) {
    return volumes;
  }
  return volumes.filter((volume) => fuzzyMatch(query, `${volume.title} ${volume.id} ${volume.author} ${volume.series}`));
}

export function filterEpubs(epubs: string[], query: string): string[] {
  if (!query.trim()) {
    return epubs;
  }
  return epubs.filter((epub) => fuzzyMatch(query, epub));
}

export function sortVolumes(volumes: VolumeSummary[], mode: SortMode): VolumeSummary[] {
  const copy = [...volumes];
  switch (mode) {
    case 'recent':
      return copy.sort((a, b) => b.updatedAt - a.updatedAt);
    case 'series':
      return copy.sort((a, b) => a.series.localeCompare(b.series) || b.updatedAt - a.updatedAt);
    case 'progress': {
      const ratio = (volume: VolumeSummary): number =>
        volume.chapterCount > 0 ? volume.translatedCount / volume.chapterCount : 0;
      return copy.sort((a, b) => ratio(a) - ratio(b) || b.updatedAt - a.updatedAt);
    }
    default:
      return assertNever(mode);
  }
}

export function pythonCommand(): string {
  const override = process.env.DEEPSEEK_MTLS_PYTHON;
  if (override && existsSync(override)) {
    return override;
  }
  const venvPython = path.join(pipelineRoot, 'venv', 'Scripts', 'python.exe');
  return existsSync(venvPython) ? venvPython : 'python';
}

function assertNever(value: never): never {
  throw new Error(`Unhandled variant: ${JSON.stringify(value)}`);
}

export function buildCommandArgv(command: CommandSpec, selectedValue: string | null): string[] {
  switch (command.kind) {
    case 'none':
    case 'legacy':
      return [...command.argv];
    case 'volume':
    case 'epub':
      return selectedValue ? [...command.argv, selectedValue] : [...command.argv];
    default:
      return assertNever(command);
  }
}

// Base argv (with any target) plus the user's chosen boolean flags.
export function buildFullArgv(
  command: CommandSpec,
  selectedValue: string | null,
  flags: readonly string[],
): string[] {
  return [...buildCommandArgv(command, selectedValue), ...flags];
}

export function runMtlCommand(
  command: CommandSpec,
  argv: string[],
  onUpdate: (state: RunState) => void,
): RunHandle {
  const fullArgv = [mtlScript, ...argv];
  const child = spawn(pythonCommand(), fullArgv, {
    cwd: pipelineRoot,
    env: process.env,
    shell: false,
  });

  let cancelled = false;

  const state: RunState = {
    command,
    argv,
    status: 'running',
    exitCode: null,
    lines: [`$ ${pythonCommand()} ${fullArgv.map((part) => (part.includes(' ') ? `"${part}"` : part)).join(' ')}`],
  };

  const emit = (): void => {
    if (!cancelled) {
      onUpdate({ ...state, lines: [...state.lines] });
    }
  };

  const append = (chunk: Buffer): void => {
    const text = chunk.toString('utf8').replace(/\r/g, '');
    for (const line of text.split('\n')) {
      if (line.trim()) {
        state.lines.push(line);
      }
    }
    state.lines = state.lines.slice(-200);
    emit();
  };

  child.stdout.on('data', append);
  child.stderr.on('data', append);
  child.on('error', (error) => {
    state.status = 'failed';
    state.lines.push(`Failed to launch: ${error.message}`);
    emit();
  });
  child.on('close', (code) => {
    state.status = code === 0 ? 'done' : 'failed';
    state.exitCode = code;
    state.lines.push(`Process exited with code ${code ?? 'unknown'}.`);
    emit();
  });

  return {
    cancel: () => {
      if (cancelled) {
        return;
      }
      cancelled = true;
      child.kill();
    },
    write: (data: string) => {
      if (cancelled || child.killed || !child.stdin.writable) {
        return;
      }
      child.stdin.write(data);
    },
  };
}
