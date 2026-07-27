# DeepSeek_MTLS Operator Console

The Ink console is the keyboard-first operator surface for the standalone
DeepSeek pipeline. Python and MCP remain authoritative; this package supplies
typed forms, preview/confirmation, local status views, and retained execution
logs. A terminal menu that invents pipeline behavior would be worse than no
menu, obviously.

```powershell
npm install
npm run typecheck
npm test
npm start
```

`mtl-ts.bat` starts this console. `mtl.bat` remains the standalone Python CLI.
There is no legacy launcher or session-wide transport switch: each capability
owns its route.

## Console layout

- 120+ columns: navigation rail, workspace, and inspector.
- 90–119: navigation rail plus workspace; volume inspection drills into the
  second pane.
- Under 90: one workspace pane with breadcrumbs.

The navigation rail contains Dashboard, Workflows, Volumes, Inputs, Advanced
Toolbox, Console, and Diagnostics. Dashboard and Volumes replace the old
`list`/`status` commands with direct read-only filesystem views.

## Routed capabilities

The primary workflows use the canonical CLI and expose every actual option:

| Workflow | Exact CLI fields |
|---|---|
| Extract | EPUB, `--volume-id` |
| Prep | volume, `--series-id` |
| Translate | volume, `--chapters` multi-select |
| QC | volume |
| Build | volume, `--output` |
| Full Pipeline | EPUB, `--volume-id`, `--series-id` |

Advanced Toolbox gets its input shape from MCP `listTools()` and overlays the
operator-only metadata the schema cannot know: grouping, form widget, risk, and
semantic validation. It covers all 20 current tools. A missing known tool stays
visible but unavailable; an unknown tool is never made executable merely because
it appeared in a handshake.

Before a run, the console shows the exact CLI command or MCP JSON payload.
Read-only calls still require review; writes and paid calls require confirmation;
image optimization and bypass flags such as `apply_manifest` or `skip_qc` get a
second acknowledgement. No free-form extra-flags escape hatch exists.

## Preflight and diagnostics

Startup checks the selected Python interpreter, required imports, MCP handshake,
and whether a DeepSeek key is present without displaying it. If the default
interpreter lacks `lxml` or another requirement, Advanced Toolbox remains
disabled and Diagnostics prints the exact repair command:

```powershell
python -m pip install -r requirements.txt
```

## Terminal focus

The Console keeps 5,000 structured log entries (time, source, severity, stage),
even after a run completes. `Tab` enters/leaves terminal focus. In focus:

- `Up`/`Down`, `PageUp`/`PageDown`, and `Home`/`End` browse retained output.
- `f` resumes follow-tail and clears the unseen counter.
- `i` forwards raw input to a running CLI child; MCP calls deliberately have no
  raw-input mode.
- `Esc` leaves input mode for browse; during a run it asks before cancellation.
- `Ctrl+C` stops the action but keeps the console open. A second idle `Ctrl+C`
  exits.

The console uses real child termination for CLI actions and an `AbortController`
that closes an in-flight MCP transport. Both routes clean up on normal exit.
