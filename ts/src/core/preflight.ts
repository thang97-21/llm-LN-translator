import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';
import { pipelineRoot, pythonCommand } from './mtls.js';
import type { Preflight } from './types.js';

const BASE_IMPORTS = 'lxml,bs4,PIL,yaml,mcp';
const DEFAULT_API_KEY_ENVS: Readonly<Record<string, string>> = {
  deepseek: 'DEEPSEEK_API_KEY',
  qwen: 'DASHSCOPE_API_KEY',
  openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  glm: 'ZAI_API_KEY',
};

export type PreflightRequirements = {
  readonly provider: string;
  readonly apiKeyEnv: string;
  readonly imports: string;
};

export function resolvePreflightRequirements(configText: string): PreflightRequirements {
  // Scope to the translation block: prep.provider (the prep-model route)
  // sits at the same 2-space indent earlier in the file and would otherwise
  // shadow this match, flipping the detected provider and API key.
  const translationSection = configText.slice(Math.max(0, configText.indexOf('\ntranslation:')));
  const provider = (/^\s{2}provider:\s*(deepseek|qwen|openai|anthropic|glm)\s*(?:#.*)?$/m.exec(translationSection)?.[1] ?? 'deepseek').toLowerCase();
  const apiKeyEnv = providerApiKeyEnv(configText, provider) ?? DEFAULT_API_KEY_ENVS[provider] ?? 'DEEPSEEK_API_KEY';
  return {
    provider,
    apiKeyEnv,
    // deepseek/qwen speak an Anthropic-*compatible* endpoint and anthropic
    // speaks the real Anthropic Messages API — both need the same SDK import.
    imports: `${BASE_IMPORTS},${provider === 'openai' || provider === 'glm' ? 'openai' : 'anthropic'}`,
  };
}

function providerApiKeyEnv(configText: string, provider: string): string | null {
  const blockStart = new RegExp(`^\\s{2}${provider.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:\\s*(?:#.*)?$`);
  let inBlock = false;
  for (const line of configText.replace(/\r/g, '').split('\n')) {
    if (blockStart.test(line)) { inBlock = true; continue; }
    if (!inBlock) continue;
    if (/^\s{2}[A-Za-z_][^:]*:\s*/.test(line)) break;
    const match = /^\s{4}api_key_env:\s*([^#\s]+)/.exec(line);
    if (match?.[1]) return match[1].replace(/^['"]|['"]$/g, '');
  }
  return null;
}



function envHasApiKey(apiKeyEnv: string): boolean {
  if (Boolean(process.env[apiKeyEnv])) return true;
  const envPath = path.join(pipelineRoot, '.env');
  if (!existsSync(envPath)) return false;
  try { return new RegExp(`^\\s*${apiKeyEnv.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*=\\s*[^\\s#]`, 'm').test(readFileSync(envPath, 'utf8')); } catch { return false; }
}

export function runPreflight(): Preflight {
  const configPath = path.join(pipelineRoot, 'config.yaml');
  const configText = existsSync(configPath) ? readFileSync(configPath, 'utf8') : '';
  const requirements = resolvePreflightRequirements(configText);
  const apiKeyPresent = envHasApiKey(requirements.apiKeyEnv);
  const python = pythonCommand();
  const repairCommand = `${python} -m pip install -r requirements.txt`;
  const version = spawnSync(python, ['--version'], { cwd: pipelineRoot, encoding: 'utf8', windowsHide: true });
  if (version.status !== 0) return { python, pythonStatus: 'missing', importsStatus: 'missing', mcpStatus: 'missing', apiKeyPresent, repairCommand, detail: `Python interpreter could not start: ${python}` };
  const imports = spawnSync(python, ['-c', `import ${requirements.imports}`], { cwd: pipelineRoot, encoding: 'utf8', windowsHide: true });
  if (imports.status !== 0) return { python, pythonStatus: 'ready', importsStatus: 'missing', mcpStatus: 'missing', apiKeyPresent, repairCommand, detail: `Required ${requirements.provider} provider imports are unavailable. Install requirements before using MCP tools.` };
  return { python, pythonStatus: 'ready', importsStatus: 'ready', mcpStatus: 'checking', apiKeyPresent, repairCommand, detail: `Python dependencies are ready for ${requirements.provider}; MCP handshake is pending.` };
}
