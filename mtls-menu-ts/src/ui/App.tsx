import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import type { Key } from 'ink';
import Spinner from 'ink-spinner';
import {
  buildFullArgv,
  commands,
  filterEpubs,
  filterVolumes,
  fuzzyMatch,
  loadEpubs,
  loadVolumeDetail,
  loadVolumes,
  runMtlCommand,
  sortVolumes,
} from '../core/mtls.js';
import { hasMcpRoute, runMtlCommandMcp } from '../core/mcpClient.js';
import type {
  CommandSpec,
  RunHandle,
  RunState,
  SortMode,
  VolumeDetail,
  VolumeSummary,
} from '../core/types.js';
import { useTerminalSize } from './useTerminalSize.js';
import {
  Badge,
  Breadcrumb,
  ProgressBar,
  PhaseStrip,
  formatTokens,
  riskColor,
  type Color,
} from './components.js';

type Screen = 'home' | 'commands' | 'volumes' | 'epubs' | 'launch' | 'run';

type AppProps = {
  onRequestLegacy: () => void;
  // 'subprocess' (default): spawn `python scripts/mtl.py <argv>`, parse stdout.
  // 'mcp': connect to `python -m src.mcp.server` over stdio, call tools directly.
  // Same Python code either way — see core/mcpClient.ts's module doc.
  transport?: 'subprocess' | 'mcp';
};

type LaunchState = {
  command: CommandSpec;
  selectedValue: string | null;
  flags: boolean[];
  cursor: number;
  confirm: boolean;
};

const homeItems = [
  { id: 'commands', label: 'Command Palette', detail: 'Run canonical MTLS commands through the richer shell.' },
  { id: 'volumes', label: 'Volume Workbench', detail: 'Inspect recent WORK volumes and choose the active target.' },
  { id: 'refresh', label: 'Refresh Dashboard', detail: 'Reload manifests and EPUB candidates from disk.' },
  { id: 'legacy', label: 'Legacy Python TUI', detail: 'Leave this shell and launch the existing menu.' },
  { id: 'exit', label: 'Exit', detail: 'Close the TypeScript menu.' },
] as const;

const SORT_ORDER: readonly SortMode[] = ['recent', 'series', 'progress'];
const SORT_LABEL: Record<SortMode, string> = {
  recent: 'recent',
  series: 'series',
  progress: 'progress ↑',
};

function clamp(index: number, length: number): number {
  if (length <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(index, length - 1));
}

function windowStart(selected: number, length: number, size: number): number {
  if (length <= size) {
    return 0;
  }
  const half = Math.floor(size / 2);
  return Math.max(0, Math.min(selected - half, length - size));
}

function isPrintable(input: string, key: Key): boolean {
  return input.length === 1 && input >= ' ' && !key.ctrl && !key.meta && !key.return && !key.tab;
}

function terminalInput(input: string, key: Key): string | null {
  if (key.return) {
    return '\r';
  }
  if (key.backspace || key.delete) {
    return '\x7f';
  }
  if (key.upArrow) {
    return '\x1b[A';
  }
  if (key.downArrow) {
    return '\x1b[B';
  }
  if (key.rightArrow) {
    return '\x1b[C';
  }
  if (key.leftArrow) {
    return '\x1b[D';
  }
  if (input && !key.ctrl && !key.meta) {
    return input;
  }
  return null;
}

function basename(filePath: string): string {
  const parts = filePath.split(/[\\/]/);
  return parts[parts.length - 1] ?? filePath;
}

function formatElapsed(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`;
}

// Heuristic coloring for the run log — stdout/stderr are merged, so we tag by content.
function lineColor(line: string): Color {
  const value = line.toLowerCase();
  if (line.startsWith('$ ')) {
    return 'cyan';
  }
  if (/(error|failed|traceback|exception)/.test(value)) {
    return 'red';
  }
  if (/(warn|warning)/.test(value)) {
    return 'yellow';
  }
  if (/(done|success|completed|✓)/.test(value)) {
    return 'green';
  }
  return 'white';
}

// Generic windowed list with scroll indicators. Keeps full item typing at call sites.
function ScrollList<T>({
  items,
  selected,
  render,
  windowSize,
}: {
  items: readonly T[];
  selected: number;
  render: (item: T, active: boolean, index: number) => ReactNode;
  windowSize: number;
}) {
  const size = Math.max(1, windowSize);
  const start = windowStart(selected, items.length, size);
  const end = Math.min(items.length, start + size);
  const visible = items.slice(start, end);
  if (items.length === 0) {
    return <Text color="yellow">No items.</Text>;
  }
  return (
    <>
      {start > 0 && <Text color="gray">  ↑ {start} more</Text>}
      {visible.map((item, offset) => render(item, selected === start + offset, start + offset))}
      {end < items.length && <Text color="gray">  ↓ {items.length - end} more</Text>}
    </>
  );
}

function Header({ selectedVolume, trail }: { selectedVolume: VolumeSummary | null; trail: readonly string[] }) {
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      <Box justifyContent="space-between">
        <Text bold color="cyan">DeepSeek_MTLS · TS7 Shell</Text>
        <Breadcrumb trail={trail} />
      </Box>
      <Text>
        Active volume:{' '}
        <Text color={selectedVolume ? 'green' : 'yellow'} wrap="truncate-end">
          {selectedVolume ? `${selectedVolume.title} (${selectedVolume.id})` : 'none selected'}
        </Text>
      </Text>
    </Box>
  );
}

function Dashboard({ volumes }: { volumes: VolumeSummary[] }) {
  const latest = volumes[0] ?? null;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1}>
      <Text bold>Dashboard <Text color="gray">· {volumes.length} WORK volumes</Text></Text>
      {latest ? (
        <Box flexDirection="column">
          <Text wrap="truncate-end">
            Latest: <Text color="green">{latest.title}</Text> <Text color="gray">({latest.id})</Text>
          </Text>
          <Box>
            <Text>Chapters </Text>
            <ProgressBar value={latest.translatedCount} total={latest.chapterCount} />
          </Box>
          <Box>
            <Text>Phases  </Text>
            <PhaseStrip phases={latest.phases} />
          </Box>
        </Box>
      ) : (
        <Text color="gray">No manifests found.</Text>
      )}
    </Box>
  );
}

function FilterLine({
  filtering,
  filter,
  count,
  sort,
}: {
  filtering: boolean;
  filter: string;
  count: number;
  sort?: SortMode;
}) {
  if (!filtering && !filter && !sort) {
    return null;
  }
  return (
    <Box>
      <Text color={filtering ? 'cyan' : 'gray'}>
        {filtering || filter ? `filter: ${filter}${filtering ? '▌' : ''}  ` : ''}
      </Text>
      <Text color="gray">
        {filter ? `${count} match${count === 1 ? '' : 'es'}  ` : ''}
        {sort ? `sort: ${SORT_LABEL[sort]}` : ''}
      </Text>
    </Box>
  );
}

function DetailPanel({ volume, detail }: { volume: VolumeSummary | null; detail: VolumeDetail | null }) {
  if (!volume) {
    return (
      <Box borderStyle="round" borderColor="gray" paddingX={1} marginTop={1} marginLeft={1}>
        <Text color="gray">No volume selected.</Text>
      </Box>
    );
  }
  const d = detail && detail.id === volume.id ? detail : null;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1} marginLeft={1}>
      <Text bold wrap="truncate-end">
        {volume.title} {!volume.hasEnTitle && <Badge label="JP" color="magenta" />}
      </Text>
      <Text color="gray" wrap="truncate-end">series: {volume.series}</Text>
      <Text color="gray" wrap="truncate-end">author: {volume.author}</Text>
      <Box marginTop={1}>
        <ProgressBar value={volume.translatedCount} total={volume.chapterCount} />
      </Box>
      <Box>
        <PhaseStrip phases={volume.phases} />
      </Box>
      <Box marginTop={1} flexDirection="column">
        {d && d.loaded ? (
          <>
            <Text color="gray">
              JP {d.jpChapters}ch · EN {d.enChapters}ch · {formatTokens(d.enWords)} words · QC {d.qcReports}
            </Text>
            <Text color="gray">
              tokens: {formatTokens(d.totalInputTokens)} in / {formatTokens(d.totalOutputTokens)} out
            </Text>
            <Text color="gray">
              logged {d.successCount + d.failCount}ch ·{' '}
              <Text color={d.failCount > 0 ? 'red' : 'green'}>{d.successCount} ok</Text>
              {d.failCount > 0 ? <Text color="red"> · {d.failCount} fail</Text> : null}
              {' · '}<Text color={d.aiIsmTotal > 0 ? 'yellow' : 'gray'}>{d.aiIsmTotal} AI-isms</Text>
            </Text>
            {d.lastError && <Text color="red" wrap="truncate-end">err: {d.lastError}</Text>}
          </>
        ) : (
          <Text color="gray">No translation artifacts on disk yet.</Text>
        )}
      </Box>
    </Box>
  );
}

function LaunchView({ launch, python }: { launch: LaunchState; python: string }) {
  const cmd = launch.command;
  const flags = cmd.flags ?? [];
  const enabled = flags.filter((_, i) => launch.flags[i] === true).map((f) => f.flag);
  const preview = buildFullArgv(cmd, launch.selectedValue, enabled);
  const flagCount = flags.length;
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={cmd.risk === 'high' ? 'red' : 'cyan'} paddingX={1} marginTop={1}>
      <Text bold>
        Launch: {cmd.label} <Text color={riskColor(cmd.risk)}>[{cmd.risk}]</Text>
      </Text>
      {launch.selectedValue && <Text color="gray" wrap="truncate-end">target: {launch.selectedValue}</Text>}
      <Box marginTop={1}>
        <Text color="gray" wrap="truncate-end">
          $ {python} mtl.py {preview.join(' ')}
        </Text>
      </Box>
      {flagCount > 0 && (
        <Box flexDirection="column" marginTop={1}>
          {flags.map((flag, index) => {
            const active = launch.cursor === index;
            const on = launch.flags[index] === true;
            return (
              <Text key={flag.flag} inverse={active} color={active ? 'cyan' : 'white'}>
                {' '}{on ? '[x]' : '[ ]'} {flag.label} <Text color="gray">({flag.flag})</Text>{' '}
              </Text>
            );
          })}
        </Box>
      )}
      <Box marginTop={1}>
        <Text inverse={launch.cursor >= flagCount} color={cmd.risk === 'high' ? 'red' : 'green'}>
          {' '}▶ {launch.confirm ? 'Press Enter again to CONFIRM' : cmd.risk === 'high' ? 'Run (high risk)' : 'Run'}{' '}
        </Text>
      </Box>
    </Box>
  );
}

function RunView({
  runState,
  elapsedMs,
  logRows,
  terminalFocused,
}: {
  runState: RunState;
  elapsedMs: number;
  logRows: number;
  terminalFocused: boolean;
}) {
  const running = runState.status === 'running';
  const border: Color = runState.status === 'failed' ? 'red' : runState.status === 'done' ? 'green' : terminalFocused ? 'magenta' : 'cyan';
  return (
    <Box flexDirection="column" borderStyle="round" borderColor={border} paddingX={1} marginTop={1}>
      <Box justifyContent="space-between">
        <Text bold>
          {running ? <Text color="cyan"><Spinner type="dots" /> </Text> : null}
          {runState.command.label}{' '}
          <Text color={border}>{runState.status}</Text>
          {runState.exitCode !== null ? <Text color="gray"> (exit {runState.exitCode})</Text> : null}
        </Text>
        <Text color="gray">⏱ {formatElapsed(elapsedMs)}</Text>
      </Box>
      <Text color={terminalFocused ? 'magenta' : 'gray'}>
        focus: {terminalFocused ? 'terminal input' : 'menu'} · Tab toggles focus
      </Text>
      <Text color="gray" wrap="truncate-end">args: {runState.argv.join(' ') || '(none)'}</Text>
      <Box flexDirection="column" marginTop={1}>
        {runState.lines.slice(-Math.max(4, logRows)).map((line, index) => (
          <Text key={`${index}-${line.slice(0, 16)}`} color={lineColor(line)} wrap="truncate-end">
            {line}
          </Text>
        ))}
      </Box>
    </Box>
  );
}

function Footer({ hint }: { hint: string }) {
  return (
    <Box marginTop={1}>
      <Text color="gray">{hint}</Text>
    </Box>
  );
}

function trailFor(screen: Screen, launch: LaunchState | null): string[] {
  switch (screen) {
    case 'home':
      return ['Home'];
    case 'commands':
      return ['Home', 'Commands'];
    case 'volumes':
      return ['Home', 'Volumes'];
    case 'epubs':
      return ['Home', 'EPUB Inputs'];
    case 'launch':
      return ['Home', 'Launch', launch?.command.label ?? ''];
    case 'run':
      return ['Home', 'Run'];
    default:
      return ['Home'];
  }
}

function hintFor(screen: Screen, filtering: boolean): string {
  if (filtering) {
    return 'Type to filter · ↑↓ move · Enter select · Esc clear';
  }
  switch (screen) {
    case 'volumes':
      return '↑↓ move · / filter · s sort · g/G ends · Enter select · Esc back';
    case 'commands':
    case 'epubs':
      return '↑↓ move · / filter · Enter select · Esc back';
    case 'launch':
      return '↑↓ move · Space toggle · Enter run · Esc back';
    case 'run':
      return 'Tab terminal focus · Esc back/cancel · Ctrl+C exit';
    default:
      return '↑↓ move · Enter select · Esc back/exit';
  }
}

export function App({ onRequestLegacy, transport = 'subprocess' }: AppProps) {
  const { exit } = useApp();
  const { rows, columns } = useTerminalSize();
  const [screen, setScreen] = useState<Screen>('home');
  const [volumes, setVolumes] = useState<VolumeSummary[]>(() => loadVolumes());
  const [epubs, setEpubs] = useState<string[]>(() => loadEpubs());
  const [selectedVolumeId, setSelectedVolumeId] = useState<string | null>(volumes[0]?.id ?? null);
  const [homeIndex, setHomeIndex] = useState(0);
  const [commandIndex, setCommandIndex] = useState(0);
  const [volumeIndex, setVolumeIndex] = useState(0);
  const [epubIndex, setEpubIndex] = useState(0);
  const [filter, setFilter] = useState('');
  const [filtering, setFiltering] = useState(false);
  const [sortMode, setSortMode] = useState<SortMode>('recent');
  const [pendingCommand, setPendingCommand] = useState<CommandSpec | null>(null);
  const [launch, setLaunch] = useState<LaunchState | null>(null);
  const [runState, setRunState] = useState<RunState | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [detail, setDetail] = useState<VolumeDetail | null>(null);
  const [terminalFocused, setTerminalFocused] = useState(false);
  const runHandleRef = useRef<RunHandle | null>(null);
  const runStartRef = useRef<number>(0);
  const detailCache = useRef<Map<string, VolumeDetail>>(new Map());

  // Windowed list sizes derived from the viewport. Volume rows are two lines each.
  const listRows = Math.max(4, rows - 12);
  const volumeRows = Math.max(3, Math.floor((rows - 12) / 2));
  const logRows = Math.max(6, rows - 10);

  const visibleVolumes = useMemo(
    () => sortVolumes(filterVolumes(volumes, filter), sortMode),
    [volumes, filter, sortMode],
  );
  const visibleCommands = useMemo(
    () => (filter ? commands.filter((command) => fuzzyMatch(filter, `${command.label} ${command.id}`)) : [...commands]),
    [filter],
  );
  const visibleEpubs = useMemo(() => filterEpubs(epubs, filter), [epubs, filter]);

  const highlightedVolume = visibleVolumes[clamp(volumeIndex, visibleVolumes.length)] ?? null;
  const highlightedId = highlightedVolume?.id ?? null;

  const selectedVolume = useMemo(
    () => volumes.find((volume) => volume.id === selectedVolumeId) ?? volumes[0] ?? null,
    [selectedVolumeId, volumes],
  );

  const cancelRun = (): void => {
    runHandleRef.current?.cancel();
    runHandleRef.current = null;
  };

  const refresh = (): void => {
    const nextVolumes = loadVolumes();
    const nextEpubs = loadEpubs();
    detailCache.current.clear();
    setVolumes(nextVolumes);
    setEpubs(nextEpubs);
    setSelectedVolumeId((current) =>
      current && nextVolumes.some((volume) => volume.id === current) ? current : nextVolumes[0]?.id ?? null,
    );
  };

  const enterScreen = (target: Screen): void => {
    setFilter('');
    setFiltering(false);
    setScreen(target);
  };

  const startCommand = (command: CommandSpec, selectedValue: string | null, enabledFlags: readonly string[]): void => {
    if (command.kind === 'legacy') {
      onRequestLegacy();
      return;
    }
    cancelRun();
    const argv = buildFullArgv(command, selectedValue, enabledFlags);
    runStartRef.current = Date.now();
    setElapsedMs(0);
    setRunState({ command, argv, status: 'running', exitCode: null, lines: ['Starting...'] });
    setTerminalFocused(false);
    setScreen('run');
    runHandleRef.current =
      transport === 'mcp' && hasMcpRoute(command.id)
        ? runMtlCommandMcp(command, argv, selectedValue, setRunState)
        : runMtlCommand(command, argv, setRunState);
  };

  const prepareLaunch = (command: CommandSpec, selectedValue: string | null): void => {
    if (command.kind === 'legacy') {
      onRequestLegacy();
      return;
    }
    const flags = command.flags ?? [];
    if (flags.length === 0 && command.risk !== 'high') {
      startCommand(command, selectedValue, []);
      return;
    }
    setFiltering(false);
    setLaunch({ command, selectedValue, flags: flags.map((flag) => flag.default), cursor: 0, confirm: false });
    setScreen('launch');
  };

  const executeLaunch = (state: LaunchState): void => {
    const enabled = (state.command.flags ?? []).filter((_, i) => state.flags[i] === true).map((flag) => flag.flag);
    setLaunch(null);
    startCommand(state.command, state.selectedValue, enabled);
  };

  const selectCurrent = (): void => {
    if (screen === 'commands') {
      const command = visibleCommands[commandIndex];
      if (!command) {
        return;
      }
      switch (command.kind) {
        case 'volume':
          setPendingCommand(command);
          enterScreen('volumes');
          break;
        case 'epub':
          setPendingCommand(command);
          enterScreen('epubs');
          break;
        case 'none':
        case 'legacy':
          prepareLaunch(command, null);
          break;
      }
      return;
    }
    if (screen === 'volumes') {
      const volume = visibleVolumes[volumeIndex];
      if (!volume) {
        return;
      }
      setSelectedVolumeId(volume.id);
      if (pendingCommand) {
        const command = pendingCommand;
        setPendingCommand(null);
        prepareLaunch(command, volume.id);
      } else {
        enterScreen('home');
      }
      return;
    }
    if (screen === 'epubs') {
      const file = visibleEpubs[epubIndex];
      if (!file || !pendingCommand) {
        return;
      }
      const command = pendingCommand;
      setPendingCommand(null);
      prepareLaunch(command, file);
    }
  };

  // Lazily load + cache the detail panel for whichever volume is highlighted.
  useEffect(() => {
    if (screen !== 'volumes' || !highlightedId) {
      return;
    }
    const cached = detailCache.current.get(highlightedId);
    if (cached) {
      setDetail(cached);
      return;
    }
    const loaded = loadVolumeDetail(highlightedId);
    detailCache.current.set(highlightedId, loaded);
    setDetail(loaded);
  }, [screen, highlightedId]);

  // Tick the elapsed timer while a run is in flight.
  useEffect(() => {
    if (screen === 'run' && runState?.status === 'running') {
      const id = setInterval(() => setElapsedMs(Date.now() - runStartRef.current), 250);
      return () => clearInterval(id);
    }
    return undefined;
  }, [screen, runState?.status]);

  useEffect(() => {
    if (runState?.status !== 'running') {
      setTerminalFocused(false);
    }
  }, [runState?.status]);

  // Volumes/epubs are already loaded in useState initializers; just ensure the
  // child process is torn down on unmount. (No reload here — that caused an
  // immediate second render.)
  useEffect(() => () => cancelRun(), []);

  useInput((input, key) => {
    if (key.ctrl && input === 'c') {
      cancelRun();
      exit();
      return;
    }

    if (screen === 'run' && key.tab) {
      setTerminalFocused((value) => !value);
      return;
    }

    if (screen === 'run' && terminalFocused) {
      const data = terminalInput(input, key);
      if (data !== null) {
        runHandleRef.current?.write(data);
      }
      return;
    }

    if (key.escape) {
      if (filtering) {
        setFiltering(false);
        setFilter('');
        return;
      }
      if (filter) {
        setFilter('');
        return;
      }
      if (screen === 'home') {
        exit();
        return;
      }
      if (screen === 'run') {
        cancelRun();
        setTerminalFocused(false);
      }
      if (screen === 'launch') {
        setLaunch(null);
      }
      setPendingCommand(null);
      setScreen('home');
      return;
    }

    if (screen === 'home') {
      if (key.upArrow) {
        setHomeIndex((value) => clamp(value - 1, homeItems.length));
      } else if (key.downArrow) {
        setHomeIndex((value) => clamp(value + 1, homeItems.length));
      } else if (key.return) {
        const item = homeItems[homeIndex];
        if (!item) {
          return;
        }
        switch (item.id) {
          case 'commands':
            enterScreen('commands');
            break;
          case 'volumes':
            enterScreen('volumes');
            break;
          case 'refresh':
            refresh();
            break;
          case 'legacy':
            onRequestLegacy();
            break;
          case 'exit':
            exit();
            break;
        }
      }
      return;
    }

    if (screen === 'launch' && launch) {
      const flagCount = launch.command.flags?.length ?? 0;
      const rowCount = flagCount + 1;
      if (key.upArrow) {
        setLaunch({ ...launch, cursor: clamp(launch.cursor - 1, rowCount) });
      } else if (key.downArrow) {
        setLaunch({ ...launch, cursor: clamp(launch.cursor + 1, rowCount) });
      } else if (input === ' ' && launch.cursor < flagCount) {
        setLaunch({ ...launch, flags: launch.flags.map((v, i) => (i === launch.cursor ? !v : v)), confirm: false });
      } else if (key.return) {
        if (launch.cursor < flagCount) {
          setLaunch({ ...launch, flags: launch.flags.map((v, i) => (i === launch.cursor ? !v : v)), confirm: false });
        } else if (launch.command.risk === 'high' && !launch.confirm) {
          setLaunch({ ...launch, confirm: true });
        } else {
          executeLaunch(launch);
        }
      }
      return;
    }

    const isListScreen = screen === 'commands' || screen === 'volumes' || screen === 'epubs';
    if (isListScreen) {
      const length =
        screen === 'commands' ? visibleCommands.length : screen === 'volumes' ? visibleVolumes.length : visibleEpubs.length;
      const setIndex =
        screen === 'commands' ? setCommandIndex : screen === 'volumes' ? setVolumeIndex : setEpubIndex;

      if (filtering) {
        if (key.backspace || key.delete) {
          setFilter((value) => value.slice(0, -1));
          setIndex(0);
        } else if (key.return) {
          selectCurrent();
        } else if (key.upArrow) {
          setIndex((value) => clamp(value - 1, length));
        } else if (key.downArrow) {
          setIndex((value) => clamp(value + 1, length));
        } else if (isPrintable(input, key)) {
          setFilter((value) => value + input);
          setIndex(0);
        }
        return;
      }

      if (input === '/') {
        setFiltering(true);
      } else if (key.upArrow) {
        setIndex((value) => clamp(value - 1, length));
      } else if (key.downArrow) {
        setIndex((value) => clamp(value + 1, length));
      } else if (input === 'g') {
        setIndex(0);
      } else if (input === 'G') {
        setIndex(clamp(length - 1, length));
      } else if (input === 's' && screen === 'volumes') {
        setSortMode((mode) => SORT_ORDER[(SORT_ORDER.indexOf(mode) + 1) % SORT_ORDER.length] ?? 'recent');
        setVolumeIndex(0);
      } else if (key.return) {
        selectCurrent();
      }
    }
  });

  const trail = trailFor(screen, launch);
  const hint = hintFor(screen, filtering);

  return (
    <Box flexDirection="column" height={rows} width={columns} paddingX={1} overflow="hidden">
      <Header selectedVolume={selectedVolume} trail={trail} />

      <Box flexDirection="column" flexGrow={1} overflow="hidden">
      {screen === 'home' && (
        <>
          <Dashboard volumes={volumes} />
          <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1}>
            <Text bold>Home</Text>
            <ScrollList
              items={homeItems}
              selected={homeIndex}
              windowSize={homeItems.length}
              render={(item, active) => (
                <Box key={item.id} flexDirection="column">
                  <Text inverse={active} color={active ? 'cyan' : 'white'}>
                    {' '}{item.label}{' '}
                  </Text>
                  {active && <Text color="gray">   {item.detail}</Text>}
                </Box>
              )}
            />
          </Box>
        </>
      )}

      {screen === 'commands' && (
        <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1}>
          <Text bold>Command Palette</Text>
          <FilterLine filtering={filtering} filter={filter} count={visibleCommands.length} />
          <ScrollList
            items={visibleCommands}
            selected={commandIndex}
            windowSize={listRows}
            render={(command, active) => (
              <Box key={command.id} flexDirection="column">
                <Text inverse={active} color={active ? 'cyan' : 'white'} wrap="truncate-end">
                  {' '}{command.label} <Text color={riskColor(command.risk)}>[{command.risk}]</Text>{' '}
                </Text>
                {active && <Text color="gray" wrap="truncate-end">   {command.detail}</Text>}
              </Box>
            )}
          />
        </Box>
      )}

      {screen === 'volumes' && (
        <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1}>
          <Text bold>{pendingCommand ? `Select Volume · ${pendingCommand.label}` : 'Volume Workbench'}</Text>
          <FilterLine filtering={filtering} filter={filter} count={visibleVolumes.length} sort={sortMode} />
          <Box flexDirection="row">
            <Box flexDirection="column" width="55%">
              <ScrollList
                items={visibleVolumes}
                selected={volumeIndex}
                windowSize={volumeRows}
                render={(volume, active) => (
                  <Box key={volume.id} flexDirection="column">
                    <Text inverse={active} color={active ? 'cyan' : 'white'} wrap="truncate-end">
                      {' '}{volume.title} {!volume.hasEnTitle ? '·JP' : ''}
                    </Text>
                    <Text color="gray" wrap="truncate-end">
                      {'   '}{volume.translatedCount}/{volume.chapterCount} ch · {volume.author}
                    </Text>
                  </Box>
                )}
              />
            </Box>
            <Box flexDirection="column" width="45%">
              <DetailPanel volume={highlightedVolume} detail={detail} />
            </Box>
          </Box>
        </Box>
      )}

      {screen === 'epubs' && (
        <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} marginTop={1}>
          <Text bold>{pendingCommand ? `Select EPUB · ${pendingCommand.label}` : 'EPUB Inputs'}</Text>
          <FilterLine filtering={filtering} filter={filter} count={visibleEpubs.length} />
          <ScrollList
            items={visibleEpubs}
            selected={epubIndex}
            windowSize={listRows}
            render={(file, active) => (
              <Text key={file} inverse={active} color={active ? 'cyan' : 'white'} wrap="truncate-end">
                {' '}{basename(file)}{' '}
              </Text>
            )}
          />
        </Box>
      )}

      {screen === 'launch' && launch && <LaunchView launch={launch} python="python" />}

      {screen === 'run' && runState && (
        <RunView
          runState={runState}
          elapsedMs={elapsedMs}
          logRows={logRows}
          terminalFocused={terminalFocused}
        />
      )}
      </Box>

      <Footer hint={hint} />
    </Box>
  );
}
