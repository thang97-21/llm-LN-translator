import assert from 'node:assert/strict';
import { closeMcpClient, listMcpTools } from '../src/core/mcpClient.js';
import { runPreflight } from '../src/core/preflight.js';

try {
  const preflight = runPreflight();
  assert.equal(preflight.importsStatus, 'ready', preflight.detail);
  const tools = await listMcpTools();
  assert.equal(tools.length, 20, `expected 20 MCP tools, received ${tools.length}`);
  console.log(`MCP handshake passed: ${tools.length} tools`);
} finally {
  await closeMcpClient();
}
