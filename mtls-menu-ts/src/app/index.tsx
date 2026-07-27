import { spawnSync } from 'node:child_process';
import { render } from 'ink';
import { App } from '../ui/App.js';
import { mtlScript, pipelineRoot, pythonCommand } from '../core/mtls.js';
import { closeMcpClient } from '../core/mcpClient.js';

// Alternate screen buffer: gives the TUI its own full-height canvas so Ink
// repaints in place instead of stacking frames into scrollback. Restored on exit.
const ENTER_ALT_SCREEN = '\x1b[?1049h';
const LEAVE_ALT_SCREEN = '\x1b[?1049l';

let inkApp: ReturnType<typeof render> | null = null;
let altScreenActive = false;

function enterAltScreen(): void {
  if (!altScreenActive && process.stdout.isTTY) {
    process.stdout.write(ENTER_ALT_SCREEN);
    altScreenActive = true;
  }
}

function leaveAltScreen(): void {
  if (altScreenActive) {
    process.stdout.write(LEAVE_ALT_SCREEN);
    altScreenActive = false;
  }
}

function launchLegacy(): void {
  inkApp?.unmount();
  leaveAltScreen();
  void closeMcpClient().finally(() => {
    const result = spawnSync(pythonCommand(), [mtlScript], {
      cwd: pipelineRoot,
      stdio: 'inherit',
      env: process.env,
    });
    process.exit(result.status ?? 0);
  });
}

const transport = process.argv.includes('--mcp') ? 'mcp' : 'subprocess';

if (process.argv.includes('--legacy')) {
  launchLegacy();
} else {
  enterAltScreen();
  process.on('exit', leaveAltScreen);
  inkApp = render(<App onRequestLegacy={launchLegacy} transport={transport} />);
  // If a --mcp session spawned `python -m src.mcp.server` as a child
  // process, closeMcpClient() must run before the parent exits or that
  // child lingers as an orphaned process.
  void inkApp.waitUntilExit().finally(() => closeMcpClient().finally(leaveAltScreen));
}
