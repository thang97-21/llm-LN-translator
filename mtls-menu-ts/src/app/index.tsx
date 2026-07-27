import { render } from 'ink';
import { App } from '../ui/App.js';
import { closeMcpClient } from '../core/mcpClient.js';

const ENTER_ALT_SCREEN = '\x1b[?1049h';
const LEAVE_ALT_SCREEN = '\x1b[?1049l';
// SGR mouse tracking (1000 = report button/wheel events, 1006 = extended
// coordinate encoding so it doesn't break past column/row 223). Without
// this the terminal never sends wheel events at all — see useMouseScroll.ts.
const ENABLE_MOUSE = '\x1b[?1000h\x1b[?1006h';
const DISABLE_MOUSE = '\x1b[?1006l\x1b[?1000l';
let alternateScreen = false;
let mouseTracking = false;

function enterAlternateScreen(): void { if (process.stdout.isTTY && !alternateScreen) { process.stdout.write(ENTER_ALT_SCREEN); alternateScreen = true; } }
function leaveAlternateScreen(): void { if (alternateScreen) { process.stdout.write(LEAVE_ALT_SCREEN); alternateScreen = false; } }
function enableMouseTracking(): void { if (process.stdout.isTTY && !mouseTracking) { process.stdout.write(ENABLE_MOUSE); mouseTracking = true; } }
function disableMouseTracking(): void { if (mouseTracking) { process.stdout.write(DISABLE_MOUSE); mouseTracking = false; } }

enterAlternateScreen();
enableMouseTracking();
process.on('exit', () => { disableMouseTracking(); leaveAlternateScreen(); });
const app = render(<App />);
void app.waitUntilExit().finally(() => closeMcpClient().finally(() => { disableMouseTracking(); leaveAlternateScreen(); }));
