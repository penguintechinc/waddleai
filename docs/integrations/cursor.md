# Cursor + WaddleAI

Cursor speaks the OpenAI-compatible API, so it works against WaddleAI's
`/v1` surface the same way any OpenAI SDK client does.

## Setup

1. Open Cursor Settings → Models → **Add custom OpenAI-compatible provider**.
2. Base URL: `https://your-waddleai-host/v1`
3. API Key: your `wa-` virtual key.
4. Model list: Cursor calls `GET /v1/models` to populate the picker — every
   model your key is routed to (including `smart-router` for automatic
   selection) shows up there.

## MCP tools (optional, requires `waddleai.mcp_v2`)

The MCP config below reads the key from the environment:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

Cursor's MCP config (`~/.cursor/mcp.json` or the workspace-local
`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "waddleai": {
      "url": "https://your-waddleai-host/mcp",
      "headers": {
        "Authorization": "Bearer <your-waddleai-key>"
      }
    }
  }
}
```

## Quirks to know about

- **Streaming**: Cursor expects OpenAI-shaped SSE chunks (`data: {...}\n\n`,
  terminated by `data: [DONE]`); WaddleAI's `/v1/chat/completions` matches
  this exactly, no special config needed.
- **`smart-router` as a model name**: if you select WaddleAI's automatic
  router as the model, WaddleAI's routing engine picks the underlying
  provider per request — Cursor won't show which model actually answered
  unless you check the WaddleAI usage dashboard or query `usage_summary` via
  MCP.
- **Reasoning/thinking models**: Cursor's UI doesn't render extended-thinking
  blocks from `/v1/chat/completions` (that's an Anthropic-native concept
  surfaced via `/v1/messages`, not the OpenAI-compatible route) — use
  [Claude Code](claude-code.md) if you need visible thinking traces.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Model list empty | Key has no model assignments, or wrong base URL (missing `/v1`) | Check `GET /v1/models` manually with `curl`; verify base URL includes `/v1` |
| `429` errors | Org quota exceeded | Check usage in the WebUI or via `usage_summary` MCP tool |
| MCP tools not listed | `waddleai.mcp_v2` off, or malformed `mcp.json` | Validate JSON; confirm the flag is enabled for your org |

## See also

- [OpenCode](opencode.md) (default apparatus)
- [Claude Code](claude-code.md)
- [Antigravity](antigravity.md)
- [Generic OpenAI-compatible clients](generic-openai.md)
