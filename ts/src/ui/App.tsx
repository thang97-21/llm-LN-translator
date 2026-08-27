import { useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import type { Key } from 'ink';
import { CLI_CAPABILITIES, effectiveRisk, hydrateMcpCapabilities, initialValues, previewCapability, serializeCli, serializeMcp, validateCapability } from '../core/capabilities.js';
import { appendConsole, createConsole } from '../core/console.js';
import { CONFIG_PATH, loadConfigFields, saveConfigField, type ConfigFieldState } from '../core/configFile.js';
import { activeProvider, filterFieldsForProvider, unquoteYamlScalar, validateConfigInput } from '../core/configSchema.js';
import { callMcpTool, closeMcpClient, listMcpTools } from '../core/mcpClient.js';
import { filterVolumes, fuzzyMatch, listChapters, loadEpubs, loadRuntimeConfigLines, loadVolumes, pipelineRoot, runCliCapability, sortVolumes, workRoot } from '../core/mtls.js';
import { runPreflight } from '../core/preflight.js';
import { asVolumeId } from '../core/types.js';
import type { CapabilitySpec, ConfigLine, FormValues, Preflight, RunHandle, RunState, VolumeSummary } from '../core/types.js';
import { layoutForColumns } from './layout.js';
import { useDirectoryWatcher, useFileWatcher } from './useFileWatcher.js';
import { useMouseScroll } from './useMouseScroll.js';
import { useTerminalSize } from './useTerminalSize.js';
import { ConsolePanel } from './ConsolePanel.js';
import { ConfigurationPanel, DeepSeekPricingPanel, DeveloperPanel, RuntimeConfigPanel } from './ConfigPanels.js';
import { FormPanel } from './FormPanel.js';
import { CapabilityList, Header, Inspector, Navigation } from './panels.js';
import { NAV, initialWorkspace, reducer, type FormState, type Workspace } from './workspaceMachine.js';

function clamp(value: number, length: number): number { return Math.max(0, Math.min(Math.max(0, length - 1), value)); }
function printable(input: string, key: Key): boolean { return input.length === 1 && input >= ' ' && !key.ctrl && !key.meta && !key.tab && !key.return; }
// Every capability (Prep Volume included) opens the same FormPanel, and a
// form can be open on top of ANY nav bucket (workflows, advanced, ...) — it
// is orthogonal to workspace.nav. The old footer only switched on nav, so
// filling out a form still advertised list-browsing keys ('/' search,
// 'r' refresh, 'Enter select') that don't do anything while a field has
// focus, plus an unconditional "Ctrl+C abort" even with no run to abort.
// FormPanel already prints its own accurate field-editing hint inline, so
// the footer here only needs to add what that line doesn't cover.
function footerText(workspace: Workspace): string {
  const runHint = workspace.run?.status === 'running' ? ' · Ctrl+C abort background run' : '';
  if (workspace.form) return `Esc closes${runHint} · Ctrl+Shift+Esc exit`;
  if (workspace.nav === 'dashboard') return `↑↓ workspaces · Enter open · PgUp/PgDn config · d dev dry-run default · Esc back${runHint} · Ctrl+Shift+Esc exit`;
  if (workspace.nav === 'configuration') return workspace.configEdit ? 'Enter commit · Esc cancel edit · Ctrl+Shift+Esc exit' : `↑↓ move · Enter edit/toggle · Space toggle · PgUp/PgDn/Home/End jump · / search · r reload · Esc back${runHint} · Ctrl+Shift+Esc exit`;
  if (workspace.nav === 'console') return workspace.run?.status === 'running' ? 'Esc back · Ctrl+C cancel run · Ctrl+Shift+Esc exit' : 'Esc back · Ctrl+Shift+Esc exit';
  return `↑↓ move · Enter select · / search · r refresh · Esc back${runHint} · Ctrl+Shift+Esc exit`;
}

export function App() {
  const { exit } = useApp(); const { rows, columns } = useTerminalSize();
  const [volumes, setVolumes] = useState<VolumeSummary[]>(() => loadVolumes()); const [epubs, setEpubs] = useState<string[]>(() => loadEpubs());
  const [configFields, setConfigFields] = useState<ConfigFieldState[]>(() => loadConfigFields());
  const [workspace, dispatch] = useReducer(reducer, volumes, initialWorkspace); const [preflight, setPreflight] = useState<Preflight>(() => runPreflight()); const [mcpCapabilities, setMcpCapabilities] = useState<CapabilitySpec[]>(() => hydrateMcpCapabilities([]));
  const runHandle = useRef<RunHandle | null>(null); const abortController = useRef<AbortController | null>(null);
  // Developer > Dry-run default — see DeveloperPanel and openForm. Session-
  // only (not persisted to config.yaml, not restored across restarts): a
  // developer flag that silently outlived the debugging session it was
  // flipped on for is worse than one that resets and makes you notice.
  const [devDryRunDefault, setDevDryRunDefault] = useState(false);
  const layout = layoutForColumns(columns); const activeVolume = volumes.find((volume) => volume.id === workspace.activeVolume) ?? null;
  // Console is the only screen ConsolePanel ever renders on — no reason to
  // keep paying for the bordered Header, the Navigation sidebar, and (in
  // three-pane) the Inspector while looking at it. Reclaiming that chrome
  // is the actual "bigger canvas" fix; the viewport math below already
  // assumed roughly this much room and was quietly overflowing without it.
  const maximized = workspace.nav === 'console';
  // Stateful, not useMemo(() => ..., []) — the Dashboard has to reflect
  // config.yaml as it actually is right now, including edits made through
  // the Configuration screen (which writes the file directly, not through
  // this component's state) or by hand in an external editor.
  const [runtimeConfig, setRuntimeConfig] = useState<ConfigLine[]>(() => loadRuntimeConfigLines());
  useFileWatcher(CONFIG_PATH, () => setRuntimeConfig(loadRuntimeConfigLines()));
  // `volumes` is loaded via loadVolumes(), which sorts by updatedAt descending
  // at the source — independent of whatever sort mode the Volumes screen is
  // currently showing. "Latest 10 interacted works" means recency, always,
  // regardless of that display-only sort toggle.
  const recentVolumes = useMemo(() => volumes.slice(0, 10), [volumes]);
  const recentVolumeIds = useMemo(() => recentVolumes.map((volume) => volume.id), [recentVolumes]);
  const refresh = (): void => { const nextVolumes = loadVolumes(); setVolumes(nextVolumes); setEpubs(loadEpubs()); setConfigFields(loadConfigFields()); setRuntimeConfig(loadRuntimeConfigLines()); dispatch({ type: 'activeVolume', id: nextVolumes.some((item) => item.id === workspace.activeVolume) ? workspace.activeVolume : nextVolumes[0]?.id ?? null }); };
  // Volume detection resilience (see useFileWatcher.ts's useDirectoryWatcher
  // docstring for the full three-layer rationale): a directory watcher +
  // poll on work/ catches a volume created by a process outside this app
  // entirely, and the run-completion effect below catches this app's own
  // "Extract EPUB" the instant it finishes — neither depended on the other
  // before, which is how "cannot detect a just-created volume" happened.
  useDirectoryWatcher(workRoot, refresh);
  useEffect(() => {
    if (workspace.run && workspace.run.status !== 'running') refresh();
  }, [workspace.run?.status]);

  useEffect(() => { const result = runPreflight(); setPreflight(result); if (result.importsStatus !== 'ready') return; const controller = new AbortController(); void listMcpTools(controller.signal).then((tools) => { setMcpCapabilities(hydrateMcpCapabilities(tools)); setPreflight((current) => ({ ...current, mcpStatus: 'ready', detail: `${tools.length} MCP tools available.` })); }).catch((error: unknown) => setPreflight((current) => ({ ...current, mcpStatus: 'missing', detail: error instanceof Error ? error.message : String(error) }))); return () => controller.abort(); }, []);
  useEffect(() => () => { runHandle.current?.cancel(); abortController.current?.abort(); void closeMcpClient(); }, []);
  // Wheel scroll: touchpad/mouse, not keyboard — see useMouseScroll.ts for why
  // this needs its own tap into Ink's input stream. Direction signs mirror
  // whatever the arrow/page keys already do in each screen: consoleBrowse's
  // offset counts backward-from-newest (up = +delta, matching key.upArrow
  // below), configScroll's offset counts forward-from-top (up = -delta,
  // matching PageUp below) — copying one sign convention for both would
  // scroll one of the two screens backwards.
  useMouseScroll((direction, notches) => {
    const step = notches * 3;
    if (workspace.nav === 'console') dispatch({ type: 'consoleBrowse', delta: direction === 'up' ? step : -step, viewport: rows - 9 });
    else if (workspace.nav === 'dashboard') dispatch({ type: 'configScroll', delta: direction === 'up' ? -step : step, viewport: rows - 8 });
  }, !workspace.form);

  const contentItems = useMemo(() => workspace.nav === 'workflows' ? [...CLI_CAPABILITIES] : workspace.nav === 'advanced' ? mcpCapabilities : [], [workspace.nav, mcpCapabilities]);
  const filteredItems = useMemo(() => contentItems.filter((item) => fuzzyMatch(workspace.search, `${item.label} ${item.group} ${item.detail}`)), [contentItems, workspace.search]);
  const volumeItems = useMemo(() => sortVolumes(filterVolumes(volumes, workspace.search), workspace.sort), [volumes, workspace.search, workspace.sort]);
  // Search composes with the Provider gate: only the selected provider's
  // settings are ever shown, so the Configuration screen mirrors config.yaml's
  // GENERAL → provider-specific layout. Provider is read from the full list so
  // the gate survives a search that excludes the provider field itself.
  //
  // Pulling the provider string OUT of the useMemo and into the dependency
  // array is intentional: configFields is mutated in place by inline edits,
  // such that the array reference doesn't change between renders. The provider
  // value is the actual signal; the array reference is merely the container.
  const activeProviderValue = useMemo(() => activeProvider(configFields), [configFields]);
  const filteredConfigFields = useMemo(() => {
    const searched = configFields.filter((field) => fuzzyMatch(workspace.search, `${field.label} ${field.section} ${field.description}`));
    return filterFieldsForProvider(searched, activeProviderValue);
  }, [configFields, workspace.search, activeProviderValue]);

  const openForm = (spec: CapabilitySpec, values?: FormValues): void => {
    if (spec.available === false) return;
    const defaults = values ?? initialValues(spec, workspace.activeVolume);
    // Developer > Dry-run default applies to ANY capability with a dry_run
    // field — Translate Volume and Build EPUB today, and any future capability
    // that adds one — not just Translate Volume specifically.
    const withDevDefaults = devDryRunDefault && spec.fields.some((field) => field.key === 'dry_run')
      ? { ...defaults, dry_run: true }
      : defaults;
    dispatch({ type: 'form', form: { spec, values: withDevDefaults, cursor: 0, confirmation: 0, issues: [] } });
  };
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
    if (key.ctrl && key.shift && key.escape) { exit(); return; }
    if (key.ctrl && input === 'c') { if (workspace.run?.status === 'running') stopRun(); return; }
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
      // Extract EPUB / Full Pipeline's epub_path field cycles through raw/'s
      // actual contents instead of needing a separate Inputs menu to browse
      // it — this IS "the proper raw location," just surfaced where the
      // path is actually used instead of as a redundant picker screen.
      if (field.kind === 'epub' && (input === ' ' || key.return) && epubs.length > 0) { const current = epubs.indexOf(String(value ?? '')); const next = epubs[(current + 1) % epubs.length] ?? epubs[0]!; setForm({ ...form, values: { ...form.values, [field.key]: next }, confirmation: 0, issues: [] }); return; }
      // prep/translate/qc/build's required `volume` field cycles through the
      // 10 most-recently-touched volumes — the common case (working the
      // volume you just extracted or translated) needs zero typing. Typing
      // still reaches any volume outside that top 10, or one just created
      // by a run this session that hasn't been picked up by `r` refresh yet.
      if (field.kind === 'volume' && (input === ' ' || key.return) && recentVolumeIds.length > 0) { const current = recentVolumeIds.map(String).indexOf(String(value ?? '')); const next = recentVolumeIds[(current + 1) % recentVolumeIds.length] ?? recentVolumeIds[0]!; setForm({ ...form, values: { ...form.values, [field.key]: String(next) }, confirmation: 0, issues: [] }); return; }
      if (field.kind === 'chapter-list' && input === 'a' && workspace.activeVolume) { setForm({ ...form, values: { ...form.values, [field.key]: listChapters(asVolumeId(workspace.activeVolume)).join(',') }, confirmation: 0, issues: [] }); return; }
      if (key.backspace || key.delete) { setForm({ ...form, values: { ...form.values, [field.key]: String(value ?? '').slice(0, -1) }, confirmation: 0, issues: [] }); return; }
      if (printable(input, key)) setForm({ ...form, values: { ...form.values, [field.key]: String(value ?? '') + input }, confirmation: 0, issues: [] });
      return;
    }
    if (workspace.nav === 'configuration' && workspace.configEdit) {
      const edit = workspace.configEdit; const field = filteredConfigFields.find((item) => item.path === edit.fieldPath);
      if (key.escape) { dispatch({ type: 'configEdit', edit: null }); return; }
      if (key.return) {
        if (!field) { dispatch({ type: 'configEdit', edit: null }); return; }
        const validated = validateConfigInput(field, edit.buffer);
        if (!validated.ok) { dispatch({ type: 'configEdit', edit: { ...edit, error: validated.error } }); return; }
        const result = saveConfigField(field.path, validated.raw);
        if (!result.ok) { dispatch({ type: 'configEdit', edit: { ...edit, error: result.error } }); return; }
        setConfigFields(loadConfigFields());
        dispatch({ type: 'configEdit', edit: null });
        dispatch({ type: 'configStatus', status: { fieldPath: field.path, message: `Saved ${field.label}.`, ok: true } });
        return;
      }
      if (key.backspace || key.delete) { dispatch({ type: 'configEdit', edit: { ...edit, buffer: edit.buffer.slice(0, -1), error: null } }); return; }
      if (printable(input, key)) { dispatch({ type: 'configEdit', edit: { ...edit, buffer: edit.buffer + input, error: null } }); return; }
      return;
    }
    if (workspace.terminalFocused && workspace.nav === 'console') {
      if (key.tab) { dispatch({ type: 'terminalFocus', value: false }); return; }
      if (key.escape) { if (workspace.run?.console.mode === 'input') dispatch({ type: 'consoleMode', mode: 'browse' }); else if (workspace.run?.status === 'running') dispatch({ type: 'cancelConfirm', value: true }); else dispatch({ type: 'terminalFocus', value: false }); return; }
      if (key.upArrow) dispatch({ type: 'consoleBrowse', delta: 1, viewport: rows - 9 }); else if (key.downArrow) dispatch({ type: 'consoleBrowse', delta: -1, viewport: rows - 9 }); else if (special.pageUp) dispatch({ type: 'consoleBrowse', delta: rows - 9, viewport: rows - 9 }); else if (special.pageDown) dispatch({ type: 'consoleBrowse', delta: -(rows - 9), viewport: rows - 9 }); else if (special.home) dispatch({ type: 'consoleJump', destination: 'home', viewport: rows - 9 }); else if (special.end || input === 'f') dispatch({ type: 'consoleJump', destination: 'end', viewport: rows - 9 }); else if (input === 'i' && workspace.run?.capability.route.transport === 'cli') dispatch({ type: 'consoleMode', mode: 'input' }); else if (workspace.run?.console.mode === 'input') { if (key.return) runHandle.current?.write('\r'); else if (key.backspace) runHandle.current?.write('\x7f'); else if (printable(input, key)) runHandle.current?.write(input); }
      return;
    }
    if (key.tab && workspace.nav === 'console') { dispatch({ type: 'terminalFocus', value: true }); return; }
    if (key.escape) {
      if (workspace.run?.status === 'running' && workspace.nav === 'console') dispatch({ type: 'cancelConfirm', value: true });
      else if (workspace.searching || workspace.search) dispatch({ type: 'search', value: '', active: false });
      else if (workspace.nav !== 'dashboard') dispatch({ type: 'nav', nav: 'dashboard', navIndex: 0 });
      return;
    }
    if (input === '/') { dispatch({ type: 'search', value: '', active: true }); return; }
    if (workspace.searching) { if (key.backspace || key.delete) dispatch({ type: 'search', value: workspace.search.slice(0, -1), active: true }); else if (printable(input, key)) dispatch({ type: 'search', value: workspace.search + input, active: true }); else if (key.return) dispatch({ type: 'search', value: workspace.search, active: false }); return; }
    if (workspace.nav === 'dashboard') {
      if (key.upArrow) dispatch({ type: 'navCursor', navIndex: clamp(workspace.navIndex - 1, NAV.length) });
      else if (key.downArrow) dispatch({ type: 'navCursor', navIndex: clamp(workspace.navIndex + 1, NAV.length) });
      else if (special.pageUp) dispatch({ type: 'configScroll', delta: -(rows - 8), viewport: rows - 8 });
      else if (special.pageDown) dispatch({ type: 'configScroll', delta: rows - 8, viewport: rows - 8 });
      else if (special.home) dispatch({ type: 'configScroll', delta: 0, viewport: rows - 8, destination: 'home' });
      else if (special.end) dispatch({ type: 'configScroll', delta: 0, viewport: rows - 8, destination: 'end' });
      else if (key.return) { const item = NAV[workspace.navIndex] ?? NAV[0]!; dispatch({ type: 'nav', nav: item.id, navIndex: workspace.navIndex }); }
      else if (input === 'd') setDevDryRunDefault((prev) => !prev);
      return;
    }
    if (workspace.nav === 'configuration') {
      if (key.upArrow) { dispatch({ type: 'item', index: Math.max(0, workspace.itemIndex - 1) }); return; }
      if (key.downArrow) { dispatch({ type: 'item', index: clamp(workspace.itemIndex + 1, filteredConfigFields.length) }); return; }
      if (special.pageUp) { dispatch({ type: 'item', index: Math.max(0, workspace.itemIndex - 8) }); return; }
      if (special.pageDown) { dispatch({ type: 'item', index: clamp(workspace.itemIndex + 8, filteredConfigFields.length) }); return; }
      if (special.home) { dispatch({ type: 'item', index: 0 }); return; }
      if (special.end) { dispatch({ type: 'item', index: Math.max(0, filteredConfigFields.length - 1) }); return; }
      if (input === 'r') { refresh(); return; }
      if (input === ' ' || key.return) {
        const field = filteredConfigFields[workspace.itemIndex]; if (!field) return;
        if (field.kind === 'boolean') { const nextRaw = field.rawValue === 'true' ? 'false' : 'true'; const result = saveConfigField(field.path, nextRaw); if (result.ok) setConfigFields(loadConfigFields()); dispatch({ type: 'configStatus', status: { fieldPath: field.path, message: result.ok ? 'Saved.' : result.error, ok: result.ok } }); return; }
        if (field.kind === 'enum') { const choices = field.choices ?? []; const current = choices.indexOf(field.rawValue); const nextRaw = choices[(current + 1) % Math.max(1, choices.length)] ?? choices[0] ?? ''; const result = saveConfigField(field.path, nextRaw); if (result.ok) setConfigFields(loadConfigFields()); dispatch({ type: 'configStatus', status: { fieldPath: field.path, message: result.ok ? 'Saved.' : result.error, ok: result.ok } }); return; }
        if (key.return) { dispatch({ type: 'configEdit', edit: { fieldPath: field.path, buffer: unquoteYamlScalar(field.rawValue), error: null } }); return; }
        return;
      }
      return;
    }
    if (key.upArrow) { dispatch({ type: 'item', index: Math.max(0, workspace.itemIndex - 1) }); return; }
    if (key.downArrow) { const length = workspace.nav === 'volumes' ? volumeItems.length : workspace.nav === 'workflows' || workspace.nav === 'advanced' ? filteredItems.length : NAV.length; dispatch({ type: 'item', index: clamp(workspace.itemIndex + 1, length) }); return; }
    if (input === 'r') { refresh(); return; }
    if (input === 's' && workspace.nav === 'volumes') { dispatch({ type: 'sort', value: workspace.sort === 'recent' ? 'series' : workspace.sort === 'series' ? 'progress' : 'recent' }); return; }
    if (key.return) {
      if (workspace.nav === 'workflows' || workspace.nav === 'advanced') { const item = filteredItems[workspace.itemIndex]; if (item) openForm(item); }
      else if (workspace.nav === 'volumes') { const item = volumeItems[workspace.itemIndex]; if (item) dispatch({ type: 'activeVolume', id: item.id }); }
    }
  });

  const dashboard = <Box flexDirection="column"><RuntimeConfigPanel lines={runtimeConfig} offset={workspace.configOffset} rows={rows} /><DeepSeekPricingPanel /><DeveloperPanel dryRunDefault={devDryRunDefault} /></Box>;
  const main = workspace.form ? <FormPanel form={workspace.form} activeVolume={workspace.activeVolume} preflight={preflight} epubs={epubs} recentVolumes={recentVolumes} /> : workspace.nav === 'dashboard' ? dashboard : workspace.nav === 'configuration' ? <ConfigurationPanel fields={filteredConfigFields} cursor={workspace.itemIndex} rows={rows} edit={workspace.configEdit} status={workspace.configStatus} query={workspace.search} /> : workspace.nav === 'workflows' || workspace.nav === 'advanced' ? <CapabilityList items={contentItems} index={workspace.itemIndex} query={workspace.search} /> : workspace.nav === 'volumes' ? <Box flexDirection="column"><Text bold>Volumes · sort {workspace.sort}</Text>{volumeItems.map((volume, index) => <Text key={volume.id} inverse={workspace.itemIndex === index} color={workspace.activeVolume === volume.id ? 'green' : 'white'}>{' '}{volume.title} ({volume.translatedCount}/{volume.chapterCount}){' '}</Text>) || <Text color="yellow">No manifests in work/ yet.</Text>}</Box> : workspace.nav === 'console' ? <ConsolePanel run={workspace.run} rows={rows} focused={workspace.terminalFocused} cancelConfirm={workspace.cancelConfirm} /> : <Box flexDirection="column"><Text bold>Diagnostics</Text><Text>Python: {preflight.python} ({preflight.pythonStatus})</Text><Text>Imports: {preflight.importsStatus} · MCP: {preflight.mcpStatus} · API key: {preflight.apiKeyPresent ? 'present' : 'missing'}</Text><Text color={preflight.importsStatus === 'ready' ? 'green' : 'yellow'}>{preflight.detail}</Text>{preflight.importsStatus !== 'ready' && <Text color="cyan">Repair: {preflight.repairCommand}</Text>}</Box>;
  const inspector = <Inspector volume={activeVolume} />;
  const footer = footerText(workspace);
  return <Box flexDirection="column" height={rows} width={columns} paddingX={1} overflow="hidden"><Header activeVolume={workspace.activeVolume} preflight={preflight} columns={columns} compact={maximized} /><Box flexGrow={1} marginTop={1} flexDirection={layout === 'single-pane' ? 'column' : 'row'}>{layout !== 'single-pane' && !maximized && <Navigation navIndex={workspace.navIndex} />}<Box flexDirection="column" flexGrow={1} marginLeft={layout === 'single-pane' || maximized ? 0 : 1}>{layout === 'single-pane' && !maximized && <Text color="gray">{NAV[workspace.navIndex]?.label ?? workspace.nav} › {workspace.form?.spec.label ?? 'workspace'}</Text>}{main}</Box>{!maximized && layout === 'three-pane' && <Box width={35} marginLeft={1}>{inspector}</Box>}{!maximized && layout === 'two-pane' && workspace.nav === 'volumes' && <Box width={35} marginLeft={1}>{inspector}</Box>}</Box><Text color="gray">{footer}</Text></Box>;
}
