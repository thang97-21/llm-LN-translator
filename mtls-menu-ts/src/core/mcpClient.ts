import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import type { RunHandle, RunState } from './types.js';
import type { CommandSpec } from './types.js';
import { pipelineRoot, pythonCommand } from './mtls.js';

// `--mcp` transport: instead of spawning `python scripts/mtl.py <argv>` and
// parsing stdout line-by-line (the default transport, see runMtlCommand in
// mtls.ts), this connects a real MCP client over stdio to
// `python -m src.mcp.server` and calls tools directly. Same underlying
// Python code either way — src.mcp.server's tools ARE scripts/mtl.py's
// commands, just addressed as typed RPC calls instead of a CLI subprocess.

let clientPromise: Promise<Client> | null = null;

function getClient(): Promise<Client> {
  if (!clientPromise) {
    clientPromise = (async () => {
      const transport = new StdioClientTransport({
        command: pythonCommand(),
        args: ['-m', 'src.mcp.server'],
        cwd: pipelineRoot,
        env: process.env as Record<string, string>,
      });
      const client = new Client({ name: 'deepseek-mtls-tui', version: '1.0.0' }, { capabilities: {} });
      await client.connect(transport);
      return client;
    })();
  }
  return clientPromise;
}

// Call sites that fail should not poison future calls with a broken
// connection — drop the cached client so the next call reconnects fresh.
async function withClient<T>(fn: (client: Client) => Promise<T>): Promise<T> {
  const client = await getClient();
  try {
    return await fn(client);
  } catch (error) {
    clientPromise = null;
    throw error;
  }
}

export async function closeMcpClient(): Promise<void> {
  if (!clientPromise) {
    return;
  }
  const client = await clientPromise;
  clientPromise = null;
  await client.close();
}

type ToolResult = {
  ok: boolean;
  structured: Record<string, unknown> | null;
  text: string;
};

async function callTool(name: string, args: Record<string, unknown>): Promise<ToolResult> {
  return withClient(async (client) => {
    const result = await client.callTool({ name, arguments: args });
    const content = Array.isArray((result as { content?: unknown[] }).content)
      ? ((result as { content: Array<{ type?: string; text?: string }> }).content)
      : [];
    const text = content
      .filter((block) => block.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text as string)
      .join('\n');
    const structured = (result as { structuredContent?: Record<string, unknown> }).structuredContent ?? null;
    const isError = Boolean((result as { isError?: boolean }).isError);
    return { ok: !isError, structured, text };
  });
}

function formatResult(result: ToolResult): string {
  if (result.structured) {
    return JSON.stringify(result.structured, null, 2);
  }
  return result.text || '(empty response)';
}

function resultVolumeId(result: ToolResult): string {
  const id = result.structured?.volume_id;
  return typeof id === 'string' ? id : '';
}

// Which selected value (volume_id or epub_path) each command's tool call
// expects, and which argument name it goes under. `run` is handled
// separately below since it chains 5 tool calls, not 1.
const SINGLE_TOOL_ROUTES: Partial<Record<string, { tool: string; argName: string }>> = {
  extract: { tool: 'extract_epub', argName: 'epub_path' },
  prep: { tool: 'prep_volume', argName: 'volume_id' },
  translate: { tool: 'run_translator', argName: 'volume_id' },
  qc: { tool: 'qc_volume', argName: 'volume_id' },
  build: { tool: 'package_epub', argName: 'volume_id' },
};

// `list` and `status` are local filesystem reads with no MCP tool behind
// them (there's nothing to call remotely — they already run without
// spawning anything). `run` is handled as a 5-call chain, not a single
// route. Everything else needs a real entry in SINGLE_TOOL_ROUTES.
export function hasMcpRoute(commandId: string): boolean {
  return commandId === 'run' || commandId in SINGLE_TOOL_ROUTES;
}

export function runMtlCommandMcp(
  command: CommandSpec,
  argv: string[],
  selectedValue: string | null,
  onUpdate: (state: RunState) => void,
): RunHandle {
  let cancelled = false;
  const state: RunState = {
    command,
    argv,
    status: 'running',
    exitCode: null,
    lines: [`$ (mcp) ${command.id}${selectedValue ? ` ${selectedValue}` : ''}`],
  };

  const emit = (): void => {
    if (!cancelled) {
      onUpdate({ ...state, lines: [...state.lines] });
    }
  };
  const push = (line: string): void => {
    for (const part of line.split('\n')) {
      state.lines.push(part);
    }
    state.lines = state.lines.slice(-200);
    emit();
  };

  (async () => {
    try {
      if (command.id === 'run') {
        push('extract_epub...');
        const extracted = await callTool('extract_epub', { epub_path: selectedValue ?? '' });
        push(formatResult(extracted));
        if (!extracted.ok) {
          throw new Error('extract_epub failed');
        }
        const volumeId = resultVolumeId(extracted);
        if (!volumeId) {
          throw new Error('extract_epub did not return a volume_id — cannot continue the chained run');
        }
        push(`-> volume_id=${volumeId}`);

        for (const [label, tool] of [
          ['prep_volume', 'prep_volume'],
          ['run_translator', 'run_translator'],
          ['qc_volume', 'qc_volume'],
          ['package_epub', 'package_epub'],
        ] as const) {
          if (cancelled) return;
          push(`${label}...`);
          const result = await callTool(tool, { volume_id: volumeId });
          push(formatResult(result));
        }

        state.status = 'done';
        state.exitCode = 0;
        emit();
        return;
      }

      const route = SINGLE_TOOL_ROUTES[command.id];
      if (!route) {
        throw new Error(`No MCP tool mapping for command "${command.id}" — this command is local-only or unsupported in --mcp mode.`);
      }
      const result = await callTool(route.tool, { [route.argName]: selectedValue ?? '' });
      push(formatResult(result));
      state.status = result.ok ? 'done' : 'failed';
      state.exitCode = result.ok ? 0 : 1;
      emit();
    } catch (error) {
      state.status = 'failed';
      state.exitCode = 1;
      push(`MCP error: ${error instanceof Error ? error.message : String(error)}`);
      emit();
    }
  })();

  return {
    cancel: () => {
      cancelled = true;
    },
    write: () => {
      // MCP tool calls are single-shot request/response — there is no stdin
      // channel to write interactive input into, unlike the subprocess
      // transport's child.stdin.
    },
  };
}

