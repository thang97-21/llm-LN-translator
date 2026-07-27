export type PhaseStatusValue = 'pending' | 'running' | 'completed' | 'reviewed' | 'built' | 'failed' | 'unknown';

export type PhaseStatus = { key: string; label: string; status: PhaseStatusValue };

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

export type ChapterLog = {
  chapterId: string;
  inputTokens: number;
  outputTokens: number;
  success: boolean;
  error: string | null;
  passed: boolean | null;
  aiIsmCount: number | null;
};

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
export type CapabilityRisk = 'read' | 'write' | 'paid' | 'overwrite';
export type FieldKind =
  | 'volume'
  | 'epub'
  | 'chapter-list'
  | 'project-path'
  | 'text'
  | 'multiline'
  | 'integer'
  | 'boolean'
  | 'enum'
  | 'string-list'
  | 'json-object'
  | 'json-array';

export type FieldSpec = {
  key: string;
  label: string;
  kind: FieldKind;
  required?: boolean;
  description?: string;
  defaultValue?: string | boolean;
  choices?: readonly string[];
  cliFlag?: string;
  positional?: boolean;
  min?: number;
  projectScoped?: boolean;
};

export type CliRoute = { transport: 'cli'; command: string };
export type McpRoute = { transport: 'mcp'; tool: string };
export type LocalRoute = { transport: 'local'; view: 'dashboard' | 'volumes' | 'inputs' | 'diagnostics' };
export type CapabilityRoute = CliRoute | McpRoute | LocalRoute;

export type CapabilitySpec = {
  id: string;
  label: string;
  detail: string;
  group: string;
  route: CapabilityRoute;
  fields: readonly FieldSpec[];
  risk: CapabilityRisk;
  available?: boolean;
  unavailableReason?: string;
};

export type FormValues = Record<string, string | boolean>;
export type ValidationIssue = { field?: string; message: string };

export type ConsoleSeverity = 'info' | 'warning' | 'error' | 'success';
export type ConsoleSource = 'cli' | 'mcp' | 'system';
export type ConsoleEntry = {
  id: number;
  timestamp: number;
  source: ConsoleSource;
  severity: ConsoleSeverity;
  stage: string;
  text: string;
};
export type ConsoleMode = 'follow' | 'browse' | 'input';
export type ConsoleState = { entries: readonly ConsoleEntry[]; mode: ConsoleMode; offset: number; unseen: number };

export type RunStatus = 'running' | 'done' | 'failed' | 'cancelled';
export type RunState = {
  capability: CapabilitySpec;
  preview: string;
  status: RunStatus;
  exitCode: number | null;
  console: ConsoleState;
};

export type RunHandle = { cancel: () => void; write: (data: string) => void };
export type PreflightStatus = 'ready' | 'missing' | 'checking';
export type Preflight = {
  python: string;
  pythonStatus: PreflightStatus;
  importsStatus: PreflightStatus;
  mcpStatus: PreflightStatus;
  apiKeyPresent: boolean;
  repairCommand: string;
  detail: string;
};
