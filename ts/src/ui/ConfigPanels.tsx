import { useEffect, useMemo, useState } from 'react';
import { Box, Text } from 'ink';
import stringWidth from 'string-width';
import type { ConfigLine } from '../core/types.js';
import type { ConfigFieldState } from '../core/configFile.js';
import { activeProvider, buildConfigRenderLines, formatConfigValue } from '../core/configSchema.js';
import { PROOFREADING_PATHS, proofreadingState } from '../core/proofreading.js';
import { translationPricingStatus, usdPerMillion } from '../core/pricing.js';
import { boolStateColor, toneColor } from './components.js';
import { padCell } from './table.js';
import type { ConfigEditState, ConfigStatus } from './workspaceMachine.js';

export function RuntimeConfigPanel({ lines, offset, rows }: { lines: readonly ConfigLine[]; offset: number; rows: number }) {
  const viewport = Math.max(3, rows - 14);
  const start = Math.max(0, Math.min(Math.max(0, lines.length - viewport), offset));
  const visible = lines.slice(start, start + viewport);
  const sectionCount = lines.filter((line) => line.kind === 'group').length;
  const settingCount = lines.filter((line) => line.kind === 'value').length;
  return <Box flexDirection="column">
    <Text bold>Runtime configuration · config.yaml</Text>
    <Text color="gray">{sectionCount} sections · {settingCount} settings · PgUp/PgDn scroll · Home/End jump</Text>
    {visible.map((line, index) => {
      const key = `${start + index}`;
      if (line.kind === 'gap') return <Text key={key}> </Text>;
      const indent = '  '.repeat(line.depth);
      if (line.kind === 'group') return <Text key={key} bold color="cyan" wrap="truncate-end">{indent}{line.label}</Text>;
      const valueColor = boolStateColor(line.boolState);
      return <Text key={key} wrap="truncate-end">  {indent}{line.label}: <Text color={valueColor}>{line.value}</Text></Text>;
    })}
  </Box>;
}

// This is a display clock, not an operator setting. It keeps date-bounded
// promotions honest and refreshes fast enough to catch their boundary.
export function TranslationPricingPanel({ fields }: { fields: readonly ConfigFieldState[] }) {
  const [clock, setClock] = useState(() => new Date());
  useEffect(() => {
    const interval = setInterval(() => setClock(new Date()), 30_000);
    return () => clearInterval(interval);
  }, []);
  const status = translationPricingStatus(fields, clock);
  const color = status.rates ? toneColor(status.condition ? 'warn' : 'good') : toneColor('bad');
  return <Box flexDirection="column" marginTop={1} borderStyle="round" borderColor={color} paddingX={1}>
    <Text bold>{status.providerLabel} tariff · <Text color={color}>{status.rateLabel}</Text></Text>
    <Text color="gray">  Translation route: {status.displayName} · checked at {clock.toLocaleString()}</Text>
    {status.rates
      ? <Text>  / 1M tokens: input {usdPerMillion(status.rates.input)} · {status.cacheReadLabel} {usdPerMillion(status.rates.cacheRead)} · {status.cacheWriteLabel} {usdPerMillion(status.rates.cacheWrite)} · output {usdPerMillion(status.rates.output)}</Text>
      : <Text color="yellow">  No trustworthy input/cache/output rate is published for this API model.</Text>}
    {status.condition ? <Text color="yellow">  {status.condition}</Text> : null}
    {status.note ? <Text color="gray">  {status.note}</Text> : null}
    {status.source ? <Text color="gray">  Source: {status.sourceLabel} · {status.source}</Text> : null}
  </Box>;
}

// Unlike DeveloperPanel below, this one IS persisted: `p` writes
// translation.anthropic.advisor.enabled, because that is what the Python
// route reads. It lives on the Dashboard because the Configuration screen
// hides every translation.anthropic.* field unless Anthropic is the active
// provider — which made the switch unreachable on any other route.
export function ProofreadingPanel({ fields, notice }: { fields: readonly ConfigFieldState[]; notice: { message: string; ok: boolean } | null }) {
  const state = proofreadingState(fields);
  const provider = activeProvider(fields);
  const label = (path: string, raw: string) => { const field = fields.find((item) => item.path === path); return field ? formatConfigValue(field, raw) : raw; };
  const status = state.enabled ? (state.blocker ? 'ON — INVALID' : 'ON') : state.locked ? 'LOCKED' : 'off';
  const tone = state.enabled ? (state.blocker ? 'bad' : 'good') : state.locked ? 'warn' : 'neutral';
  return <Box flexDirection="column" marginTop={1} borderStyle="round" borderColor={toneColor(tone)} paddingX={1}>
    <Text bold>Proofreading Mode · <Text color={toneColor(tone)}>{status}</Text></Text>
    <Text wrap="truncate-end">  Executor: {label(PROOFREADING_PATHS.executor, state.executor) || '—'} · Advisor: {label(PROOFREADING_PATHS.advisor, state.advisor) || '—'}</Text>
    {state.blocker ? <Text color="yellow" wrap="truncate-end">  {state.blocker}</Text> : null}
    {provider !== 'anthropic' ? <Text color="gray" wrap="truncate-end">  Anthropic route only. The current provider is {provider || 'unset'}; the setting is kept for when you switch.</Text> : null}
    {notice ? <Text color={toneColor(notice.ok ? 'good' : 'bad')} wrap="truncate-end">  {notice.message}</Text> : null}
    <Text color="gray">  Press p to toggle. Saved to config.yaml; Anthropic's official pairings only.</Text>
  </Box>;
}

// Session-only toggles — deliberately NOT config.yaml fields. This is "what
// should the NEXT launch form default to," not "what should the pipeline
// permanently do" (that's what the Configuration screen is for). Living here
// keeps a fast, throwaway developer switch from acquiring a saved-to-disk
// footprint it was never meant to have.
export function DeveloperPanel({ dryRunDefault }: { dryRunDefault: boolean }) {
  return <Box flexDirection="column" marginTop={1} borderStyle="round" borderColor="yellow" paddingX={1}>
    <Text bold color="yellow">Developer</Text>
    <Text>  Dry-run default: <Text color={toneColor(dryRunDefault ? 'good' : 'neutral')}>{dryRunDefault ? 'ON' : 'off'}</Text></Text>
    <Text color="gray">  Press d to toggle. When ON, opening any form with a Dry Run field pre-checks it —</Text>
    <Text color="gray">  Translate writes the API payload, Build assembles the EPUB structure, both to</Text>
    <Text color="gray">  work/&lt;vol&gt;/DRY_RUN/, both with no real send/package and no manifest changes.</Text>
  </Box>;
}

export function ConfigurationPanel({ fields, cursor, rows, edit, status, query }: { fields: readonly ConfigFieldState[]; cursor: number; rows: number; edit: ConfigEditState; status: ConfigStatus; query: string }) {
  const renderLines = useMemo(() => buildConfigRenderLines(fields), [fields]);
  // Capped so one unusually long label can't drag the value column off-screen
  // for every other row; wrap="truncate-end" below is the safety net if a
  // label still overruns.
  const labelWidth = useMemo(() => Math.min(32, fields.reduce((max, field) => Math.max(max, stringWidth(field.label)), 0)), [fields]);
  const provider = activeProvider(fields);
  const viewport = Math.max(6, rows - 10);
  const targetLine = renderLines.findIndex((line) => line.kind === 'field' && line.fieldIndex === cursor);
  const start = Math.max(0, Math.min(Math.max(0, renderLines.length - viewport), targetLine < 0 ? 0 : targetLine - Math.floor(viewport / 2)));
  const visible = renderLines.slice(start, start + viewport);
  if (!fields.length) return <Box flexDirection="column"><Text bold>Configuration · config.yaml</Text><Text color="yellow">Nothing matches.</Text></Box>;
  return <Box flexDirection="column">
    <Text bold>Configuration · config.yaml{query ? <Text color="cyan"> · search: {query}</Text> : null}</Text>
    <Text color="gray">{fields.length} settings · Provider: {provider || '—'} · Enter edits/toggles · Space toggles · Esc cancels edit</Text>
    {visible.map((line, index) => {
      const key = `${start + index}`;
      if (line.kind === 'gap') return <Text key={key}> </Text>;
      if (line.kind === 'menu') return <Text key={key} bold color="cyan" wrap="truncate-end">▸ {line.label}</Text>;
      if (line.kind === 'section') return <Text key={key} bold color="cyan" wrap="truncate-end">  {line.label}</Text>;
      const field = fields[line.fieldIndex]!;
      const isSelected = line.fieldIndex === cursor;
      const isEditing = edit?.fieldPath === field.path;
      const displayValue = isEditing ? edit!.buffer : formatConfigValue(field, field.rawValue);
      const valueColor = isEditing ? toneColor('active') : field.kind === 'boolean' ? toneColor(field.rawValue === 'true' ? 'good' : 'bad') : toneColor('warn');
      return <Box key={key} flexDirection="column">
        <Text inverse={isSelected && !isEditing} color={isSelected ? toneColor('active') : toneColor('plain')} wrap="truncate-end">{'    '}{padCell(field.label, labelWidth)} <Text color={valueColor}>{displayValue}</Text>{isEditing ? <Text color="gray">▏</Text> : null}{' '}</Text>
        {isSelected ? <Text color="gray" wrap="truncate-end">      {field.description}</Text> : null}
        {isEditing && edit!.error ? <Text color={toneColor('bad')} wrap="truncate-end">      ! {edit!.error}</Text> : null}
        {status && status.fieldPath === field.path ? <Text color={toneColor(status.ok ? 'good' : 'bad')} wrap="truncate-end">      {status.message}</Text> : null}
      </Box>;
    })}
  </Box>;
}
