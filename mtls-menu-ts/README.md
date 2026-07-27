# MTLS TypeScript Menu

Experimental TypeScript 7 / Ink operator shell for MTL Studio.

This is intentionally separate from the legacy Python TUI:

- Legacy TUI: `pipeline/mtl.bat` or `python scripts/mtl.py`
- TypeScript shell: `pipeline/mtl-ts.bat` or `npm start` from this directory

The shell delegates pipeline work to the canonical Python entrypoint at
`pipeline/scripts/mtl.py`. It reads `WORK/*/manifest.json` for dashboard and
volume context, but it does not mutate manifests directly.

## Commands

```bash
npm install
npm run typecheck        # tsc -b: build the project-reference graph (TS7 --builders)
npm run typecheck:watch  # native TS7 watch over the whole graph
npm run clean            # tsc -b --clean: drop dist/ declaration cache
npm start
npm run legacy
```

## Workspace layout

Split into three composite TypeScript projects along the dependency graph, so
TS7's `tsc -b` builds them in parallel and incrementally:

```
tsconfig.json        solution file (files: [], references core/ui/app)
tsconfig.base.json   shared strict compilerOptions
src/core/            types + Node/FS/process logic   (no React)   → declares dist/core
src/ui/              Ink components (App.tsx)          refs core   → declares dist/ui
src/app/             entry point (index.tsx)           refs core+ui
src/legacy-launcher.cjs   standalone CJS launcher (not part of the graph)
```

Projects are `composite` + `emitDeclarationOnly`: `tsc -b` emits only `.d.ts`
into `dist/` (gitignored) for cross-project checking — `tsx` runs the TS source
directly, so no compiled JS is ever needed.

## TypeScript 7 usage

TS7 is the native (Go) compiler — its value here is fast `tsc`/LSP and stricter
defaults, not new syntax. This package leans on that in three ways:

- **Toolchain:** `typecheck` runs `tsc -b` over the reference graph (native
  `tsgo`, parallel `--builders`, incremental via `dist/*/.tsbuildinfo`).
  `tsconfig.base.json` targets TS7-era strictness: `noUncheckedIndexedAccess`,
  `exactOptionalPropertyTypes`, `verbatimModuleSyntax`, `isolatedModules`,
  `noUnusedLocals`/`noUnusedParameters`, `noFallthroughCasesInSwitch`,
  `noImplicitOverride`.
  (`noPropertyAccessFromIndexSignature` is intentionally off — it fights the
  `asRecord` dynamic-JSON boundary in `core/mtls.ts` with no added safety.)
- **Type system:** the menu is fully typed end to end. `MenuList<T>` is generic
  (no `unknown` erasure, no `as` casts at call sites); `CommandSpec` is a
  discriminated union dispatched exhaustively via `assertNever`; `commands` is
  declared `as const satisfies readonly CommandSpec[]`; raw manifest status
  strings are collapsed to a closed `PhaseStatusValue` union at the parse
  boundary and colored through an exhaustive `Record`.
- **Runtime:** `start`/`dev` pass `--tsconfig ./tsconfig.base.json` to `tsx`.
  The root `tsconfig.json` is a solution file (`files: []`), which `tsx` won't
  read `jsx` settings from — the explicit flag pins the automatic JSX runtime.

## Features

- **Dashboard & rows** — block-char progress bars, a compact per-phase glyph
  strip (`1✓ 1.5✓ 1.55· 2◐ 4✗`), an inverse-video selection bar, one-line
  CJK-safe title truncation, and a `·JP` badge on volumes whose metadata isn't
  translated yet. List/log windows size to the live terminal height.
- **Search & navigation** — `/` fuzzy-filters the current list; `s` cycles the
  volume sort (recent / series / progress); `g`/`G` jump to ends. The Volume
  Workbench is a two-pane view: list on the left, a detail panel on the right.
- **Detail panel (read-only artifact mining)** — for the highlighted volume,
  `core/loadVolumeDetail` reads `translation_log.json` (token totals, ok/fail
  counts, AI-ism totals, last error) and counts `JP/`, `EN/` (with word count),
  and `QC/` files. No `cost_audit.json` exists on disk, so no USD is shown.
- **Launch & safety** — commands with flags (e.g. `phase2` → `--force`,
  `--batch`) open a launch screen: toggle flags (Space), preview the exact
  `python mtl.py …` command, and confirm. High-risk commands (`phase2`) require
  a second Enter to confirm before spawning.
- **Live run panel** — `ink-spinner` while running, an elapsed timer, and
  content-colored stdout/stderr. Leaving or exiting the run screen kills the
  child process.

## Notes

The MTLS pipeline remains Python-owned; this package is only a richer terminal
operator surface. It reads `WORK/*/manifest.json` (and, on demand, per-volume
artifacts) and never mutates them.
