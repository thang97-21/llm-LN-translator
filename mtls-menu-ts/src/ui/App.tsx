import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import type { Key } from 'ink';
import Spinner from 'ink-spinner';
import { CLI_CAPABILITIES, effectiveRisk, hydrateMcpCapabilities, initialValues, previewCapability, serializeCli, serializeMcp, validateCapability } from '../core/capabilities.js';
import { appendConsole, browseConsole, createConsole, jumpConsole, setConsoleMode, visibleConsoleEntries } from '../core/console.js';
import { callMcpTool, closeMcpClient, listMcpTools } from '../core/mcpClient.js';
import { filterVolumes, fuzzyMatch, listChapters, loadEpubs, loadVolumeDetail, loadVolumes, pipelineRoot, runCliCapability, sortVolumes } from '../core/mtls.js';
import { runPreflight } from '../core/preflight.js';
import type { CapabilitySpec, ConsoleSeverity, FormValues, Preflight, RunHandle, RunState, SortMode, VolumeSummary } from '../core/types.js';
import { Badge, PhaseStrip, ProgressBar, riskColor } from './components.js';
import { layoutForColumns } from './layout.js';
import { useTerminalSize } from './useTerminalSize.js';

type Nav = 'dashboard' | 'workflows' | 'volumes' | 'inputs' | 'advanced' | 'console' | 'diagnostics';
type FormState = { spec: CapabilitySpec; values: FormValues; cursor: number; confirmation: 0 | 1 | 2; issues: readonly string[] };
type Workspace = { nav: Nav; navIndex: number; itemIndex: number; activeVolume: string | null; search: string; searching: boolean; form: FormState | null; run: RunState | null; terminalFocused: boolean; cancelConfirm: boolean; sort: SortMode };
type Action =
  | { type: 'nav'; nav: Nav; navIndex: number }
  | { type: 'item'; index: number }
  | { type: 'search'; value: string; active?: boolean }
  | { type: 'activeVolume'; id: string | null }
  | { type: 'form'; form: FormState | null }
  | { type: 'run'; run: RunState | null }
  | { type: 'console'; text: string; source: 'cli' | 'mcp' | 'system'; severity?: ConsoleSeverity; stage?: string }
  | { type: 'runDone'; code: number | null; cancelled: boolean }
  | { type: 'consoleMode'; mode: 'follow' | 'browse' | 'input' }
  | { type: 'consoleBrowse'; delta: number; viewport: number }
  | { type: 'consoleJump'; destination: 'home' | 'end'; viewport: number }
  | { type: 'terminalFocus'; value: boolean }
  | { type: 'cancelConfirm'; value: boolean }
  | { type: 'sort'; value: SortMode };

const NAV: readonly { id: Nav; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' }, { id: 'workflows', label: 'Workflows' }, { id: 'volumes', label: 'Volumes' }, { id: 'inputs', label: 'Inputs' }, { id: 'advanced', label: 'Advanced Toolbox' }, { id: 'console', label: 'Console' }, { id: 'diagnostics', label: 'Diagnostics' },
];

function reducer(state: Workspace, action: Action): Workspace {
  switch (action.type) {
    case 'nav': return { ...state, nav: action.nav, navIndex: action.navIndex, itemIndex: 0, search: '', searching: false, cancelConfirm: false };
    case 'item': return { ...state, itemIndex: action.index };
    case 'search': return { ...state, search: action.value, searching: action.active ?? state.searching, itemIndex: 0 };
    case 'activeVolume': return { ...state, activeVolume: action.id };
    case 'form': return { ...state, form: action.form };
    case 'run': return { ...state, run: action.run, nav: 'console', navIndex: NAV.findIndex((item) => item.id === 'console'), terminalFocused: false, cancelConfirm: false };
    case 'console': return state.run ? { ...state, run: { ...state.run, console: appendConsole(state.run.console, action.text, action.source, action.severity, action.stage) } } : state;
    case 'runDone': return state.run?.status === 'running' ? { ...state, run: { ...state.run, status: action.cancelled ? 'cancelled' : action.code === 0 ? 'done' : 'failed', exitCode: action.code, console: appendConsole(state.run.console, action.cancelled ? 'Action cancelled.' : `Action finished with code ${action.code ?? 'unknown'}.`, 'system', action.cancelled ? 'warning' : action.code === 0 ? 'success' : 'error', 'complete') } } : state;
    case 'consoleMode': return state.run ? { ...state, run: { ...state.run, console: setConsoleMode(state.run.console, action.mode) } } : state;
    case 'consoleBrowse': return state.run ? { ...state, run: { ...state.run, console: browseConsole(state.run.console, action.delta, action.viewport) } } : state;
    case 'consoleJump': return state.run ? { ...state, run: { ...state.run, console: jumpConsole(state.run.console, action.destination, action.viewport) } } : state;
    case 'terminalFocus': return { ...state, terminalFocused: action.value };
    case 'cancelConfirm': return { ...state, cancelConfirm: action.value };
    case 'sort': return { ...state, sort: action.value, itemIndex: 0 };
  }
}

function clamp(value: number, length: number): number { return Math.max(0, Math.min(Math.max(0, length - 1), value)); }
function printable(input: string, key: Key): boolean { return input.length === 1 && input >= ' ' && !key.ctrl && !key.meta && !key.tab && !key.return; }
function basename(value: string): string { return value.split(/[\\/]/).pop() ?? value; }
function initialWorkspace(volumes: readonly VolumeSummary[]): Workspace { return { nav: 'dashboard', navIndex: 0, itemIndex: 0, activeVolume: volumes[0]?.id ?? null, search: '', searching: false, form: null, run: null, terminalFocused: false, cancelConfirm: false, sort: 'recent' }; }

function Header({ workspace, preflight, columns }: { workspace: Workspace; preflight: Preflight; columns: number }) {
  const active = workspace.activeVolume ?? 'none';
  return <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
    <Box justifyContent="space-between"><Text bold color="cyan">DeepSeek_MTLS · Operator Console</Text><Text color="gray">{layoutForColumns(columns)}</Text></Box>
    <Text wrap="truncate-end">volume: <Text color={workspace.activeVolume ? 'green' : 'yellow'}>{active}</Text> · python <Badge label={preflight.pythonStatus} color={preflight.pythonStatus === 'ready' ? 'green' : 'red'} /> · MCP <Badge label={preflight.mcpStatus} color={preflight.mcpStatus === 'ready' ? 'green' : preflight.mcpStatus === 'checking' ? 'yellow' : 'red'} /> · API key <Badge label={preflight.apiKeyPresent ? 'present' : 'missing'} color={preflight.apiKeyPresent ? 'green' : 'yellow'} /></Text>
  </Box>;
}

function Navigation({ workspace }: { workspace: Workspace }) { return <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1} width={22}>{NAV.map((item, index) => <Text key={item.id} inverse={workspace.navIndex === index} color={workspace.navIndex === index ? 'cyan' : 'white'}>{' '}{item.label}{' '}</Text>)}</Box>; }

function CapabilityList({ items, index, query }: { items: readonly CapabilitySpec[]; index: number; query: string }) {
  const visible = items.filter((item) => fuzzyMatch(query, `${item.group} ${item.label} ${item.detail}`));
  return <Box flexDirection="column"><Text bold>{visible.length} capability{visible.length === 1 ? '' : 'ies'}{query ? <Text color="cyan"> · search: {query}</Text> : null}</Text>{visible.length ? visible.map((item, position) => <Box key={item.id} flexDirection="column"><Text inverse={position === index} color={position === index ? 'cyan' : item.available === false ? 'gray' : 'white'}>{' '}{item.label} <Text color={riskColor(item.risk)}>[{item.risk}]</Text>{' '}</Text>{position === index && <Text color="gray" wrap="truncate-end">  {item.group}: {item.detail}{item.unavailableReason ? ` — ${item.unavailableReason}` : ''}</Text>}</Box>) : <Text color="yellow">Nothing matches.</Text>}</Box>;
}

function Inspector({ volume }: { volume: VolumeSummary | null }) { if (!volume) return <Box borderStyle="round" borderColor="gray" paddingX={1}><Text color="gray">Select a volume to inspect it.</Text></Box>; const detail = loadVolumeDetail(volume.id); return <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1}><Text bold wrap="truncate-end">{volume.title}</Text><Text color="gray">{volume.author} · {volume.series}</Text><ProgressBar value={volume.translatedCount} total={volume.chapterCount} /><PhaseStrip phases={volume.phases} /><Text color="gray">JP {detail.jpChapters} · EN {detail.enChapters} · QC {detail.qcReports}</Text>{detail.lastError ? <Text color="red" wrap="truncate-end">{detail.lastError}</Text> : null}</Box>; }

function FormPanel({ form, activeVolume, preflight }: { form: FormState; activeVolume: string | null; preflight: Preflight }) {
  const risk = effectiveRisk(form.spec, form.values); let preview = '(complete required fields to preview)'; try { preview = previewCapability(form.spec, form.values, preflight.python); } catch { /* validation gives the useful error */ }
  const chapters = activeVolume ? listChapters(activeVolume) : [];
  return <Box flexDirection="column" borderStyle="round" borderColor={risk === 'overwrite' ? 'red' : 'cyan'} paddingX={1}><Text bold>{form.spec.label} <Text color={riskColor(risk)}>[{risk}]</Text></Text><Text color="gray" wrap="truncate-end">{form.spec.detail}</Text>{form.spec.fields.map((field, index) => <Box key={field.key} flexDirection="column"><Text inverse={form.cursor === index} color={form.cursor === index ? 'cyan' : 'white'}>{' '}{field.label}: <Text color="yellow">{String(form.values[field.key] ?? '') || '—'}</Text>{field.required ? ' *' : ''}</Text>{form.cursor === index && field.kind === 'chapter-list' && chapters.length > 0 ? <Text color="gray">  JP: {chapters.join(', ')} · press a for all, or type comma-separated IDs</Text> : null}</Box>)}<Text color="gray" wrap="truncate-end">review: {preview}</Text>{form.issues.map((issue) => <Text key={issue} color="red">! {issue}</Text>)}<Text inverse={form.cursor === form.spec.fields.length} color={risk === 'overwrite' ? 'red' : 'green'}>{' '}▶ {form.confirmation === 0 ? 'Review and confirm' : form.confirmation === 1 ? risk === 'overwrite' ? 'Press Enter for overwrite warning' : 'Press Enter to launch' : 'Press Enter to acknowledge overwrite'}{' '}</Text><Text color="gray">Up/Down fields · type to edit · Space toggles booleans · Enter advances · Esc closes</Text></Box>;
}

function ConsolePanel({ run, rows, focused, cancelConfirm }: { run: RunState | null; rows: number; focused: boolean; cancelConfirm: boolean }) {
  if (!run) return <Box borderStyle="round" borderColor="gray" paddingX={1}><Text color="gray">No action yet. Runs stay here after completion instead of evaporating into a 200-line lie.</Text></Box>;
  const entries = visibleConsoleEntries(run.console, Math.max(4, rows - 9));
  return <Box flexDirection="column" borderStyle="round" borderColor={run.status === 'failed' ? 'red' : run.status === 'done' ? 'green' : focused ? 'magenta' : 'cyan'} paddingX={1}><Text bold>{run.status === 'running' ? <Text color="cyan"><Spinner type="dots" /> </Text> : null}{run.capability.label} · {run.status}</Text><Text color="gray" wrap="truncate-end">{run.preview}</Text><Text color={focused ? 'magenta' : 'gray'}>focus: {focused ? run.console.mode : 'menu'} · retained {run.console.entries.length}/5000 · {run.console.unseen ? `${run.console.unseen} unseen` : 'tail current'}</Text>{cancelConfirm ? <Text color="red">Cancel the running action? Enter confirms; Esc keeps it alive.</Text> : null}{entries.map((entry) => <Text key={entry.id} color={entry.severity === 'error' ? 'red' : entry.severity === 'warning' ? 'yellow' : entry.severity === 'success' ? 'green' : 'white'} wrap="truncate-end">[{entry.source}/{entry.stage}] {entry.text}</Text>)}<Text color="gray">Tab focus · ↑↓ scroll · PgUp/PgDn page · Home/End · f follow · i raw stdin · Esc browse</Text></Box>;
}

export function App() {
  const { exit } = useApp(); const { rows, columns } = useTerminalSize();
  const [volumes, setVolumes] = useState<VolumeSummary[]>(() => loadVolumes()); const [epubs, setEpubs] = useState<string[]>(() => loadEpubs());
  const [workspace, dispatch] = useReducer(reducer, volumes, initialWorkspace); const [preflight, setPreflight] = useState<Preflight>(() => runPreflight()); const [mcpCapabilities, setMcpCapabilities] = useState<CapabilitySpec[]>(() => hydrateMcpCapabilities([]));
  const runHandle = useRef<RunHandle | null>(null); const abortController = useRef<AbortController | null>(null);
  const layout = layoutForColumns(columns); const activeVolume = volumes.find((volume) => volume.id === workspace.activeVolume) ?? null;
  const refresh = (): void => { const nextVolumes = loadVolumes(); setVolumes(nextVolumes); setEpubs(loadEpubs()); dispatch({ type: 'activeVolume', id: nextVolumes.some((item) => item.id === workspace.activeVolume) ? workspace.activeVolume : nextVolumes[0]?.id ?? null }); };

  useEffect(() => { const result = runPreflight(); setPreflight(result); if (result.importsStatus !== 'ready') return; const controller = new AbortController(); void listMcpTools(controller.signal).then((tools) => { setMcpCapabilities(hydrateMcpCapabilities(tools)); setPreflight((current) => ({ ...current, mcpStatus: 'ready', detail: `${tools.length} MCP tools available.` })); }).catch((error: unknown) => setPreflight((current) => ({ ...current, mcpStatus: 'missing', detail: error instanceof Error ? error.message : String(error) }))); return () => controller.abort(); }, []);
  useEffect(() => () => { runHandle.current?.cancel(); abortController.current?.abort(); void closeMcpClient(); }, []);

  const contentItems = useMemo(() => workspace.nav === 'workflows' ? [...CLI_CAPABILITIES] : workspace.nav === 'advanced' ? mcpCapabilities : [], [workspace.nav, mcpCapabilities]);
  const filteredItems = useMemo(() => contentItems.filter((item) => fuzzyMatch(workspace.search, `${item.label} ${item.group} ${item.detail}`)), [contentItems, workspace.search]);
  const volumeItems = useMemo(() => sortVolumes(filterVolumes(volumes, workspace.search), workspace.sort), [volumes, workspace.search, workspace.sort]);

  const openForm = (spec: CapabilitySpec, values?: FormValues): void => { if (spec.available === false) return; dispatch({ type: 'form', form: { spec, values: values ?? initialValues(spec, workspace.activeVolume), cursor: 0, confirmation: 0, issues: [] } }); };
  const stopRun = (): void => { runHandle.current?.cancel(); abortController.current?.abort(); runHandle.current = null; abortController.current = null; dispatch({ type: 'runDone', code: null, cancelled: true }); dispatch({ type: 'cancelConfirm', value: false }); };
  const launch = (form: FormState): void => {
    const issues = validateCapability(form.spec, form.values, pipelineRoot); if (issues.length) { dispatch({ type: 'form', form: { ...form, issues: issues.map((issue) => issue.message) } }); return; }
    const preview = previewCapability(form.spec, form.values, preflight.python); const run: RunState = { capability: form.spec, preview, status: 'running', exitCode: null, console: appendConsole(createConsole(), preview, 'system', 'info', 'review') }; dispatch({ type: 'form', form: null }); dispatch({ type: 'run', run });
    if (form.spec.route.transport === 'cli') {
      runHandle.current = runCliCapability(form.spec, serializeCli(form.spec, form.values), (text, source, severity) => dispatch({ type: 'console', text, source, ...(severity ? { severity } : {}), stage: 'cli' }), (code, cancelled) => dispatch({ type: 'runDone', code, cancelled }));
    } else if (form.spec.route.transport === 'mcp') {
      const controller = new AbortController(); const tool = form.spec.route.tool; abortController.current = controller;
      void callMcpTool(tool, serializeMcp(form.spec, form.values), controller.signal).then((result) => { dispatch({ type: 'console', text: result.structured ? JSON.stringify(result.structured, null, 2) : result.text || '(empty response)', source: 'mcp', severity: result.ok ? 'success' : 'error', stage: tool }); dispatch({ type: 'runDone', code: result.ok ? 0 : 1, cancelled: false }); }).catch((error: unknown) => { dispatch({ type: 'console', text: error instanceof Error ? error.message : String(error), source: 'mcp', severity: 'error', stage: tool }); dispatch({ type: 'runDone', code: 1, cancelled: controller.signal.aborted }); });
    }
  };

  useInput((input, key) => {
    const special = key as Key & { pageUp?: boolean; pageDown?: boolean; home?: boolean; end?: boolean };
    if (key.ctrl && input === 'c') { if (workspace.run?.status === 'running') stopRun(); else exit(); return; }
    if (workspace.cancelConfirm) { if (key.return) stopRun(); else if (key.escape) dispatch({ type: 'cancelConfirm', value: false }); return; }
    if (workspace.form) {
      const form = workspace.form; const field = form.spec.fields[form.cursor]; const setForm = (next: FormState): void => dispatch({ type: 'form', form: next });
      if (key.escape) { dispatch({ type: 'form', form: null }); return; }
      if (key.upArrow) { setForm({ ...form, cursor: clamp(form.cursor - 1, form.spec.fields.length + 1), confirmation: 0, issues: [] }); return; }
      if (key.downArrow) { setForm({ ...form, cursor: clamp(form.cursor + 1, form.spec.fields.length + 1), confirmation: 0, issues: [] }); return; }
      if (form.cursor === form.spec.fields.length) {
        if (key.return) { const risk = effectiveRisk(form.spec, form.values); if (form.confirmation === 0) setForm({ ...form, confirmation: 1, issues: [] }); else if (risk === 'overwrite' && form.confirmation === 1) setForm({ ...form, confirmation: 2 }); else launch(form); } return;
      }
      if (!field) return;
      const value = form.values[field.key];
      if ((field.kind === 'boolean' && (input === ' ' || key.return))) { setForm({ ...form, values: { ...form.values, [field.key]: value !== true }, confirmation: 0, issues: [] }); return; }
      if (field.kind === 'enum' && (input === ' ' || key.return)) { const choices = field.choices ?? []; const current = choices.indexOf(String(value ?? '')); setForm({ ...form, values: { ...form.values, [field.key]: choices[(current + 1) % Math.max(1, choices.length)] ?? '' }, confirmation: 0, issues: [] }); return; }
      if (field.kind === 'chapter-list' && input === 'a' && workspace.activeVolume) { setForm({ ...form, values: { ...form.values, [field.key]: listChapters(workspace.activeVolume).join(',') }, confirmation: 0, issues: [] }); return; }
      if (key.backspace || key.delete) { setForm({ ...form, values: { ...form.values, [field.key]: String(value ?? '').slice(0, -1) }, confirmation: 0, issues: [] }); return; }
      if (printable(input, key)) setForm({ ...form, values: { ...form.values, [field.key]: String(value ?? '') + input }, confirmation: 0, issues: [] });
      return;
    }
    if (workspace.terminalFocused && workspace.nav === 'console') {
      if (key.tab) { dispatch({ type: 'terminalFocus', value: false }); return; }
      if (key.escape) { if (workspace.run?.console.mode === 'input') dispatch({ type: 'consoleMode', mode: 'browse' }); else if (workspace.run?.status === 'running') dispatch({ type: 'cancelConfirm', value: true }); else dispatch({ type: 'terminalFocus', value: false }); return; }
      if (key.upArrow) dispatch({ type: 'consoleBrowse', delta: 1, viewport: rows - 9 }); else if (key.downArrow) dispatch({ type: 'consoleBrowse', delta: -1, viewport: rows - 9 }); else if (special.pageUp) dispatch({ type: 'consoleBrowse', delta: rows - 9, viewport: rows - 9 }); else if (special.pageDown) dispatch({ type: 'consoleBrowse', delta: -(rows - 9), viewport: rows - 9 }); else if (special.home) dispatch({ type: 'consoleJump', destination: 'home', viewport: rows - 9 }); else if (special.end || input === 'f') dispatch({ type: 'consoleJump', destination: 'end', viewport: rows - 9 }); else if (input === 'i' && workspace.run?.capability.route.transport === 'cli') dispatch({ type: 'consoleMode', mode: 'input' }); else if (workspace.run?.console.mode === 'input') { if (key.return) runHandle.current?.write('\r'); else if (key.backspace) runHandle.current?.write('\x7f'); else if (printable(input, key)) runHandle.current?.write(input); }
      return;
    }
    if (key.tab && workspace.nav === 'console') { dispatch({ type: 'terminalFocus', value: true }); return; }
    if (key.escape) { if (workspace.run?.status === 'running' && workspace.nav === 'console') dispatch({ type: 'cancelConfirm', value: true }); else if (workspace.searching || workspace.search) dispatch({ type: 'search', value: '', active: false }); else exit(); return; }
    if (input === '/') { dispatch({ type: 'search', value: '', active: true }); return; }
    if (workspace.searching) { if (key.backspace || key.delete) dispatch({ type: 'search', value: workspace.search.slice(0, -1), active: true }); else if (printable(input, key)) dispatch({ type: 'search', value: workspace.search + input, active: true }); else if (key.return) dispatch({ type: 'search', value: workspace.search, active: false }); return; }
    if (key.leftArrow || input === '[') { dispatch({ type: 'nav', nav: NAV[clamp(workspace.navIndex - 1, NAV.length)]!.id, navIndex: clamp(workspace.navIndex - 1, NAV.length) }); return; }
    if (key.rightArrow || input === ']') { dispatch({ type: 'nav', nav: NAV[clamp(workspace.navIndex + 1, NAV.length)]!.id, navIndex: clamp(workspace.navIndex + 1, NAV.length) }); return; }
    if (key.upArrow) { dispatch({ type: 'item', index: Math.max(0, workspace.itemIndex - 1) }); return; }
    if (key.downArrow) { const length = workspace.nav === 'volumes' ? volumeItems.length : workspace.nav === 'inputs' ? epubs.length : workspace.nav === 'workflows' || workspace.nav === 'advanced' ? filteredItems.length : NAV.length; dispatch({ type: 'item', index: clamp(workspace.itemIndex + 1, length) }); return; }
    if (input === 'r') { refresh(); return; }
    if (input === 's' && workspace.nav === 'volumes') { dispatch({ type: 'sort', value: workspace.sort === 'recent' ? 'series' : workspace.sort === 'series' ? 'progress' : 'recent' }); return; }
    if (key.return) {
      if (workspace.nav === 'dashboard') { const item = NAV[workspace.itemIndex] ?? NAV[0]!; dispatch({ type: 'nav', nav: item.id, navIndex: NAV.findIndex((candidate) => candidate.id === item.id) }); }
      else if (workspace.nav === 'workflows' || workspace.nav === 'advanced') { const item = filteredItems[workspace.itemIndex]; if (item) openForm(item); }
      else if (workspace.nav === 'volumes') { const item = volumeItems[workspace.itemIndex]; if (item) dispatch({ type: 'activeVolume', id: item.id }); }
      else if (workspace.nav === 'inputs') { const item = epubs[workspace.itemIndex]; const extract = CLI_CAPABILITIES.find((capability) => capability.id === 'extract'); if (item && extract) openForm(extract, { ...initialValues(extract, workspace.activeVolume), epub_path: item }); }
    }
  });

  const dashboard = <Box flexDirection="column"><Text bold>Operator dashboard</Text><Text color="gray">{volumes.length} volume(s), {epubs.length} EPUB input(s). `list` and `status` are views now; spawning Python to read a directory was never a feature.</Text>{NAV.map((item, index) => <Text key={item.id} inverse={workspace.itemIndex === index}>{' '}{item.label}{' '}</Text>)}</Box>;
  const main = workspace.form ? <FormPanel form={workspace.form} activeVolume={workspace.activeVolume} preflight={preflight} /> : workspace.nav === 'dashboard' ? dashboard : workspace.nav === 'workflows' || workspace.nav === 'advanced' ? <CapabilityList items={contentItems} index={workspace.itemIndex} query={workspace.search} /> : workspace.nav === 'volumes' ? <Box flexDirection="column"><Text bold>Volumes · sort {workspace.sort}</Text>{volumeItems.map((volume, index) => <Text key={volume.id} inverse={workspace.itemIndex === index} color={workspace.activeVolume === volume.id ? 'green' : 'white'}>{' '}{volume.title} ({volume.translatedCount}/{volume.chapterCount}){' '}</Text>) || <Text color="yellow">No manifests in work/ yet.</Text>}</Box> : workspace.nav === 'inputs' ? <Box flexDirection="column"><Text bold>Project EPUB inputs</Text>{epubs.map((file, index) => <Text key={file} inverse={workspace.itemIndex === index}>{' '}{basename(file)}{' '}</Text>) || <Text color="yellow">raw/ is empty. Put an EPUB there; the picker only exposes project-scoped inputs.</Text>}</Box> : workspace.nav === 'console' ? <ConsolePanel run={workspace.run} rows={rows} focused={workspace.terminalFocused} cancelConfirm={workspace.cancelConfirm} /> : <Box flexDirection="column"><Text bold>Diagnostics</Text><Text>Python: {preflight.python} ({preflight.pythonStatus})</Text><Text>Imports: {preflight.importsStatus} · MCP: {preflight.mcpStatus} · API key: {preflight.apiKeyPresent ? 'present' : 'missing'}</Text><Text color={preflight.importsStatus === 'ready' ? 'green' : 'yellow'}>{preflight.detail}</Text>{preflight.importsStatus !== 'ready' && <Text color="cyan">Repair: {preflight.repairCommand}</Text>}</Box>;
  const inspector = <Inspector volume={activeVolume} />;
  return <Box flexDirection="column" height={rows} width={columns} paddingX={1} overflow="hidden"><Header workspace={workspace} preflight={preflight} columns={columns} /><Box flexGrow={1} marginTop={1} flexDirection={layout === 'single-pane' ? 'column' : 'row'}>{layout !== 'single-pane' && <Navigation workspace={workspace} />}<Box flexDirection="column" flexGrow={1} marginLeft={layout === 'single-pane' ? 0 : 1}>{layout === 'single-pane' && <Text color="gray">{NAV[workspace.navIndex]?.label ?? workspace.nav} › {workspace.form?.spec.label ?? 'workspace'}</Text>}{main}</Box>{layout === 'three-pane' && <Box width={35} marginLeft={1}>{inspector}</Box>}{layout === 'two-pane' && workspace.nav === 'volumes' && <Box width={35} marginLeft={1}>{inspector}</Box>}</Box><Text color="gray">↑↓ move · Enter select · / search · r refresh · Tab terminal focus · Ctrl+C abort/exit</Text></Box>;
}
