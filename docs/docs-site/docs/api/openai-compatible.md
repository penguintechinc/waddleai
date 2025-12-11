# OpenAI-Compatible API

WaddleAI provides a fully compatible OpenAI API interface, plus the Claude Messages API format.

## Base URL

```
http://localhost:8000
```

## Authentication

All requests require an API key in the Authorization header:

```bash
Authorization: Bearer wai_your_api_key_here
```

## Supported Endpoints

### Chat Completions (OpenAI Format)

**Endpoint**: `POST /v1/chat/completions`

**Request**:
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "temperature": 0.7,
  "max_tokens": 150,
  "stream": false
}
```

**Response**:
```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1234567890,
  "model": "gpt-3.5-turbo",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello! How can I help you today?"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 9,
    "total_tokens": 19,
    "waddleai_tokens": 23
  }
}
```

### Claude Messages API Format

**Endpoint**: `POST /v1/messages`

**Request**:
```json
{
  "model": "claude-3-sonnet-20240229",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "Hello, Claude!"}
  ],
  "system": "You are a helpful assistant.",
  "temperature": 0.7
}
```

**Response**:
```json
{
  "id": "msg_123",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "Hello! How can I assist you today?"
    }
  ],
  "model": "claude-3-sonnet-20240229",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 10,
    "output_tokens": 9,
    "waddleai_tokens": 23
  }
}
```

### List Models

**Endpoint**: `GET /v1/models`

**Response**:
```json
{
  "object": "list",
  "data": [
    {
      "id": "gpt-4",
      "object": "model",
      "created": 1234567890,
      "owned_by": "openai",
      "provider": "openai"
    },
    {
      "id": "claude-3-opus",
      "object": "model",
      "created": 1234567890,
      "owned_by": "anthropic",
      "provider": "anthropic"
    }
  ]
}
```

### Embeddings

**Endpoint**: `POST /v1/embeddings`

**Request**:
```json
{
  "model": "text-embedding-ada-002",
  "input": "The quick brown fox"
}
```

**Response**:
```json
{
  "object": "list",
  "data": [{
    "object": "embedding",
    "embedding": [0.1, 0.2, ...],
    "index": 0
  }],
  "model": "text-embedding-ada-002",
  "usage": {
    "prompt_tokens": 5,
    "total_tokens": 5,
    "waddleai_tokens": 6
  }
}
```

## Streaming Responses

**Request with streaming**:
```json
{
  "model": "gpt-3.5-turbo",
  "messages": [{"role": "user", "content": "Count to 5"}],
  "stream": true
}
```

**Response** (Server-Sent Events):
```
data: {"id":"chatcmpl-123","choices":[{"delta":{"content":"1"}}]}

data: {"id":"chatcmpl-123","choices":[{"delta":{"content":", 2"}}]}

data: {"id":"chatcmpl-123","choices":[{"delta":{"content":", 3"}}]}

data: [DONE]
```

## Intelligent Routing

WaddleAI automatically routes requests to the best model unless specified.

### Model Selection Hierarchy

1. **Request Override**: Specify model in request
2. **API Key Default**: Set in Management Portal
3. **User Default**: Per-user preference
4. **Organization Default**: Per-org preference
5. **Routing LLM**: AI-powered decision
6. **System Default**: Fallback model

### Model Preference Header

Override routing with header:

```bash
X-Preferred-Model: codellama
```

## Usage Tracking

WaddleAI tracks both internal (WaddleAI tokens) and external (LLM tokens) usage.

**WaddleAI Tokens**: Internal accounting with markup
**LLM Tokens**: Actual tokens used by provider

Example:
```json
{
  "usage": {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150,
    "waddleai_tokens": 180,
    "provider": "openai",
    "model": "gpt-3.5-turbo",
    "cost_usd": 0.0003
  }
}
```

## Code Examples

### Python

```python
import openai

# Configure OpenAI SDK to use WaddleAI
openai.api_base = "http://localhost:8000/v1"
openai.api_key = "wai_your_key_here"

# Use as normal
response = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)
```

### Node.js

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: 'wai_your_key_here',
  baseURL: 'http://localhost:8000/v1',
});

const response = await client.chat.completions.create({
  model: 'gpt-3.5-turbo',
  messages: [{ role: 'user', content: 'Hello!' }],
});

console.log(response.choices[0].message.content);
```

### cURL

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer wai_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Error Handling

### Standard Errors

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

### Common Error Codes

| Code | Description |
|------|-------------|
| `invalid_api_key` | API key missing or invalid |
| `quota_exceeded` | Token limit exceeded |
| `rate_limit_exceeded` | Too many requests |
| `model_not_found` | Requested model not available |
| `prompt_injection_detected` | Security violation |

## Next Steps

- [Management API](management-api.md)
- [Authentication](authentication.md)
- [Code Examples](examples.md)