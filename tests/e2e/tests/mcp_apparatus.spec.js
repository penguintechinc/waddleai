const { expect, test } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '../../..');
const MCP_KEY = process.env.WADDLEAI_MCP_E2E_KEY;
const MCP_URL = (process.env.WADDLEAI_MCP_E2E_URL || 'https://waddleai.penguintech.cloud').replace(/\/$/, '');

function rpc(id, method, params = {}) {
  return { jsonrpc: '2.0', id, method, params };
}

test.describe('MCP apparatus', () => {
  test('published OpenCode and Claude Code configurations point at the same MCP surface', () => {
    const opencode = JSON.parse(fs.readFileSync(path.join(ROOT, 'examples/opencode/opencode.json'), 'utf8'));
    const claudeDocs = fs.readFileSync(path.join(ROOT, 'docs/integrations/claude-code.md'), 'utf8');

    expect(opencode.mcp.waddleai.url).toMatch(/\/mcp$/);
    expect(claudeDocs).toContain('ANTHROPIC_BASE_URL');
    expect(claudeDocs).toContain('/mcp');
  });

  test('a beta MCP key initializes and lists tools through streamable HTTP', async ({ request }) => {
    test.skip(!MCP_KEY, 'Set WADDLEAI_MCP_E2E_KEY to run against beta.');
    const headers = {
      Authorization: `Bearer ${MCP_KEY}`,
      Accept: 'application/json, text/event-stream',
      'Content-Type': 'application/json',
    };
    const initialize = await request.post(`${MCP_URL}/mcp`, {
      headers,
      data: rpc(1, 'initialize', {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'waddleai-e2e', version: '1.0.0' },
      }),
    });
    expect(initialize.ok()).toBeTruthy();
    const sessionId = initialize.headers()['mcp-session-id'];
    expect(sessionId).toBeTruthy();

    const tools = await request.post(`${MCP_URL}/mcp`, {
      headers: { ...headers, 'Mcp-Session-Id': sessionId },
      data: rpc(2, 'tools/list'),
    });
    expect(tools.ok()).toBeTruthy();
    const body = await tools.text();
    expect(body).toContain('search_code');
    expect(body).toContain('usage_summary');
  });
});
