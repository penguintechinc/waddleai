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
  },
  "waddleai": {
    "provider": "openai",
    "model_used": "gpt-4",
    "security_passed": true,
    "routing_rule": "default",
    "cost_waddleai": 8,
    "cost_usd": 0.008
  }
}
```

#### Error Responses

```json
{
  "error": {
    "type": "quota_exceeded",
    "message": "Daily token quota exceeded",
    "code": "quota_exceeded",
    "details": {
      "daily_used": 10000,
      "daily_limit": 10000,
      "monthly_used": 50000,
      "monthly_limit": 100000
    }
  }
}
```

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
      "context_length": 8192,
      "cost_per_waddleai_token": 0.001
    },
    {
      "id": "claude-3-opus",
      "object": "model", 
      "created": 1699896916,
      "owned_by": "anthropic",
      "provider": "anthropic",
      "capabilities": ["chat"],
      "context_length": 200000,
      "cost_per_waddleai_token": 0.0015
    },
    {
      "id": "llama2",
      "object": "model",
      "created": 1699896916,
      "owned_by": "meta",
      "provider": "ollama",
      "capabilities": ["chat", "completion"],
      "context_length": 4096,
      "cost_per_waddleai_token": 0.0001
    }
  ]
}
```

## Completions (Legacy)

### POST /v1/completions

Generate text completions (legacy endpoint, chat completions recommended).

#### Request

```bash
curl https://your-waddleai-proxy.com/v1/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer wa-your-api-key" \
  -d '{
    "model": "gpt-3.5-turbo",
    "prompt": "Once upon a time",
    "max_tokens": 100,
    "temperature": 0.7
  }'
```

#### Response

```json
{
  "id": "cmpl-abc123",
  "object": "text_completion",
  "created": 1699896916,
  "model": "gpt-3.5-turbo",
  "choices": [
    {
      "text": " there was a small village nestled in the mountains...",
      "index": 0,
      "logprobs": null,
      "finish_reason": "length"
    }
  ],
  "usage": {
    "prompt_tokens": 4,
    "completion_tokens": 100,
    "total_tokens": 104,
    "waddleai_tokens": 12
  }
}
```

## Embeddings

### POST /v1/embeddings

Create embeddings for text inputs (if supported by target model).

#### Request

```bash
curl https://your-waddleai-proxy.com/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer wa-your-api-key" \
  -d '{
    "model": "text-embedding-ada-002",
    "input": "The food was delicious and the waiter was friendly."
  }'
```

#### Response

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "embedding": [0.0023064255, -0.009327292, ...],
      "index": 0
    }
  ],
  "model": "text-embedding-ada-002",
  "usage": {
    "prompt_tokens": 8,
    "total_tokens": 8,
    "waddleai_tokens": 2
  }
}
```

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

### Security Alerts

Get recent security alerts (if you have appropriate permissions):

#### GET /api/security/threats

```bash
curl https://your-waddleai-proxy.com/api/security/threats \
  -H "Authorization: Bearer wa-your-api-key"
```

Response:
```json
{
  "recent_threats": [
    {
      "timestamp": "2024-01-15T10:30:00Z",
      "threat_type": "prompt_injection",
      "severity": "high",
      "blocked": true,
      "description": "Detected instruction override attempt"
    }
  ],
  "stats": {
    "last_24h": {
      "total_threats": 3,
      "blocked": 3,
      "allowed": 0
    }
  }
}
```

## Rate Limits

WaddleAI enforces multiple types of limits:

| Limit Type | Default | Description |
|------------|---------|-------------|
| Requests per minute | 60 | API calls per minute |
| Daily tokens | 10,000 | WaddleAI tokens per day |
| Monthly tokens | 100,000 | WaddleAI tokens per month |

Rate limit information is included in response headers:

```
X-RateLimit-Limit-RPM: 60
X-RateLimit-Remaining-RPM: 45
X-RateLimit-Reset-RPM: 1699896976
X-RateLimit-Limit-Daily: 10000
X-RateLimit-Remaining-Daily: 8800
```

## Error Codes

| Code | Type | Description |
|------|------|-------------|
| 400 | `invalid_request` | Invalid request format |
| 400 | `security_blocked` | Request blocked by security scanning |
| 401 | `invalid_api_key` | Invalid or expired API key |
| 403 | `insufficient_permissions` | Insufficient permissions |
| 429 | `rate_limit_exceeded` | Rate limit exceeded |
| 429 | `quota_exceeded` | Token quota exceeded |
| 500 | `server_error` | Internal server error |
| 502 | `provider_error` | Upstream LLM provider error |
| 503 | `service_unavailable` | Service temporarily unavailable |

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

For more advanced features, see the [Management API documentation](management-api.md).