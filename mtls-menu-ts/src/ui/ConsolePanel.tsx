import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import type { RunState } from '../core/types.js';
import { visibleConsoleEntries } from '../core/console.js';

export function ConsolePanel({ run, rows, focused, cancelConfirm }: { run: RunState | null; rows: number; focused: boolean; cancelConfirm: boolean }) {
  if (!run) return <Box borderStyle="round" borderColor="gray" paddingX={1}><Text color="gray">No action yet. Runs stay here after completion instead of evaporating into a 200-line lie.</Text></Box>;
  const entries = visibleConsoleEntries(run.console, Math.max(4, rows - 9));
  return <Box flexDirection="column" borderStyle="round" borderColor={run.status === 'failed' ? 'red' : run.status === 'done' ? 'green' : focused ? 'magenta' : 'cyan'} paddingX={1}><Text bold>{run.status === 'running' ? <Text color="cyan"><Spinner type="dots" /> </Text> : null}{run.capability.label} · {run.status}</Text><Text color="gray" wrap="truncate-end">{run.preview}</Text><Text color={focused ? 'magenta' : 'gray'}>focus: {focused ? run.console.mode : 'menu'} · retained {run.console.entries.length}/5000 · {run.console.unseen ? `${run.console.unseen} unseen` : 'tail current'}</Text>{cancelConfirm ? <Text color="red">Cancel the running action? Enter confirms; Esc keeps it alive.</Text> : null}{entries.map((entry) => <Text key={entry.id} color={entry.severity === 'error' ? 'red' : entry.severity === 'warning' ? 'yellow' : entry.severity === 'success' ? 'green' : 'white'} wrap="truncate-end">[{entry.source}/{entry.stage}] {entry.text}</Text>)}<Text color="gray">wheel scrolls anytime · Tab focus · ↑↓ scroll · PgUp/PgDn page · Home/End · f follow · i raw stdin · Esc browse</Text></Box>;
}
