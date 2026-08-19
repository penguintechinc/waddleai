# OpenAI-Compatible API Reference

WaddleAI provides a fully compatible OpenAI API that can be used as a drop-in replacement for OpenAI's API. All requests include additional WaddleAI features like security scanning, token management, and routing.

## Base URL

```
https://your-waddleai-proxy.com/v1
```

## Authentication

Use your WaddleAI API key in the Authorization header:

```
Authorization: Bearer wa-your-api-key-here
```

## Chat Completions

### POST /v1/chat/completions

Create a chat completion response. Identical to OpenAI's API with additional WaddleAI features.

#### Request

```bash
curl https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer wa-your-api-key" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "user", "content": "Hello, how are you?"}
    ],
    "temperature": 0.7,
    "max_tokens": 150
  }'
```

#### Request Body Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | string | Yes | Model to use (e.g., "gpt-4", "claude-3-opus", "llama2") |
| `messages` | array | Yes | Array of message objects |
| `temperature` | number | No | Sampling temperature (0-2) |
| `max_tokens` | integer | No | Maximum tokens to generate |
| `top_p` | number | No | Nucleus sampling parameter |
| `frequency_penalty` | number | No | Frequency penalty (-2 to 2) |
| `presence_penalty` | number | No | Presence penalty (-2 to 2) |
| `stop` | string/array | No | Stop sequences |
| `stream` | boolean | No | Whether to stream responses |

#### Selecting a provider — provider-qualified model strings

Prefix the `model` field with a provider name to pin **both** the provider and the model:

```python
model="anthropic:claude-opus-5-1m"   # this model, from Anthropic directly
model="bedrock:claude-opus-5-1m"     # the same model, via AWS Bedrock
model="ollama:gemma4:e2b"            # local Ollama
model="gemma4:e2b"                   # no provider pinned — WaddleAI routes
```

This matters because naming a model does not determine who serves it. The same Claude model is reachable through Anthropic direct, AWS Bedrock and GCP Vertex, each with different data residency, contractual terms, quota pools and pricing. Without a pin, WaddleAI chooses.

It works in the plain `model` field rather than a header, so any OpenAI-compatible SDK supports it with no special handling.

**Parsing**: the prefix is treated as a provider **only if it exactly matches a known provider** — `openai`, `anthropic`, `ollama`, `llamacpp`, `gemini`, `bedrock`, `azure_openai`, `cohere`, `xai` — and only the first colon is split on. Otherwise the whole string is the model name. That rule is why `gemma4:e2b` still resolves as a model: Ollama tags contain colons natively, and there is no provider called `gemma4`.

**A pin disables substitution.** If the pinned provider is unavailable the request fails with a typed error rather than being served by another provider — pinning is usually a data-residency or contractual decision, and silently substituting would fail open on exactly that constraint. Org allow-lists and capability checks still apply; a pin cannot reach a provider your org is not permitted to use.

> **Status:** specified, not yet implemented. See the platform spec §7.2 Stage 0. Today only the model name is honoured (see `X-Preferred-Model` below); the provider is chosen by the router.

#### WaddleAI-Specific Headers

| Header | Status | Description |
|--------|--------|-------------|
| `X-Preferred-Model` | **Implemented** | Overrides the `model` field in the request body |
| `X-Session-ID` | **Implemented** | Conversation/session identifier (also accepted as `session_id` in the body) |
| `X-WaddleAI-Tool-Type` | Specified, not implemented | Declares the tool type explicitly, skipping classification (spec §7.2 stage 0) |
| `X-WaddleAI-Route` | ⚠️ **Not implemented** | Previously documented as forcing a provider. Nothing reads it. Use a provider-qualified model string instead — it is provider *and* model, and needs no custom header |
| `X-WaddleAI-Memory` | ⚠️ **Not implemented** | Previously documented; nothing reads it |
| `X-WaddleAI-Security` | ⚠️ **Not implemented** | Previously documented; nothing reads it. Security policy is resolved from `security_policies` scopes, not per-request |

#### Response

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699896916,
  "model": "gpt-4",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! I'm doing well, thank you for asking. How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 19,
    "total_tokens": 31,
    "waddleai_tokens": 8
  }
}
```

There is no top-level `waddleai` metadata object. When cache, proxy-memory, or routing features are active for the request, an additive `usage.waddleai` object is merged in (e.g. `tokens_saved`, `routed_from`) — it's omitted entirely, not `{}`, when none of those apply.

#### Error Responses

Every error is the same shape — `type` is always the literal string `"error"`; the specific cause is in `message` (there is no `code` or `details` field):

```json
{
  "error": {
    "message": "token budget exceeded for this key",
    "type": "error"
  }
}
```

HTTP status carries the actual error category: `401` unauthenticated, `403` missing org/tenant context or insufficient permission, `429` quota/token-budget exceeded, `502`/`504` upstream provider error/timeout, `500` internal error.

### Streaming Responses

Set `"stream": true` to receive server-sent events:

```bash
curl https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer wa-your-api-key" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Count to 5"}],
    "stream": true
  }'
```

Response:
```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1699896916,"model":"gpt-4","choices":[{"index":0,"delta":{"role":"assistant","content":"1"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","created":1699896916,"model":"gpt-4","choices":[{"index":0,"delta":{"content":", 2"},"finish_reason":null}]}

...

data: [DONE]
```

## Models

### GET /v1/models

List available models across all configured providers.

#### Request

```bash
curl https://your-waddleai-proxy.com/v1/models \
  -H "Authorization: Bearer wa-your-api-key"
```

#### Response

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4",
      "object": "model",
      "created": 1699896916,
      "owned_by": "openai",
      "provider": "openai",
      "capabilities": ["chat", "completion"],
      "context_length": 8192
    },
    {
      "id": "claude-3-opus",
      "object": "model", 
      "created": 1699896916,
      "owned_by": "anthropic",
      "provider": "anthropic",
      "capabilities": ["chat"],
      "context_length": 200000
    },
    {
      "id": "llama2",
      "object": "model",
      "created": 1699896916,
      "owned_by": "meta",
      "provider": "ollama",
      "capabilities": ["chat", "completion"],
      "context_length": 4096
    }
  ]
}
```

There is no `cost_per_waddleai_token` field on model list entries — cost is derived per request in `usage`, not published per model.

## Not Implemented

The legacy `POST /v1/completions` and `POST /v1/embeddings` endpoints are **not implemented** by the proxy — only `/v1/chat/completions`, `/v1/models`, `/v1/messages`, and `/v1/messages/count_tokens` exist (verified against `proxy/apps/proxy_server/main.py` route table). Requests to either return `404`.

## WaddleAI Extensions

### Usage Information

Get current usage and quota information:

#### GET /api/usage

```bash
curl https://your-waddleai-proxy.com/api/usage \
  -H "Authorization: Bearer wa-your-api-key"
```

Response:
```json
{
  "total_waddleai_tokens": 1500,
  "total_llm_input_tokens": 8000,
  "total_llm_output_tokens": 4000,
  "total_requests": 45,
  "llm_breakdown": {
    "openai_gpt4": {"input": 5000, "output": 2500},
    "anthropic_claude": {"input": 2000, "output": 1000},
    "ollama_llama2": {"input": 1000, "output": 500}
  },
  "daily_usage": {
    "2024-01-15": {"waddleai_tokens": 500, "requests": 15},
    "2024-01-14": {"waddleai_tokens": 750, "requests": 20}
  }
}
```

#### GET /api/quota

```bash
curl https://your-waddleai-proxy.com/api/quota \
  -H "Authorization: Bearer wa-your-api-key"
```

Response:
```json
{
  "quota_ok": true,
  "daily": {
    "used": 1200,
    "limit": 10000,
    "remaining": 8800,
    "ok": true
  },
  "monthly": {
    "used": 15000,
    "limit": 100000,
    "remaining": 85000,
    "ok": true
  }
}
```

There is no `GET /api/security/threats` endpoint — no security-alerts API exists today.

## Rate Limits

Per-org request-rate limiting is enforced at the network layer via a Cilium `CiliumEnvoyConfig` local-rate-limit filter (`services/management/app/services/cilium_policy.py`), not inside the application. The proxy does **not** emit `X-RateLimit-*` response headers — do not build client logic around them. Token budgets (daily/monthly, per key/user/org) are enforced in-app via `TokenBudgetStage` and surface as a `429` with `error.message` describing the exceeded limit (see Error Responses above); there is no dedicated requests-per-minute counter independent of Cilium's edge enforcement.

## Error Codes

| Status | Meaning |
|--------|---------|
| 400 | Invalid request (bad routing strategy, malformed body) |
| 401 | Missing/invalid Authorization header or API key |
| 403 | Missing organization/tenant context, or admin permission required |
| 404 | Unknown route (includes `/v1/completions`, `/v1/embeddings` — not implemented) |
| 429 | Token/quota budget exceeded |
| 500 | Internal server error |
| 502 | Upstream LLM provider error |
| 504 | Upstream LLM provider timeout |

There is no separate machine-readable `error.type`/`error.code` taxonomy (e.g. `quota_exceeded`, `invalid_api_key`) — `error.type` is always the literal string `"error"`; branch on HTTP status instead.

## Best Practices

### Authentication
- Store API keys securely in environment variables
- Use different keys for different environments
- Rotate keys regularly

### Error Handling
```python
import openai
from openai import OpenAIError

try:
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello"}]
    )
except openai.RateLimitError as e:
    # Handle quota/rate limit exceeded
    print(f"Rate limited: {e}")
    # Implement exponential backoff
except openai.APIError as e:
    # Handle API errors
    print(f"API error: {e}")
```

### Performance
- Use connection pooling for high-volume applications
- Implement request caching where appropriate
- Monitor usage patterns and optimize model selection

### Cost Optimization
- Choose appropriate models for each task
- Monitor WaddleAI token consumption
- Use cheaper models for simple tasks
- Implement usage budgets and alerts

---

For provider/key/quota administration, see the WaddleAI Management API routes under `/api/v1/` (organizations, providers, keys, quotas, usage) — a dedicated reference doc doesn't exist yet.