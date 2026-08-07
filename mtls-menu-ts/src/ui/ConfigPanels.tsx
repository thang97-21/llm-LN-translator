import { useMemo } from 'react';
import { Box, Text } from 'ink';
import type { ConfigLine } from '../core/types.js';
import type { ConfigFieldState } from '../core/configFile.js';
import { activeProvider, buildConfigRenderLines, formatConfigValue } from '../core/configSchema.js';
import type { ConfigEditState, ConfigStatus } from './workspaceMachine.js';

export function RuntimeConfigPanel({ lines, offset, rows }: { lines: readonly ConfigLine[]; offset: number; rows: number }) {
  const viewport = Math.max(6, rows - 8);
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
      const valueColor = line.boolState === 'on' ? 'green' : line.boolState === 'off' ? 'red' : 'yellow';
      return <Text key={key} wrap="truncate-end">  {indent}{line.label}: <Text color={valueColor}>{line.value}</Text></Text>;
    })}
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
    <Text>  Dry-run default: <Text color={dryRunDefault ? 'green' : 'gray'}>{dryRunDefault ? 'ON' : 'off'}</Text></Text>
    <Text color="gray">  Press d to toggle. When ON, opening any form with a Dry Run field pre-checks it —</Text>
    <Text color="gray">  Translate writes the API payload, Build assembles the EPUB structure, both to</Text>
    <Text color="gray">  work/&lt;vol&gt;/DRY_RUN/, both with no real send/package and no manifest changes.</Text>
  </Box>;
}

export function ConfigurationPanel({ fields, cursor, rows, edit, status, query }: { fields: readonly ConfigFieldState[]; cursor: number; rows: number; edit: ConfigEditState; status: ConfigStatus; query: string }) {
  const renderLines = useMemo(() => buildConfigRenderLines(fields), [fields]);
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
      const valueColor = isEditing ? 'cyan' : field.kind === 'boolean' ? (field.rawValue === 'true' ? 'green' : 'red') : 'yellow';
      return <Box key={key} flexDirection="column">
        <Text inverse={isSelected && !isEditing} color={isSelected ? 'cyan' : 'white'} wrap="truncate-end">{'    '}{field.label}: <Text color={valueColor}>{displayValue}</Text>{isEditing ? <Text color="gray">▏</Text> : null}{' '}</Text>
        {isSelected ? <Text color="gray" wrap="truncate-end">      {field.description}</Text> : null}
        {isEditing && edit!.error ? <Text color="red" wrap="truncate-end">      ! {edit!.error}</Text> : null}
        {status && status.fieldPath === field.path ? <Text color={status.ok ? 'green' : 'red'} wrap="truncate-end">      {status.message}</Text> : null}
      </Box>;
    })}
  </Box>;
}
