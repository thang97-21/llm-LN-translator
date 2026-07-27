import { Box, Text } from 'ink';
import type { CapabilityRisk, PhaseStatus, PhaseStatusValue } from '../core/types.js';

export type Color = 'green' | 'yellow' | 'red' | 'gray' | 'cyan' | 'white' | 'magenta';

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

export function statusColor(status: PhaseStatusValue): Color {
  return PHASE_STATUS_COLOR[status];
}

export function riskColor(risk: CapabilityRisk): Color {
  return RISK_COLOR[risk];
}

export function formatTokens(count: number): string {
  if (count >= 1_000_000) {
    return `${(count / 1_000_000).toFixed(1)}M`;
  }
  if (count >= 1_000) {
    return `${(count / 1_000).toFixed(1)}K`;
  }
  return String(count);
}

export function Badge({ label, color = 'yellow' }: { label: string; color?: Color }) {
  return <Text color={color}>[{label}]</Text>;
}

export function Breadcrumb({ trail }: { trail: readonly string[] }) {
  return (
    <Text color="gray">
      {trail.map((part, index) =>
        index === trail.length - 1 ? (
          <Text key={part} color="cyan" bold>{part}</Text>
        ) : (
          <Text key={part}>{part} › </Text>
        ),
      )}
    </Text>
  );
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
