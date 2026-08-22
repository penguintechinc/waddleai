# Claude Code + WaddleAI

Claude Code speaks Anthropic's native `/v1/messages` API directly (not the
OpenAI-compatible shape), so pointing it at WaddleAI relies on the proxy's
`/v1/messages` fidelity work: streaming, `tool_use` blocks, system-prompt
arrays, `thinking` blocks, prompt-cache passthrough, and `/v1/messages/count_tokens`
all need to behave exactly like the real Anthropic API. That parity landed in
Phase 1 (`feature/aiproxy-migration`), so this integration works as soon as
your WaddleAI deployment is on that release — no MCP-v2 dependency.

## Setup

```bash
export ANTHROPIC_BASE_URL=https://your-waddleai-host
export ANTHROPIC_API_KEY=$WADDLEAI_API_KEY
```

Claude Code reads both at startup. No other config changes are needed —
WaddleAI's `/v1/messages` and `/v1/messages/count_tokens` routes are wired
to accept the same request/response shape Claude Code already sends and
expects.

## What routes through WaddleAI

| Claude Code capability | WaddleAI route |
|---|---|
| Chat turns (streaming and non-streaming) | `POST /v1/messages` |
| Token counting before a turn | `POST /v1/messages/count_tokens` |
| Tool use (`tool_use`/`tool_result` blocks) | Passed through `/v1/messages` unchanged |
| Extended thinking blocks | Passed through `/v1/messages` unchanged |
| Prompt caching (`cache_control`) | Passed through to the upstream provider; cache hits/savings are reflected in the WaddleAI usage ledger |

## MCP tools (optional, requires `waddleai.mcp_v2`)

The MCP configs below read the key from the environment:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

Claude Code can also connect to WaddleAI's MCP server directly for
`search_code`/`memory_search`/`list_models`/etc., independent of the
`/v1/messages` traffic above. Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "waddleai": {
      "type": "http",
      "url": "https://your-waddleai-host/mcp",
      "headers": {
        "Authorization": "Bearer <your-waddleai-key>"
      }
    }
  }
}
```

Or, for a dev machine without a persistent HTTP connection, use the
`waddleai-mcp` Rust stdio shim (see the [CLI docs](../../clients/waddleai-cli/README.md)):

```json
{
  "mcpServers": {
    "waddleai": {
      "command": "waddleai",
      "args": ["mcp"],
      "env": {
        "WADDLEAI_API_URL": "https://your-waddleai-host",
        "WADDLEAI_API_KEY": "<your-waddleai-key>"
      }
    }
  }
}
```

Both forward to the same `/mcp` streamable-HTTP endpoint; the shim exists
for environments where Claude Code's stdio-only transport is easier to wire
than an HTTP MCP connection.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Claude Code reports an unrecognized response shape | Deployment predates the §5 `/v1/messages` fidelity work | Upgrade WaddleAI |
| `401 Unauthorized` | `ANTHROPIC_API_KEY` isn't a valid `wa-` key, or `ANTHROPIC_BASE_URL` has a trailing path segment | Use the bare host, e.g. `https://waddleai.example.com` (no `/v1`) |
| Tool calls silently drop context | Model behind the key doesn't support `tool_use` | Pin a model known to support tools via `set_preference` or the routing policy |
| MCP tools missing | `waddleai.mcp_v2` flag off, or connecting to `/mcp/admin` with a non-admin key | Confirm the flag and that you're pointed at `/mcp`, not `/mcp/admin` |

## See also

- [OpenCode](opencode.md) (default apparatus)
- [Cursor](cursor.md)
- [Antigravity](antigravity.md)
- [Generic OpenAI-compatible clients](generic-openai.md)
