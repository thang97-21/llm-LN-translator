// Normalized pipeline/phase status. Raw manifest strings are collapsed to this
// closed set at the parse boundary (see normalizePhaseStatus in mtls.ts) so the
// view layer can switch on it exhaustively instead of doing stringly-typed scans.
export type PhaseStatusValue =
  | 'pending'
  | 'running'
  | 'completed'
  | 'reviewed'
  | 'built'
  | 'failed'
  | 'unknown';

export type PhaseStatus = {
  key: string;
  label: string;
  status: PhaseStatusValue;
};

export type VolumeSummary = {
  id: string;
  title: string;
  author: string;
  series: string;
  hasEnTitle: boolean;
  updatedAt: number;
  chapterCount: number;
  translatedCount: number;
  phases: PhaseStatus[];
};

// Per-chapter row parsed from translation_log.json.
export type ChapterLog = {
  chapterId: string;
  inputTokens: number;
  outputTokens: number;
  success: boolean;
  error: string | null;
  passed: boolean | null;
  aiIsmCount: number | null;
};

// Lazily loaded, richer per-volume view mined from on-disk artifacts.
export type VolumeDetail = {
  id: string;
  loaded: boolean;
  jpChapters: number;
  enChapters: number;
  enWords: number;
  qcReports: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  successCount: number;
  failCount: number;
  aiIsmTotal: number;
  lastError: string | null;
  logChapters: ChapterLog[];
};

export type SortMode = 'recent' | 'series' | 'progress';

export type CommandKind = 'volume' | 'epub' | 'none' | 'legacy';
export type CommandRisk = 'low' | 'medium' | 'high';

// A boolean CLI flag a command can be launched with (value-flags are out of scope).
export type CommandFlag = {
  flag: string;
  label: string;
  default: boolean;
};

type CommandBase = {
  id: string;
  label: string;
  detail: string;
  argv: readonly string[];
  risk: CommandRisk;
  flags?: readonly CommandFlag[];
};

// Discriminated on `kind`. Each variant is its own type so dispatch sites can be
// made exhaustive with an assertNever fallback, and so kinds can grow distinct
// payloads later without touching the others.
export type VolumeCommand = CommandBase & { kind: 'volume' };
export type EpubCommand = CommandBase & { kind: 'epub' };
export type NoneCommand = CommandBase & { kind: 'none' };
export type LegacyCommand = CommandBase & { kind: 'legacy' };

export type CommandSpec = VolumeCommand | EpubCommand | NoneCommand | LegacyCommand;

export type RunStatus = 'running' | 'done' | 'failed';

export type RunState = {
  command: CommandSpec;
  argv: string[];
  status: RunStatus;
  exitCode: number | null;
  lines: string[];
};

// Handle returned by runMtlCommand so the caller can tear the child process down
// when the user leaves the run screen or exits the app.
export type RunHandle = {
  cancel: () => void;
  write: (data: string) => void;
};
