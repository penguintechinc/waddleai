# API Examples

Complete examples for using WaddleAI with various programming languages and tools.

## Quick Start Examples

### Python with OpenAI SDK

```python
from openai import OpenAI

# Point to your WaddleAI instance
client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")

# Use any supported model
response = client.chat.completions.create(
    model="gpt-4",  # Or "claude-3-opus", "llama3.2", etc.
    messages=[
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Write a Python function to calculate fibonacci numbers."},
    ],
)

print(response.choices[0].message.content)
```

### Python with Anthropic SDK (Claude Messages)

```python
import anthropic

# Point to your WaddleAI instance
client = anthropic.Anthropic(api_key="<your-waddleai-key>", base_url="http://localhost:8000")

message = client.messages.create(
    model="claude-3-5-sonnet-20241022",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain quantum computing in simple terms."}],
)

print(message.content[0].text)
```

### JavaScript/TypeScript with OpenAI SDK

```typescript
import OpenAI from 'openai';

const client = new OpenAI({
  apiKey: '<your-waddleai-key>',
  baseURL: 'http://localhost:8000/v1',
});

async function chat() {
  const completion = await client.chat.completions.create({
    model: 'gpt-4',
    messages: [
      { role: 'system', content: 'You are a helpful assistant.' },
      { role: 'user', content: 'What is the capital of France?' },
    ],
  });

  console.log(completion.choices[0].message.content);
}

chat();
```

### cURL Examples

The curl examples below read your key from the environment:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

#### OpenAI Chat Completion

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Write a haiku about coding."}
    ],
    "max_tokens": 100
  }'
```

#### Claude Messages API

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: $WADDLEAI_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Explain the theory of relativity."}
    ]
  }'
```

#### Streaming Response

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Count to 10."}],
    "stream": true
  }'
```

## Model Selection Examples

### Request-Level Model Override

```python
# Specify model in request (highest priority)
response = client.chat.completions.create(
    model="claude-3-opus", messages=[{"role": "user", "content": "Complex reasoning task"}]
)
```

### Header-Based Model Preference

```python
import requests

import os
response = requests.post(
    "http://localhost:8000/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {os.environ['WADDLEAI_API_KEY']}",
        "X-Preferred-Model": "codellama:34b",  # Preferred model
    },
    json={
        "model": "auto",  # Will use X-Preferred-Model
        "messages": [{"role": "user", "content": "Write Python code"}],
    },
)
```

### Automatic Routing

```python
# Let WaddleAI's routing LLM choose the best model
response = client.chat.completions.create(
    model="auto",  # Intelligent routing
    messages=[
        {
            "role": "user",
            "content": "Debug this Python function: def factorial(n): return n * factorial(n)",
        }
    ],
)
# Router will likely choose codellama or claude-3 for programming task
```

## Advanced Examples

### Streaming with Event Handling

```python
from openai import OpenAI

client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")

stream = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Write a short story about AI."}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content is not None:
        print(chunk.choices[0].delta.content, end="")
```

### Multiple Conversations with Context

```python
conversation = [
    {"role": "system", "content": "You are a Python expert."},
    {"role": "user", "content": "How do I read a file in Python?"},
]

response1 = client.chat.completions.create(model="gpt-4", messages=conversation)

# Add response to conversation
conversation.append({"role": "assistant", "content": response1.choices[0].message.content})

# Continue conversation
conversation.append({"role": "user", "content": "How do I handle errors when reading files?"})

response2 = client.chat.completions.create(model="gpt-4", messages=conversation)
```

### Function Calling

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather in a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["location"],
            },
        },
    }
]

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "What's the weather in Boston?"}],
    tools=tools,
    tool_choice="auto",
)

# Handle tool calls
if response.choices[0].message.tool_calls:
    tool_call = response.choices[0].message.tool_calls[0]
    print(f"Function to call: {tool_call.function.name}")
    print(f"Arguments: {tool_call.function.arguments}")
```

### Embeddings

```python
response = client.embeddings.create(
    model="text-embedding-ada-002", input="The quick brown fox jumps over the lazy dog."
)

embedding = response.data[0].embedding
print(f"Embedding dimension: {len(embedding)}")
```

## Error Handling

### Basic Error Handling

```python
from openai import OpenAI, APIError, RateLimitError, AuthenticationError

client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")

try:
    response = client.chat.completions.create(
        model="gpt-4", messages=[{"role": "user", "content": "Hello!"}]
    )
except AuthenticationError as e:
    print(f"Authentication failed: {e}")
except RateLimitError as e:
    print(f"Rate limit exceeded: {e}")
except APIError as e:
    print(f"API error: {e}")
```

### Retry with Exponential Backoff

```python
import time
from openai import OpenAI, RateLimitError


def chat_with_retry(client, messages, max_retries=3):
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(model="gpt-4", messages=messages)
        except RateLimitError as e:
            if attempt < max_retries - 1:
                wait_time = 2**attempt  # Exponential backoff
                print(f"Rate limited. Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise
```

## Integration Examples

### Flask Web Application

```python
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)
client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")


@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message")

    response = client.chat.completions.create(
        model="gpt-4", messages=[{"role": "user", "content": user_message}]
    )

    return jsonify(
        {
            "response": response.choices[0].message.content,
            "tokens_used": response.usage.total_tokens,
        }
    )


if __name__ == "__main__":
    app.run(port=5000)
```

### FastAPI with Streaming

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from openai import OpenAI

app = FastAPI()
client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")


@app.post("/chat/stream")
async def chat_stream(message: str):
    def generate():
        stream = client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": message}], stream=True
        )
        for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    return StreamingResponse(generate(), media_type="text/plain")
```

### Async Python with httpx

```python
import httpx
import asyncio


import os
async def chat_async(message: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ['WADDLEAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={"model": "gpt-4", "messages": [{"role": "user", "content": message}]},
        )
        return response.json()


# Usage
result = asyncio.run(chat_async("Hello, WaddleAI!"))
print(result["choices"][0]["message"]["content"])
```

## Testing Examples

### Unit Test with Mock

```python
import unittest
from unittest.mock import patch, MagicMock
from openai import OpenAI


class TestChatbot(unittest.TestCase):
    @patch("openai.resources.chat.completions.Completions.create")
    def test_chat_response(self, mock_create):
        # Mock the API response
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "Hello, human!"
        mock_create.return_value = mock_response

        client = OpenAI(api_key="test", base_url="http://localhost:8000/v1")
        response = client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "Hi"}]
        )

        self.assertEqual(response.choices[0].message.content, "Hello, human!")
```

### Load Testing with locust

```python
from locust import HttpUser, task, between


import os
class WaddleAIUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.headers = {
            "Authorization": f"Bearer {os.environ['WADDLEAI_API_KEY']}",
            "Content-Type": "application/json",
        }

    @task
    def chat_completion(self):
        self.client.post(
            "/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "Hello!"}],
                "max_tokens": 50,
            },
        )
```

## Best Practices

### 1. Connection Pooling

```python
from openai import OpenAI
import threading

# Reuse client across threads
client = OpenAI(api_key="<your-waddleai-key>", base_url="http://localhost:8000/v1")


def worker(prompt):
    response = client.chat.completions.create(
        model="gpt-4", messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


# Safe for concurrent use
threads = [threading.Thread(target=worker, args=(f"Question {i}",)) for i in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

### 2. Token Counting

```python
import tiktoken


def count_tokens(text, model="gpt-4"):
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text))


# Estimate cost before request
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Write a long essay about AI."},
]

total_tokens = sum(count_tokens(msg["content"]) for msg in messages)
print(f"Estimated input tokens: {total_tokens}")
```

### 3. Graceful Degradation

```python
def smart_chat(message, preferred_model="gpt-4", fallback_model="gpt-3.5-turbo"):
    try:
        return client.chat.completions.create(
            model=preferred_model, messages=[{"role": "user", "content": message}]
        )
    except Exception as e:
        print(f"Failed with {preferred_model}, trying {fallback_model}")
        return client.chat.completions.create(
            model=fallback_model, messages=[{"role": "user", "content": message}]
        )
```

## See Also

- [OpenAI-Compatible API Reference](openai-compatible.md)
- [Authentication Guide](authentication.md)
- [Claude Code Integration](../integrations/claude-code.md)
- [Cursor IDE Integration](../integrations/cursor-ide.md)
