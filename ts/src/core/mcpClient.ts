import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import process from 'node:process';
import { pipelineRoot, pythonCommand } from './mtls.js';
import type { McpListedTool } from './capabilities.js';

let clientPromise: Promise<Client> | null = null;

async function getClient(): Promise<Client> {
  if (!clientPromise) {
    clientPromise = (async () => {
      const transport = new StdioClientTransport({ command: pythonCommand(), args: ['-m', 'src.Deepseek.mcp.server'], cwd: pipelineRoot, env: process.env as Record<string, string> });
      const client = new Client({ name: 'llm-translator-operator-console', version: '2.0.0' }, { capabilities: {} });
      await client.connect(transport);
      return client;
    })();
  }
  try { return await clientPromise; } catch (error) { clientPromise = null; throw error; }
}

export async function closeMcpClient(): Promise<void> {
  const pending = clientPromise;
  clientPromise = null;
  if (pending) { const client = await pending; await client.close(); }
}

export async function listMcpTools(signal?: AbortSignal): Promise<McpListedTool[]> {
  if (signal?.aborted) throw new Error('MCP handshake cancelled');
  const client = await getClient();
  const abort = (): void => { void closeMcpClient(); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    const result = await client.listTools();
    if (signal?.aborted) throw new Error('MCP handshake cancelled');
    return result.tools.map((tool) => ({ name: tool.name, ...(tool.description ? { description: tool.description } : {}), ...(tool.inputSchema ? { inputSchema: tool.inputSchema } : {}) }));
  } finally { signal?.removeEventListener('abort', abort); }
}

export type McpCallResult = { ok: boolean; text: string; structured: Record<string, unknown> | null };

export async function callMcpTool(name: string, payload: Record<string, unknown>, signal?: AbortSignal): Promise<McpCallResult> {
  if (signal?.aborted) throw new Error('MCP request cancelled');
  const client = await getClient();
  const abort = (): void => { void closeMcpClient(); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    const result = await client.callTool({ name, arguments: payload });
    if (signal?.aborted) throw new Error('MCP request cancelled');
    const raw = result as { content?: Array<{ type?: string; text?: string }>; structuredContent?: Record<string, unknown>; isError?: boolean };
    const text = (raw.content ?? []).filter((block) => block.type === 'text' && typeof block.text === 'string').map((block) => block.text ?? '').join('\n');
    return { ok: !raw.isError, text, structured: raw.structuredContent ?? null };
  } catch (error) {
    clientPromise = null;
    throw error;
  } finally { signal?.removeEventListener('abort', abort); }
}
