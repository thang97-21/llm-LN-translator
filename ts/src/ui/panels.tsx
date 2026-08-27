import { Box, Text } from 'ink';
import { memo, useMemo } from 'react';
import type { CapabilitySpec, Preflight, VolumeSummary } from '../core/types.js';
import { fuzzyMatch, loadVolumeDetail } from '../core/mtls.js';
import { Badge, PhaseStrip, ProgressBar, riskColor } from './components.js';
import { layoutForColumns } from './layout.js';
import { NAV } from './workspaceMachine.js';

// Narrowed to the two primitive fields actually read (activeVolume, navIndex)
// and wrapped in memo: `workspace` itself gets a new object identity on
// nearly every dispatch (search keystrokes, console log chunks, form edits),
// so passing it whole would re-render Header/Navigation on all of that too.
export const Header = memo(function Header({ activeVolume, preflight, columns, compact }: { activeVolume: string | null; preflight: Preflight; columns: number; compact?: boolean }) {
  const active = activeVolume ?? 'none';
  const status = <Text wrap="truncate-end">volume: <Text color={activeVolume ? 'green' : 'yellow'}>{active}</Text> · python <Badge label={preflight.pythonStatus} color={preflight.pythonStatus === 'ready' ? 'green' : 'red'} /> · MCP <Badge label={preflight.mcpStatus} color={preflight.mcpStatus === 'ready' ? 'green' : preflight.mcpStatus === 'checking' ? 'yellow' : 'red'} /> · API key <Badge label={preflight.apiKeyPresent ? 'present' : 'missing'} color={preflight.apiKeyPresent ? 'green' : 'yellow'} /></Text>;
  // The Console screen only ever shows the console — 3 chrome rows saved
  // here (title row + both borders) is 3 more lines of scrollback visible
  // without touching ConsolePanel itself. Same status line either way.
  if (compact) return status;
  return <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
    <Box justifyContent="space-between"><Text bold color="cyan">LLM Translator · Operator Console</Text><Text color="gray">{layoutForColumns(columns)}</Text></Box>
    {status}
  </Box>;
});

export const Navigation = memo(function Navigation({ navIndex }: { navIndex: number }) { return <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} width={22}>{NAV.map((item, index) => <Text key={item.id} inverse={navIndex === index} color={navIndex === index ? 'cyan' : 'white'}>{' '}{item.label}{' '}</Text>)}</Box>; });

export function CapabilityList({ items, index, query }: { items: readonly CapabilitySpec[]; index: number; query: string }) {
  const visible = items.filter((item) => fuzzyMatch(query, `${item.group} ${item.label} ${item.detail}`));
  return <Box flexDirection="column"><Text bold>{visible.length === 1 ? '1 capability' : `${visible.length} capabilities`}{query ? <Text color="cyan"> · search: {query}</Text> : null}</Text>{visible.length ? visible.map((item, position) => <Box key={item.id} flexDirection="column"><Text inverse={position === index} color={position === index ? 'cyan' : item.available === false ? 'gray' : 'white'}>{' '}{item.label} <Text color={riskColor(item.risk)}>[{item.risk}]</Text>{' '}</Text>{position === index && <Text color="gray" wrap="truncate-end">  {item.group}: {item.detail}{item.unavailableReason ? ` — ${item.unavailableReason}` : ''}</Text>}</Box>) : <Text color="yellow">Nothing matches.</Text>}</Box>;
}

export function Inspector({ volume }: { volume: VolumeSummary | null }) {
  // Keyed on id+updatedAt, not the volume object itself: refresh() rebuilds
  // the whole volumes array (new object identity) on every directory-watcher
  // poll tick regardless of whether this volume actually changed, so identity
  // would defeat the memo. updatedAt is the manifest's mtime, so EN chapter
  // counts here can lag a running translation until the manifest itself
  // updates — an accepted tradeoff for not re-reading every chapter file off
  // disk on every console line and keystroke while this panel is visible.
  const detail = useMemo(() => (volume ? loadVolumeDetail(volume.id) : null), [volume?.id, volume?.updatedAt]);
  if (!volume || !detail) return <Box borderStyle="round" borderColor="gray" paddingX={1}><Text color="gray">Select a volume to inspect it.</Text></Box>;
  return <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1}><Text bold wrap="truncate-end">{volume.title}</Text><Text color="gray">{volume.author} · {volume.series}</Text><ProgressBar value={volume.translatedCount} total={volume.chapterCount} /><PhaseStrip phases={volume.phases} /><Text color="gray">JP {detail.jpChapters} · EN {detail.enChapters} · QC {detail.qcReports}</Text>{volume.manifestError ? <Text color="yellow" wrap="truncate-end">! {volume.manifestError}</Text> : null}{detail.lastError ? <Text color="red" wrap="truncate-end">{detail.lastError}</Text> : null}</Box>;
}
