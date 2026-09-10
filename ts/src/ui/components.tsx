import { Box, Text } from 'ink';
import type { CapabilityRisk, ConsoleSeverity, PhaseStatus, PhaseStatusValue, RunStatus } from '../core/types.js';

export type Color = 'green' | 'yellow' | 'red' | 'gray' | 'cyan' | 'white' | 'magenta';

// Shared status vocabulary. Every screen was independently reinventing its own
// red/green/yellow ternary for what is, underneath, the same handful of
// meanings — this is the single place that decides what "good" looks like.
// `neutral` (gray) is for de-emphasized chrome; `plain` (white) is ordinary
// legible text — keeping them distinct matters, since collapsing "quiet" and
// "normal" would dim log lines that were never meant to recede.
export type Tone = 'good' | 'warn' | 'bad' | 'neutral' | 'plain' | 'active';

const TONE_COLOR: Record<Tone, Color> = {
  good: 'green',
  warn: 'yellow',
  bad: 'red',
  neutral: 'gray',
  plain: 'white',
  active: 'cyan',
};

export function toneColor(tone: Tone): Color {
  return TONE_COLOR[tone];
}

const SEVERITY_TONE: Record<ConsoleSeverity, Tone> = {
  error: 'bad',
  warning: 'warn',
  success: 'good',
  info: 'plain',
};

export function severityColor(severity: ConsoleSeverity): Color {
  return toneColor(SEVERITY_TONE[severity]);
}

const RUN_STATUS_TONE: Record<RunStatus, Tone> = {
  failed: 'bad',
  done: 'good',
  running: 'active',
  cancelled: 'active',
};

// `focused` is a UI-focus override, not a status of the run itself — it stays
// a parameter rather than a fifth RunStatus member the state machine doesn't have.
export function runStatusColor(status: RunStatus, focused: boolean): Color {
  if (status !== 'failed' && status !== 'done' && focused) return 'magenta';
  return toneColor(RUN_STATUS_TONE[status]);
}

export function boolStateColor(state: 'on' | 'off' | null): Color {
  return state === 'on' ? toneColor('good') : state === 'off' ? toneColor('bad') : toneColor('warn');
}

// Exhaustive by construction: adding a PhaseStatusValue without a color/glyph here
// is a compile error, not a silent fall-through.
const PHASE_STATUS_COLOR: Record<PhaseStatusValue, Color> = {
  completed: 'green',
  built: 'green',
  reviewed: 'green',
  running: 'cyan',
  pending: 'gray',
  failed: 'red',
  unknown: 'yellow',
};

const PHASE_GLYPH: Record<PhaseStatusValue, string> = {
  completed: '✓',
  built: '■',
  reviewed: '◆',
  running: '◐',
  pending: '·',
  failed: '✗',
  unknown: '?',
};

const RISK_COLOR: Record<CapabilityRisk, Color> = {
  read: 'green',
  write: 'yellow',
  paid: 'red',
  overwrite: 'red',
};

export function riskColor(risk: CapabilityRisk): Color {
  return RISK_COLOR[risk];
}

export function Badge({ label, color = 'yellow' }: { label: string; color?: Color }) {
  return <Text color={color}>[{label}]</Text>;
}

// Block-character progress bar, colored by completion.
export function ProgressBar({ value, total, width = 16 }: { value: number; total: number; width?: number }) {
  const ratio = total > 0 ? Math.min(1, Math.max(0, value / total)) : 0;
  const filled = Math.round(ratio * width);
  const pct = Math.round(ratio * 100);
  const color: Color = ratio >= 1 ? 'green' : ratio > 0 ? 'cyan' : 'gray';
  return (
    <Text>
      <Text color={color}>{'█'.repeat(filled)}</Text>
      <Text color="gray">{'░'.repeat(Math.max(0, width - filled))}</Text>
      <Text> {pct}% ({value}/{total})</Text>
    </Text>
  );
}

// Compact per-phase pipeline strip, e.g. "1✓ 1.5✓ 1.55· 2◐ 4·".
export function PhaseStrip({ phases }: { phases: readonly PhaseStatus[] }) {
  return (
    <Box>
      {phases.map((phase) => (
        <Text key={phase.key} color={PHASE_STATUS_COLOR[phase.status]}>
          {phase.label.replace(/^P/, '')}
          {PHASE_GLYPH[phase.status]}{' '}
        </Text>
      ))}
    </Box>
  );
}
