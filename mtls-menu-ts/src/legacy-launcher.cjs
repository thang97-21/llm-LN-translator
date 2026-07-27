const { existsSync } = require('node:fs');
const { join, resolve } = require('node:path');
const { spawnSync } = require('node:child_process');

const packageRoot = resolve(__dirname, '..');
const pipelineRoot = resolve(packageRoot, '..');
const venvPython = join(pipelineRoot, 'venv', 'Scripts', 'python.exe');
const python = existsSync(venvPython) ? venvPython : 'python';
const script = join(pipelineRoot, 'scripts', 'mtl.py');

const result = spawnSync(python, [script], {
  cwd: pipelineRoot,
  stdio: 'inherit',
  env: process.env,
});

process.exit(result.status ?? 0);
