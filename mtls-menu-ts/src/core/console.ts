import type { ConsoleEntry, ConsoleMode, ConsoleSeverity, ConsoleSource, ConsoleState } from './types.js';

export const CONSOLE_LIMIT = 5_000;

export function createConsole(): ConsoleState { return { entries: [], mode: 'follow', offset: 0, unseen: 0 }; }

export function appendConsole(
  state: ConsoleState,
  text: string,
  source: ConsoleSource,
  severity: ConsoleSeverity = 'info',
  stage = 'run',
): ConsoleState {
  const next = [...state.entries];
  for (const line of text.replace(/\r/g, '').split('\n')) {
    if (!line) continue;
    next.push({ id: next.length ? next[next.length - 1]!.id + 1 : 1, timestamp: Date.now(), source, severity, stage, text: line });
  }
  const evicted = Math.max(0, next.length - CONSOLE_LIMIT);
  const entries = evicted ? next.slice(evicted) : next;
  if (state.mode === 'follow') return { entries, mode: 'follow', offset: 0, unseen: 0 };
  return { entries, mode: state.mode, offset: Math.max(0, state.offset - evicted), unseen: state.unseen + Math.max(0, next.length - state.entries.length) };
}

export function browseConsole(state: ConsoleState, delta: number, viewport: number): ConsoleState {
  const max = Math.max(0, state.entries.length - Math.max(1, viewport));
  const offset = Math.max(0, Math.min(max, state.offset + delta));
  return { ...state, mode: 'browse', offset, unseen: offset === max ? 0 : state.unseen };
}

export function jumpConsole(state: ConsoleState, destination: 'home' | 'end', viewport: number): ConsoleState {
  if (destination === 'end') return { ...state, mode: 'follow', offset: 0, unseen: 0 };
  return { ...state, mode: 'browse', offset: Math.max(0, state.entries.length - Math.max(1, viewport)), unseen: state.unseen };
}

export function setConsoleMode(state: ConsoleState, mode: ConsoleMode): ConsoleState {
  return mode === 'follow' ? { ...state, mode, offset: 0, unseen: 0 } : { ...state, mode };
}

export function visibleConsoleEntries(state: ConsoleState, viewport: number): readonly ConsoleEntry[] {
  const end = Math.max(0, state.entries.length - state.offset);
  return state.entries.slice(Math.max(0, end - Math.max(1, viewport)), end);
}
