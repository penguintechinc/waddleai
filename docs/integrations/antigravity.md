# Antigravity + WaddleAI

Antigravity supports OpenAI-compatible providers and MCP the same way
Cursor does. If you've configured [Cursor](cursor.md) against WaddleAI
already, the same values apply here.

## Setup

1. In Antigravity's model provider settings, add a custom OpenAI-compatible
   endpoint:
   - Base URL: `https://your-waddleai-host/v1`
   - API Key: your `wa-` virtual key
2. Antigravity fetches the model list from `GET /v1/models` — no manual
   model registration needed.

## MCP tools (optional, requires `waddleai.mcp_v2`)

Add a remote MCP server pointed at `https://your-waddleai-host/mcp` with an
`Authorization: Bearer wa-your-key-here` header, using whichever MCP-config
mechanism your Antigravity version exposes (check its docs for the exact
config file location — this has moved between releases).

## Quirks to know about

- Antigravity's own agent loop may issue tool calls that overlap in name
  with WaddleAI's native MCP tools (e.g. its own `search` tool vs.
  WaddleAI's `search_code`) — WaddleAI's tools are not namespaced under a
  prefix for the native surface (only gateway-aggregated external tools like
  `elder.*` are), so pick distinguishable tool descriptions if you see
  collisions in Antigravity's tool picker.
- Same `smart-router` caveat as Cursor: automatic routing means the model
  that actually answered isn't visible in Antigravity's UI; check
  `usage_summary` via MCP or the WebUI usage dashboard.

## Troubleshooting

Same failure modes and fixes as [Cursor](cursor.md) — the two clients hit
identical WaddleAI surfaces.

## See also

- [OpenCode](opencode.md) (default apparatus)
- [Claude Code](claude-code.md)
- [Cursor](cursor.md)
- [Generic OpenAI-compatible clients](generic-openai.md)
