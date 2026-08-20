# Generic OpenAI-Compatible Clients + WaddleAI

Any tool that speaks the OpenAI SDK's `base_url` + `api_key` pattern works
against WaddleAI without a WaddleAI-specific integration. This covers the
official OpenAI SDKs (Python, Node), most LangChain/LlamaIndex integrations,
and any other client with a "custom OpenAI-compatible endpoint" option.

## Setup

```python
import openai

client = openai.OpenAI(
    api_key="wa-your-key-here",
    base_url="https://your-waddleai-host/v1",
)

response = client.chat.completions.create(
    model="smart-router",  # or a specific model WaddleAI routes to
    messages=[{"role": "user", "content": "Hello"}],
)
```

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: 'wa-your-key-here',
  baseURL: 'https://your-waddleai-host/v1',
});

const completion = await client.chat.completions.create({
  model: 'smart-router',
  messages: [{ role: 'user', content: 'Hello' }],
});
```

## What's supported

- `POST /v1/chat/completions` — streaming and non-streaming, tool calls,
  JSON mode where the underlying provider supports it
- `GET /v1/models` — model discovery
- Standard OpenAI error shapes (`error.type`, `error.code`) so existing
  retry/backoff logic in OpenAI SDKs works unmodified

## What's WaddleAI-specific (read if you're debugging)

- **`usage.waddleai`**: every response's `usage` object carries an
  additional `waddleai` sub-object with WaddleAI-normalized token counts
  (billing units, comparable across providers) alongside the raw
  provider-reported `prompt_tokens`/`completion_tokens`. Most OpenAI clients
  ignore unknown fields, so this is additive and safe to ignore if you don't
  need it.
- **`smart-router` as a model name**: a virtual model that picks the actual
  provider/model per request based on your org's routing policy. Pin a
  specific model instead if you need deterministic model selection.
- **MCP tools are a separate surface** — the OpenAI-compatible API has no
  concept of WaddleAI's `search_code`/`memory_search`/etc. tools. If your
  client also speaks MCP, see [OpenCode](opencode.md) or
  [Claude Code](claude-code.md) for how the two surfaces combine.

## Troubleshooting

`$WADDLEAI_API_KEY` in the table below is your exported virtual key:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

| Symptom | Cause | Fix |
|---|---|---|
| `401` | Malformed `Authorization` header (some clients need `Bearer <key>`, others just `<key>`) | Confirm your SDK sends `Authorization: Bearer $WADDLEAI_API_KEY` |
| `404` on `/v1/chat/completions` | `base_url` missing `/v1` or has a trailing slash mismatch | Use exactly `https://your-waddleai-host/v1` |
| Response missing expected model | Requested `smart-router` and got routed elsewhere | Pin an explicit model, or check `get_routing_policy` via MCP |

## See also

- [OpenCode](opencode.md) (default apparatus)
- [Claude Code](claude-code.md)
- [Cursor](cursor.md)
- [Antigravity](antigravity.md)
