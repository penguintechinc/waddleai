# OpenCode + WaddleAI

OpenCode is WaddleAI's default apparatus: one config file gets you both an
OpenAI-compatible model provider routed through WaddleAI and the WaddleAI
MCP tools (`search_code`, `memory_search`, `list_models`, ...) in the same
session.

## Prerequisites

- A WaddleAI virtual key (`wa-...`) with the `waddleai.mcp_v2` flag enabled
  for your org (ask an admin, or check `/api/v1/integrations/opencode-config`
  — see below).
- OpenCode installed (`npm install -g opencode-ai` or your platform's
  release binary).

## Quick start

1. Export your key:
   ```bash
   export WADDLEAI_API_KEY=wa-your-key-here
   ```
2. Copy [`examples/opencode/opencode.json`](../../examples/opencode/opencode.json)
   into your project (or `~/.config/opencode/opencode.json` for a global
   default), and replace `{your-waddleai-host}` with your deployment's
   hostname.
3. Start OpenCode. The `waddleai` provider appears in the model picker; the
   `waddleai` MCP server's tools appear in the tool palette automatically.

## Per-key generated config (recommended)

Rather than hand-editing the placeholder, download a config pre-filled for
your exact virtual key and deployment:

```bash
curl -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  https://your-waddleai-host/api/v1/integrations/opencode-config \
  -o opencode.json
```

The rendered file sources its `models` list live from `/v1/models` at
generation time (so it reflects your org's actual routing/model assignments,
not the static placeholder in the example) and points the `waddleai`
provider and MCP entries at your deployment with your key already filled in
— nothing to redact before checking it in (don't check it in; it embeds a
live key).

> This endpoint is served by the WaddleAI Management API's `/api/v1/integrations`
> surface. If your deployment predates that rollout, use the manual steps
> above.

## What you get

| Config section | Purpose |
|---|---|
| `provider.waddleai` | Custom OpenAI-compatible provider — every model WaddleAI can route to (including `smart-router` for automatic model selection) is reachable through `/v1/chat/completions` |
| `mcp.waddleai` | Streamable-HTTP MCP connection to `/mcp` — `search_code`, `get_symbol`, `search_docs`, `fetch_docs`, `memory_add`, `memory_search`, `list_models`, `get_routing_policy`, `usage_summary`, `set_preference` |

Auth for both is the same `wa-` bearer key — one credential, one connection
in OpenCode's eyes, whether it's asking a model a question or calling a
tool.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Provider missing from model picker | `WADDLEAI_API_KEY` unset or malformed `baseURL` | Confirm the env var is exported in the shell OpenCode launched from |
| MCP tools not listed | `waddleai.mcp_v2` flag off for your org, or key lacks the right scope | Ask an admin to check the flag; confirm your key isn't an admin-only key pointed at the wrong mount |
| `401` on every request | Expired or revoked key | Rotate the key in the WebUI (Virtual Keys page) |
| Model list stale | Using the static example instead of the generated config | Re-download from `/api/v1/integrations/opencode-config` |

## See also

- [Claude Code](claude-code.md)
- [Cursor](cursor.md)
- [Antigravity](antigravity.md)
- [Generic OpenAI-compatible clients](generic-openai.md)
