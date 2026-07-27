import { render } from 'ink';
import { App } from '../ui/App.js';
import { closeMcpClient } from '../core/mcpClient.js';

const ENTER_ALT_SCREEN = '\x1b[?1049h';
const LEAVE_ALT_SCREEN = '\x1b[?1049l';
let alternateScreen = false;

function enterAlternateScreen(): void { if (process.stdout.isTTY && !alternateScreen) { process.stdout.write(ENTER_ALT_SCREEN); alternateScreen = true; } }
function leaveAlternateScreen(): void { if (alternateScreen) { process.stdout.write(LEAVE_ALT_SCREEN); alternateScreen = false; } }

enterAlternateScreen();
process.on('exit', leaveAlternateScreen);
const app = render(<App />);
void app.waitUntilExit().finally(() => closeMcpClient().finally(leaveAlternateScreen));
