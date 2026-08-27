import type { ConsoleSeverity, FormValues, RunState, SortMode, VolumeSummary } from '../core/types.js';
import { appendConsole, browseConsole, jumpConsole, setConsoleMode } from '../core/console.js';
import type { CapabilitySpec } from '../core/types.js';

// The workspace state machine, extracted from App.tsx so the reducer's
// transitions can be tested without mounting a single Ink component. The
// discriminated-union Action type IS the state machine: the compiler's
// exhaustiveness check on the switch below rejects any transition that
// forgets a case, and the final `never` guard (in the form of the switch
// having no default) means adding an Action variant without handling it
// here is a compile error, not a runtime shrug.

export type Nav = 'dashboard' | 'workflows' | 'volumes' | 'advanced' | 'configuration' | 'console' | 'diagnostics';
export type FormState = { spec: CapabilitySpec; values: FormValues; cursor: number; confirmation: 0 | 1 | 2; issues: readonly string[] };
// Identified by the field's dot-path, not its position in whatever list is
// currently visible — a save reloads configFields from disk mid-session,
// and a search query could in principle reorder that list, so an index
// would silently point at the wrong row.
export type ConfigEditState = { fieldPath: string; buffer: string; error: string | null } | null;
export type ConfigStatus = { fieldPath: string; message: string; ok: boolean } | null;
export type Workspace = { nav: Nav; navIndex: number; itemIndex: number; configOffset: number; activeVolume: string | null; search: string; searching: boolean; form: FormState | null; run: RunState | null; terminalFocused: boolean; cancelConfirm: boolean; sort: SortMode; configEdit: ConfigEditState; configStatus: ConfigStatus };
export type Action =
  | { type: 'nav'; nav: Nav; navIndex: number }
  | { type: 'navCursor'; navIndex: number }
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
  | { type: 'configScroll'; delta: number; viewport: number; destination?: 'home' | 'end' }
  | { type: 'terminalFocus'; value: boolean }
  | { type: 'cancelConfirm'; value: boolean }
  | { type: 'sort'; value: SortMode }
  | { type: 'configEdit'; edit: ConfigEditState }
  | { type: 'configStatus'; status: ConfigStatus };

export const NAV: readonly { id: Nav; label: string }[] = [
  { id: 'dashboard', label: 'Dashboard' }, { id: 'workflows', label: 'Workflows' }, { id: 'volumes', label: 'Volumes' }, { id: 'advanced', label: 'Advanced Toolbox' }, { id: 'configuration', label: 'Configuration' }, { id: 'console', label: 'Console' }, { id: 'diagnostics', label: 'Diagnostics' },
];

export function reducer(state: Workspace, action: Action): Workspace {
  switch (action.type) {
    case 'nav': return { ...state, nav: action.nav, navIndex: action.navIndex, itemIndex: 0, configOffset: 0, search: '', searching: false, cancelConfirm: false, configEdit: null, configStatus: null };
    case 'navCursor': return { ...state, navIndex: action.navIndex };
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
    case 'configScroll': return { ...state, configOffset: action.destination === 'home' ? 0 : action.destination === 'end' ? Number.MAX_SAFE_INTEGER : Math.max(0, state.configOffset + action.delta) };
    case 'terminalFocus': return { ...state, terminalFocused: action.value };
    case 'cancelConfirm': return { ...state, cancelConfirm: action.value };
    case 'sort': return { ...state, sort: action.value, itemIndex: 0 };
    case 'configEdit': return { ...state, configEdit: action.edit };
    case 'configStatus': return { ...state, configStatus: action.status };
  }
}

export function initialWorkspace(volumes: readonly VolumeSummary[]): Workspace { return { nav: 'dashboard', navIndex: 0, itemIndex: 0, configOffset: 0, activeVolume: volumes[0]?.id ?? null, search: '', searching: false, form: null, run: null, terminalFocused: false, cancelConfirm: false, sort: 'recent', configEdit: null, configStatus: null }; }
