// Proofreading Mode (translation.anthropic.advisor.*) gate for the console.
//
// client.py::_validate_advisor_config already refuses a bad pairing — but only
// at the start of the NEXT paid run, long after the toggle said "Saved." This
// module applies the same rules at save time so the console can never write a
// config the backend is going to reject.

import { unquoteYamlScalar } from './configSchema.js';

// Mirror of ADVISOR_COMPATIBILITY in src/Anthropic/client.py (Anthropic's
// official executor → advisor table). tests/run.ts parses client.py and fails
// if the two drift, so update both together.
export const ADVISOR_COMPATIBILITY: Readonly<Record<string, readonly string[]>> = {
  'claude-sonnet-5': [
    'claude-mythos-5-1', 'claude-fable-5-1', 'claude-mythos-5', 'claude-fable-5',
    'claude-opus-5', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-5',
  ],
  'claude-opus-5': [
    'claude-mythos-5-1', 'claude-fable-5-1', 'claude-mythos-5', 'claude-fable-5',
    'claude-opus-5',
  ],
  'claude-fable-5-1': ['claude-mythos-5-1', 'claude-fable-5-1'],
};

// Explicit lock, independent of the table above: even if someone adds an
// Opus 5.5 row to the mirror by mistake, Proofreading Mode stays off with it
// until this line is deliberately removed.
export const PROOFREADING_LOCKED_EXECUTORS: ReadonlySet<string> = new Set(['claude-opus-5-5']);

export const PROOFREADING_PATHS = {
  enabled: 'translation.anthropic.advisor.enabled',
  advisor: 'translation.anthropic.advisor.model',
  executor: 'translation.anthropic.model',
  effort: 'translation.anthropic.thinking.effort',
} as const;

type FieldValue = { readonly path: string; readonly rawValue: string };

export type ProofreadingState = {
  readonly enabled: boolean;
  readonly executor: string;
  readonly advisor: string;
  readonly effort: string;
  readonly locked: boolean;
  readonly validAdvisors: readonly string[];
  /** Why Proofreading Mode cannot run with this config, or null if it can. */
  readonly blocker: string | null;
};

function read(fields: readonly FieldValue[], path: string): string {
  return unquoteYamlScalar(fields.find((field) => field.path === path)?.rawValue ?? '').trim();
}

export function proofreadingState(fields: readonly FieldValue[]): ProofreadingState {
  const executor = read(fields, PROOFREADING_PATHS.executor);
  const advisor = read(fields, PROOFREADING_PATHS.advisor);
  const effort = read(fields, PROOFREADING_PATHS.effort) || 'high';
  const enabled = read(fields, PROOFREADING_PATHS.enabled) === 'true';
  const locked = PROOFREADING_LOCKED_EXECUTORS.has(executor);
  const validAdvisors = locked ? [] : ADVISOR_COMPATIBILITY[executor] ?? [];
  let blocker: string | null = null;
  if (locked) blocker = `${executor} is locked out: Anthropic documents no advisor pairing for it.`;
  else if (!validAdvisors.length) blocker = `Anthropic documents no advisor pairing for executor ${executor || '(unset)'}.`;
  else if (!validAdvisors.includes(advisor)) blocker = `${advisor || '(unset)'} is not an official advisor for ${executor} (valid: ${validAdvisors.join(', ')}).`;
  // Same rule client.py enforces: below "high", escalation either never fires
  // or leaks a pre-consult fragment (Runs 5-7).
  else if (effort !== 'high') blocker = `requires Thinking Effort "high" (currently "${effort}").`;
  return { enabled, executor, advisor, effort, locked, validAdvisors, blocker };
}

export type ConfigWrite = { readonly path: string; readonly raw: string };
export type ConfigWritePlan =
  | { readonly ok: true; readonly writes: readonly ConfigWrite[]; readonly note: string | null }
  | { readonly ok: false; readonly error: string };

const GATED_PATHS: ReadonlySet<string> = new Set([PROOFREADING_PATHS.executor, PROOFREADING_PATHS.advisor, PROOFREADING_PATHS.effort]);

// Every console save goes through here. Two outcomes beyond a plain write:
//  - turning Proofreading Mode ON against a blocked config is refused outright;
//  - changing executor/advisor/effort while it is ON into a blocked config is
//    allowed, but switches it OFF in the same save. The disable is ordered
//    FIRST so a failed second write leaves the safe state on disk.
export function planConfigWrite(fields: readonly FieldValue[], path: string, raw: string): ConfigWritePlan {
  const write: ConfigWrite = { path, raw };
  const next = fields.map((field) => (field.path === path ? { ...field, rawValue: raw } : field));
  const after = proofreadingState(next);
  if (path === PROOFREADING_PATHS.enabled && raw === 'true' && after.blocker) {
    return { ok: false, error: `Proofreading Mode stays off: ${after.blocker}` };
  }
  if (GATED_PATHS.has(path) && proofreadingState(fields).enabled && after.blocker) {
    return { ok: true, writes: [{ path: PROOFREADING_PATHS.enabled, raw: 'false' }, write], note: `Saved. Proofreading Mode switched off: ${after.blocker}` };
  }
  return { ok: true, writes: [write], note: null };
}

export function planProofreadingToggle(fields: readonly FieldValue[]): ConfigWritePlan {
  return planConfigWrite(fields, PROOFREADING_PATHS.enabled, proofreadingState(fields).enabled ? 'false' : 'true');
}

// Advisor Model cycles only through the official pairings for the current
// executor, so Space never lands on a choice the gate would then refuse. A
// locked/unknown executor has no pairings — cycle everything; the gate keeps
// the mode itself off regardless.
export function advisorCycleChoices(fields: readonly FieldValue[], allChoices: readonly string[]): readonly string[] {
  const { validAdvisors } = proofreadingState(fields);
  const allowed = allChoices.filter((choice) => validAdvisors.includes(choice));
  return allowed.length ? allowed : allChoices;
}
