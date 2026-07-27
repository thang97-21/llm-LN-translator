import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';
import { pipelineRoot, pythonCommand } from './mtls.js';
import type { Preflight } from './types.js';

const REQUIRED_IMPORTS = 'lxml,bs4,PIL,yaml,anthropic,mcp';

function envHasDeepSeekKey(): boolean {
  if (Boolean(process.env.DEEPSEEK_API_KEY)) return true;
  const envPath = path.join(pipelineRoot, '.env');
  if (!existsSync(envPath)) return false;
  try { return /^\s*DEEPSEEK_API_KEY\s*=\s*[^\s#]/m.test(readFileSync(envPath, 'utf8')); } catch { return false; }
}

export function runPreflight(): Preflight {
  const python = pythonCommand();
  const repairCommand = `${python} -m pip install -r requirements.txt`;
  const version = spawnSync(python, ['--version'], { cwd: pipelineRoot, encoding: 'utf8', windowsHide: true });
  if (version.status !== 0) return { python, pythonStatus: 'missing', importsStatus: 'missing', mcpStatus: 'missing', apiKeyPresent: envHasDeepSeekKey(), repairCommand, detail: `Python interpreter could not start: ${python}` };
  const imports = spawnSync(python, ['-c', `import ${REQUIRED_IMPORTS}`], { cwd: pipelineRoot, encoding: 'utf8', windowsHide: true });
  if (imports.status !== 0) return { python, pythonStatus: 'ready', importsStatus: 'missing', mcpStatus: 'missing', apiKeyPresent: envHasDeepSeekKey(), repairCommand, detail: 'Required Python imports are unavailable. Install requirements before using MCP tools.' };
  return { python, pythonStatus: 'ready', importsStatus: 'ready', mcpStatus: 'checking', apiKeyPresent: envHasDeepSeekKey(), repairCommand, detail: 'Python dependencies are ready; MCP handshake is pending.' };
}
