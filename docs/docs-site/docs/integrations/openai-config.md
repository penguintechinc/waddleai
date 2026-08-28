# OpenAI Configuration

This guide covers configuring WaddleAI to connect to OpenAI's API, including API keys, model selection, rate limiting, and advanced features like function calling and vision.

## Prerequisites

- Active OpenAI account
- OpenAI API key with credits
- WaddleAI management portal access (admin role)

## Getting OpenAI API Key

1. Visit [OpenAI Platform](https://platform.openai.com/)
2. Sign in or create an account
3. Navigate to **API keys** section
4. Click **Create new secret key**
5. Name your key (e.g., "WaddleAI Production")
6. Copy and securely store the API key

!!! warning "Security"
    OpenAI API keys start with `sk-`. Never commit them to version control or share publicly.

## Adding OpenAI Connection in WaddleAI

### Via Management Portal

1. Log into WaddleAI Management Portal
2. Navigate to **Admin > LLM Providers**
3. Click **Add Provider**
4. Fill in the configuration:

**Provider Configuration:**

| Field | Value |
|-------|-------|
| Name | OpenAI Production |
| Provider | openai |
| Endpoint URL | https://api.openai.com/v1 |
| API Key | sk-your-api-key-here |
| Enabled | ✓ |

**Model List:**
```
gpt-4-turbo-preview
gpt-4
gpt-4-32k
gpt-3.5-turbo
gpt-3.5-turbo-16k
text-embedding-ada-002
dall-e-3
whisper-1
tts-1
```

5. Click **Save**

### Via API

```bash
curl -X POST https://your-waddleai-mgmt.com/api/connection_links \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenAI Production",
    "provider": "openai",
    "endpoint_url": "https://api.openai.com/v1",
    "api_key": "sk-your-api-key-here",
    "model_list": [
      "gpt-4-turbo-preview",
      "gpt-4",
      "gpt-3.5-turbo",
      "text-embedding-ada-002"
    ],
    "enabled": true,
    "rate_limits": {
      "requests_per_minute": 500,
      "tokens_per_minute": 150000
    }
  }'
```

### Via Configuration File

**config/providers.yaml:**

```yaml
providers:
  - name: openai_production
    provider: openai
    endpoint_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}  # From environment variable
    enabled: true
    organization_id: org-your-org-id  # Optional
    models:
      - gpt-4-turbo-preview
      - gpt-4
      - gpt-3.5-turbo
      - text-embedding-ada-002
      - dall-e-3
    rate_limits:
      requests_per_minute: 500
      tokens_per_minute: 150000
      tokens_per_day: 5000000
    retry_config:
      max_retries: 3
      initial_delay_ms: 1000
      max_delay_ms: 10000
      exponential_base: 2
```

## Available OpenAI Models

### GPT-4 Family

#### GPT-4 Turbo Preview
- **Model ID:** `gpt-4-turbo-preview`
- **Context window:** 128k tokens
- **Training data:** Up to April 2023
- **Best for:** Most advanced reasoning, complex tasks
- **Features:** Vision, JSON mode, function calling

#### GPT-4
- **Model ID:** `gpt-4`
- **Context window:** 8k tokens
- **Training data:** Up to September 2021
- **Best for:** Complex reasoning, creative tasks
- **Cost:** Premium pricing

#### GPT-4-32k
- **Model ID:** `gpt-4-32k`
- **Context window:** 32k tokens
- **Training data:** Up to September 2021
- **Best for:** Long document analysis

### GPT-3.5 Family

#### GPT-3.5 Turbo
- **Model ID:** `gpt-3.5-turbo`
- **Context window:** 16k tokens
- **Training data:** Up to September 2021
- **Best for:** Fast responses, cost-effective
- **Cost:** Most economical option

#### GPT-3.5 Turbo 16k
- **Model ID:** `gpt-3.5-turbo-16k`
- **Context window:** 16k tokens
- **Training data:** Up to September 2021
- **Best for:** Longer conversations

### Specialized Models

#### Text Embedding
- **Model ID:** `text-embedding-ada-002`
- **Use:** Generate embeddings for semantic search
- **Dimensions:** 1536
- **Cost:** Very low per token

#### DALL-E 3
- **Model ID:** `dall-e-3`
- **Use:** Image generation from text
- **Quality:** Standard or HD
- **Sizes:** 1024x1024, 1024x1792, 1792x1024

#### Whisper
- **Model ID:** `whisper-1`
- **Use:** Speech-to-text transcription
- **Languages:** 97+ languages
- **Input:** Audio files (mp3, mp4, wav, etc.)

#### TTS (Text-to-Speech)
- **Model ID:** `tts-1`, `tts-1-hd`
- **Use:** Convert text to speech
- **Voices:** alloy, echo, fable, onyx, nova, shimmer
- **Output:** MP3 format

## Rate Limits

OpenAI imposes rate limits based on your usage tier:

### Usage Tiers

| Tier | Requirements | RPM | TPM |
|------|-------------|-----|-----|
| Free | New accounts | 3 | 40,000 |
| Tier 1 | $5+ paid | 500 | 150,000 |
| Tier 2 | $50+ paid, 7+ days | 1,000 | 300,000 |
| Tier 3 | $100+ paid, 7+ days | 3,000 | 600,000 |
| Tier 4 | $250+ paid, 14+ days | 5,000 | 1,000,000 |
| Tier 5 | $1000+ paid, 30+ days | 10,000 | 5,000,000 |

### Configure Rate Limits in WaddleAI

```json
{
  "rate_limits": {
    "requests_per_minute": 500,
    "tokens_per_minute": 150000,
    "tokens_per_day": 5000000,
    "concurrent_requests": 50
  }
}
```

## Using OpenAI via WaddleAI

### Basic Chat Completion

```python
import openai

client = openai.OpenAI(
    api_key="<your-waddleai-key>",
    base_url="https://your-waddleai-proxy.com/v1"
)

response = client.chat.completions.create(
    model="gpt-4-turbo-preview",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain quantum computing"}
    ],
    max_tokens=1000
)

print(response.choices[0].message.content)
```

### Streaming Responses

```python
response = client.chat.completions.create(
    model="gpt-4-turbo-preview",
    messages=[{"role": "user", "content": "Write a story"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Function Calling

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"]
                    }
                },
                "required": ["location"]
            }
        }
    }
]

response = client.chat.completions.create(
    model="gpt-4-turbo-preview",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=tools,
    tool_choice="auto"
)

# Check if function was called
if response.choices[0].message.tool_calls:
    function_call = response.choices[0].message.tool_calls[0]
    print(f"Function: {function_call.function.name}")
    print(f"Arguments: {function_call.function.arguments}")
```

### JSON Mode

```python
response = client.chat.completions.create(
    model="gpt-4-turbo-preview",
    response_format={"type": "json_object"},
    messages=[
        {"role": "system", "content": "You are a helpful assistant. Respond in JSON."},
        {"role": "user", "content": "List 3 programming languages with their use cases"}
    ]
)

import json
data = json.loads(response.choices[0].message.content)
print(data)
```

### Vision (GPT-4V)

```python
response = client.chat.completions.create(
    model="gpt-4-vision-preview",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.com/image.jpg"
                    }
                }
            ]
        }
    ],
    max_tokens=300
)

print(response.choices[0].message.content)
```

### Embeddings

```python
response = client.embeddings.create(
    model="text-embedding-ada-002",
    input="Your text to embed"
)

embedding = response.data[0].embedding
print(f"Embedding dimensions: {len(embedding)}")
```

### Image Generation (DALL-E 3)

```python
response = client.images.generate(
    model="dall-e-3",
    prompt="A futuristic city with flying cars at sunset",
    size="1024x1024",
    quality="standard",
    n=1
)

image_url = response.data[0].url
print(f"Image URL: {image_url}")
```

### Speech-to-Text (Whisper)

```python
audio_file = open("speech.mp3", "rb")
transcript = client.audio.transcriptions.create(
    model="whisper-1",
    file=audio_file
)

print(transcript.text)
```

### Text-to-Speech

```python
response = client.audio.speech.create(
    model="tts-1",
    voice="alloy",
    input="Hello! This is a test of text-to-speech."
)

with open("speech.mp3", "wb") as f:
    f.write(response.content)
```

## Advanced Configuration

### Organization ID

For accounts with multiple organizations:

```yaml
providers:
  - name: openai_org1
    provider: openai
    endpoint_url: https://api.openai.com/v1
    api_key: ${OPENAI_API_KEY}
    organization_id: org-abc123
```

### Custom Headers

```yaml
custom_headers:
  OpenAI-Organization: org-abc123
  OpenAI-Project: proj-xyz789
```

### Timeout Configuration

```yaml
timeout_config:
  connect_timeout_ms: 10000
  read_timeout_ms: 120000  # 2 minutes for long completions
  write_timeout_ms: 10000
```

### Retry Configuration

```yaml
retry_config:
  max_retries: 3
  retry_on_status_codes: [429, 500, 502, 503, 504]
  initial_delay_ms: 1000
  max_delay_ms: 60000
  exponential_base: 2
  jitter: true
```

## Cost Management

### Pricing (Approximate - Check OpenAI for current rates)

**GPT-4 Turbo:**
- Input: $10 per 1M tokens
- Output: $30 per 1M tokens

**GPT-4:**
- Input: $30 per 1M tokens
- Output: $60 per 1M tokens

**GPT-3.5 Turbo:**
- Input: $0.50 per 1M tokens
- Output: $1.50 per 1M tokens

**Embeddings:**
- $0.10 per 1M tokens

**DALL-E 3:**
- Standard: $0.040 per image (1024x1024)
- HD: $0.080 per image (1024x1024)

### Cost Calculator

```python
OPENAI_PRICING = {
    "gpt-4-turbo-preview": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015}
}

def calculate_cost(model, input_tokens, output_tokens):
    pricing = OPENAI_PRICING.get(model, {"input": 0, "output": 0})
    cost = (input_tokens / 1000) * pricing["input"]
    cost += (output_tokens / 1000) * pricing["output"]
    return cost

# Example
cost = calculate_cost("gpt-4-turbo-preview", 1000, 500)
print(f"Cost: ${cost:.4f}")
```

### Token Management

```python
# Estimate tokens before API call
import tiktoken

def estimate_tokens(text, model="gpt-4"):
    encoding = tiktoken.encoding_for_model(model)
    tokens = len(encoding.encode(text))
    return tokens

prompt = "Your long prompt here..."
estimated_tokens = estimate_tokens(prompt)
print(f"Estimated tokens: {estimated_tokens}")

# Adjust max_tokens to control output cost
max_output_tokens = min(1000, 8000 - estimated_tokens)
```

### Budget Alerts

```python
# Set monthly budget for OpenAI
import requests

requests.post(
    "https://mgmt.waddleai.com/api/budgets",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "provider": "openai",
        "monthly_limit_usd": 10000,
        "alert_threshold_percent": 80,
        "alert_email": "finance@company.com"
    }
)
```

## Routing Configuration

Configure intelligent routing to OpenAI models:

```python
routing_instructions = """
Route requests to OpenAI models based on these rules:

1. Use GPT-4 Turbo for:
   - Complex reasoning and analysis
   - Creative writing tasks
   - Tasks requiring latest features (vision, JSON mode)
   - When cost is not primary concern

2. Use GPT-4 for:
   - Tasks requiring highest accuracy
   - Complex code generation
   - When proven track record is needed

3. Use GPT-3.5 Turbo for:
   - Simple queries
   - Code completion
   - High-volume applications
   - Cost-sensitive workloads

4. Prefer OpenAI for:
   - Function calling requirements
   - JSON mode responses
   - Vision tasks (GPT-4V)
   - Embeddings generation
   - Multi-modal tasks
"""

requests.post(
    "https://mgmt.waddleai.com/api/routing/instructions",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "instructions": routing_instructions,
        "routing_llm": "llama3.2:1b"
    }
)
```

## Monitoring Usage

### View OpenAI-Specific Usage

```python
import requests

usage = requests.get(
    "https://mgmt.waddleai.com/api/usage?days=30",
    headers={"Authorization": f"Bearer {token}"}
).json()

openai_usage = usage["provider_usage"].get("openai", {})
print(f"OpenAI tokens: {openai_usage['tokens']}")
print(f"OpenAI requests: {openai_usage['requests']}")

# Calculate approximate cost
cost = calculate_cost_from_usage(openai_usage)
print(f"Estimated cost: ${cost:.2f}")
```

### Model-Specific Analytics

```python
# Get breakdown by model
analytics = requests.get(
    "https://mgmt.waddleai.com/api/analytics/models?provider=openai",
    headers={"Authorization": f"Bearer {token}"}
).json()

for model, stats in analytics.items():
    print(f"{model}: {stats['requests']} requests, {stats['tokens']} tokens")
```

## Troubleshooting

### Connection Issues

```bash
# Test OpenAI connectivity
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

### Authentication Errors

**Problem:** 401 Incorrect API key

**Solution:**
1. Verify API key starts with `sk-`
2. Check key hasn't been revoked
3. Verify account has credits
4. Check organization ID if using multiple orgs

### Rate Limit Errors

**Problem:** 429 Rate limit exceeded

**Solution:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10)
)
def call_openai_with_retry(messages):
    return client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=messages
    )
```

### Context Length Errors

**Problem:** 400 This model's maximum context length

**Solution:**
```python
def truncate_conversation(messages, max_tokens=8000):
    """Keep only recent messages that fit in context"""
    encoding = tiktoken.encoding_for_model("gpt-4")

    total_tokens = 0
    truncated = []

    # Always keep system message
    if messages[0]["role"] == "system":
        truncated.append(messages[0])
        total_tokens += len(encoding.encode(messages[0]["content"]))
        messages = messages[1:]

    # Add messages from most recent backwards
    for msg in reversed(messages):
        msg_tokens = len(encoding.encode(msg["content"]))
        if total_tokens + msg_tokens > max_tokens:
            break
        truncated.insert(1, msg)  # Insert after system message
        total_tokens += msg_tokens

    return truncated
```

### Function Call Errors

**Problem:** Function calling not working

**Solution:**
1. Ensure using compatible model (gpt-4-turbo, gpt-3.5-turbo)
2. Verify tools schema is valid JSON Schema
3. Check function names match exactly
4. Include proper descriptions

## Best Practices

### 1. Model Selection

```python
def select_model(task_type, max_budget_per_request=0.10):
    """Smart model selection based on task and budget"""

    if task_type == "simple_query":
        return "gpt-3.5-turbo"
    elif task_type == "code_generation":
        return "gpt-4-turbo-preview" if max_budget_per_request > 0.05 else "gpt-3.5-turbo"
    elif task_type == "complex_reasoning":
        return "gpt-4-turbo-preview"
    elif task_type == "vision":
        return "gpt-4-vision-preview"
    else:
        return "gpt-3.5-turbo"  # Default to economical option
```

### 2. Prompt Engineering

```python
# Good: Clear, specific prompt
prompt = """
Task: Refactor this Python function

Code:
def calc(a, b, c):
    x = a + b
    y = x * c
    return y

Requirements:
1. Add type hints
2. Improve variable names
3. Add docstring
4. Add input validation

Provide only the refactored code.
"""

# Bad: Vague prompt
prompt = "make this code better: def calc(a,b,c): return (a+b)*c"
```

### 3. Temperature Control

```python
# Deterministic (code, structured data)
response = client.chat.completions.create(
    model="gpt-4",
    temperature=0.2,
    messages=messages
)

# Creative (writing, brainstorming)
response = client.chat.completions.create(
    model="gpt-4",
    temperature=0.8,
    messages=messages
)
```

### 4. Error Handling

```python
import openai
import logging

def safe_completion(messages, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4-turbo-preview",
                messages=messages,
                timeout=30
            )
            return response.choices[0].message.content

        except openai.RateLimitError:
            wait_time = 2 ** attempt
            logging.warning(f"Rate limited, waiting {wait_time}s")
            time.sleep(wait_time)

        except openai.APIError as e:
            logging.error(f"OpenAI API error: {e}")
            if attempt == max_retries - 1:
                raise

        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            raise

    return None
```

### 5. Caching

```python
from functools import lru_cache
import hashlib

@lru_cache(maxsize=1000)
def cached_completion(prompt_hash, model="gpt-3.5-turbo"):
    """Cache responses for repeated prompts"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

def get_completion(prompt, model="gpt-3.5-turbo"):
    prompt_hash = hashlib.md5(f"{prompt}{model}".encode()).hexdigest()
    return cached_completion(prompt_hash, model)
```

## Security Considerations

### API Key Management

```bash
# Store in environment variable
export OPENAI_API_KEY="sk-..."

# Or use secret manager (AWS Secrets Manager example)
aws secretsmanager get-secret-value \
  --secret-id openai-api-key \
  --query SecretString \
  --output text
```

### Request Logging

```yaml
logging:
  openai:
    log_requests: true
    log_responses: false  # Don't log actual completions
    log_metadata: true
    mask_api_key: true
```

### Content Filtering

```python
# Enable OpenAI's content filtering
response = client.chat.completions.create(
    model="gpt-4-turbo-preview",
    messages=messages,
    # OpenAI automatically applies content filtering
)

# Check if content was flagged
if hasattr(response.choices[0], 'finish_reason'):
    if response.choices[0].finish_reason == 'content_filter':
        print("Content was filtered")
```

## Next Steps

- [Anthropic Configuration](anthropic-config.md)
- [Ollama Setup](ollama-setup.md)
- [Management API](../api/management-api.md)
- [Cost Optimization](../administration/quota-management.md)

## Support

For OpenAI integration support:
- OpenAI Docs: https://platform.openai.com/docs
- OpenAI Community: https://community.openai.com
- OpenAI Status: https://status.openai.com
- WaddleAI Support: support@waddleai.com
