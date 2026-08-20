# Anthropic (Claude) Configuration

This guide covers configuring WaddleAI to connect to Anthropic's Claude API, including API keys, model selection, rate limiting, and best practices.

## Prerequisites

- Active Anthropic API account
- Anthropic API key with appropriate credits
- WaddleAI management portal access (admin role)

## Getting Anthropic API Key

1. Visit [Anthropic Console](https://console.anthropic.com/)
2. Sign in or create an account
3. Navigate to **API Keys** section
4. Click **Create Key**
5. Name your key (e.g., "WaddleAI Production")
6. Copy and securely store the API key

!!! warning "Security"
    Anthropic API keys start with `sk-ant-`. Never commit them to version control or share publicly.

## Adding Anthropic Connection in WaddleAI

### Via Management Portal

1. Log into WaddleAI Management Portal
2. Navigate to **Admin > LLM Providers**
3. Click **Add Provider**
4. Fill in the configuration:

**Provider Configuration:**

| Field | Value |
|-------|-------|
| Name | Anthropic Production |
| Provider | anthropic |
| Endpoint URL | https://api.anthropic.com/v1 |
| API Key | sk-ant-your-api-key-here |
| Enabled | ✓ |

**Model List:**
```
claude-3-opus-20240229
claude-3-sonnet-20240229
claude-3-haiku-20240307
claude-2.1
claude-instant-1.2
```

5. Click **Save**

### Via API

```bash
curl -X POST https://your-waddleai-mgmt.com/api/connection_links \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Anthropic Production",
    "provider": "anthropic",
    "endpoint_url": "https://api.anthropic.com/v1",
    "api_key": "sk-ant-your-api-key-here",
    "model_list": [
      "claude-3-opus-20240229",
      "claude-3-sonnet-20240229",
      "claude-3-haiku-20240307",
      "claude-2.1",
      "claude-instant-1.2"
    ],
    "enabled": true,
    "rate_limits": {
      "requests_per_minute": 50,
      "tokens_per_minute": 100000
    }
  }'
```

### Via Configuration File

**config/providers.yaml:**

```yaml
providers:
  - name: anthropic_production
    provider: anthropic
    endpoint_url: https://api.anthropic.com/v1
    api_key: ${ANTHROPIC_API_KEY}  # From environment variable
    enabled: true
    models:
      - claude-3-opus-20240229
      - claude-3-sonnet-20240229
      - claude-3-haiku-20240307
      - claude-2.1
      - claude-instant-1.2
    rate_limits:
      requests_per_minute: 50
      tokens_per_minute: 100000
      tokens_per_day: 2000000
    retry_config:
      max_retries: 3
      initial_delay_ms: 1000
      max_delay_ms: 10000
      exponential_base: 2
```

## Available Claude Models

### Claude 3 Family (Latest)

#### Claude 3 Opus
- **Model ID:** `claude-3-opus-20240229`
- **Best for:** Complex reasoning, advanced analysis, research
- **Context window:** 200k tokens
- **Cost:** Highest tier
- **Performance:** Best reasoning and accuracy
- **Use cases:**
  - Complex code generation
  - In-depth analysis
  - Research tasks
  - Multi-step reasoning

#### Claude 3 Sonnet
- **Model ID:** `claude-3-sonnet-20240229`
- **Best for:** Balanced performance and cost
- **Context window:** 200k tokens
- **Cost:** Mid tier
- **Performance:** Strong reasoning, faster than Opus
- **Use cases:**
  - General coding tasks
  - Document analysis
  - Content generation
  - Most production workloads

#### Claude 3 Haiku
- **Model ID:** `claude-3-haiku-20240307`
- **Best for:** Fast responses, high throughput
- **Context window:** 200k tokens
- **Cost:** Lowest tier
- **Performance:** Very fast, good accuracy
- **Use cases:**
  - Code completion
  - Quick queries
  - High-volume applications
  - Chatbots

### Claude 2 Family (Legacy)

#### Claude 2.1
- **Model ID:** `claude-2.1`
- **Context window:** 200k tokens
- **Cost:** Lower than Claude 3
- **Use cases:** Cost-effective for simpler tasks

#### Claude Instant 1.2
- **Model ID:** `claude-instant-1.2`
- **Context window:** 100k tokens
- **Cost:** Lowest cost option
- **Use cases:** High-volume, simple queries

## Rate Limits

Anthropic imposes rate limits based on your account tier:

### Default Limits

| Tier | Requests/min | Tokens/min | Tokens/day |
|------|--------------|------------|------------|
| Free | 5 | 20,000 | 300,000 |
| Build (Tier 1) | 50 | 100,000 | 2,000,000 |
| Scale (Tier 2) | 1,000 | 400,000 | 10,000,000 |
| Enterprise | Custom | Custom | Custom |

### Configure Rate Limits in WaddleAI

```json
{
  "rate_limits": {
    "requests_per_minute": 50,
    "tokens_per_minute": 100000,
    "tokens_per_day": 2000000,
    "concurrent_requests": 10
  }
}
```

WaddleAI will automatically:
- Track usage against limits
- Queue requests when limits approached
- Implement exponential backoff on rate limit errors
- Return 429 status when limits exceeded

## Using Claude via WaddleAI

### OpenAI-Compatible API

Claude is accessible through WaddleAI's OpenAI-compatible endpoint:

```python
import openai

client = openai.OpenAI(api_key="wa-your-api-key", base_url="https://your-waddleai-proxy.com/v1")

response = client.chat.completions.create(
    model="claude-3-sonnet-20240229",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
    max_tokens=1000,
)

print(response.choices[0].message.content)
```

### Direct API Call

Set your key first:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

```bash
curl https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-sonnet-20240229",
    "messages": [
      {"role": "user", "content": "Write a Python function to sort a list"}
    ],
    "max_tokens": 1000
  }'
```

### With System Prompts

Claude excels with well-crafted system prompts:

```python
response = client.chat.completions.create(
    model="claude-3-sonnet-20240229",
    messages=[
        {
            "role": "system",
            "content": "You are an expert Python developer specializing in clean, efficient code following PEP 8 standards.",
        },
        {
            "role": "user",
            "content": "Write a function to calculate fibonacci numbers with memoization",
        },
    ],
    max_tokens=1500,
    temperature=0.7,
)
```

## Advanced Configuration

### Custom Headers

```yaml
providers:
  - name: anthropic_production
    provider: anthropic
    endpoint_url: https://api.anthropic.com/v1
    api_key: ${ANTHROPIC_API_KEY}
    custom_headers:
      anthropic-version: "2023-06-01"
      anthropic-beta: "tools-2024-05-16"
```

### Timeout Configuration

```yaml
timeout_config:
  connect_timeout_ms: 5000
  read_timeout_ms: 60000
  write_timeout_ms: 5000
```

### TLS Configuration

```yaml
tls_config:
  verify: true
  ca_bundle: /path/to/ca-bundle.crt
  client_cert: /path/to/client-cert.pem
  client_key: /path/to/client-key.pem
```

### Retry Logic

```yaml
retry_config:
  max_retries: 3
  retry_on_status_codes: [429, 500, 502, 503, 504]
  initial_delay_ms: 1000
  max_delay_ms: 10000
  exponential_base: 2
  jitter: true
```

## Cost Management

### Token Estimation

Claude uses a different tokenization than GPT models. Estimate tokens:

```python
import anthropic

# Rough estimate: 1 token ≈ 4 characters for English text
text = "Your prompt here"
estimated_tokens = len(text) / 4

print(f"Estimated tokens: {estimated_tokens}")
```

### Cost Calculator

```python
# Claude 3 pricing (approximate)
CLAUDE_PRICING = {
    "claude-3-opus-20240229": {
        "input": 15.00 / 1_000_000,  # per token
        "output": 75.00 / 1_000_000,
    },
    "claude-3-sonnet-20240229": {"input": 3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    "claude-3-haiku-20240307": {"input": 0.25 / 1_000_000, "output": 1.25 / 1_000_000},
}


def calculate_cost(model, input_tokens, output_tokens):
    pricing = CLAUDE_PRICING[model]
    input_cost = input_tokens * pricing["input"]
    output_cost = output_tokens * pricing["output"]
    return input_cost + output_cost


# Example
cost = calculate_cost("claude-3-sonnet-20240229", 1000, 500)
print(f"Cost: ${cost:.4f}")
```

### Budget Alerts

Configure in WaddleAI management portal:

```python
# Set monthly budget for Anthropic
import requests

requests.post(
    "https://mgmt.waddleai.com/api/budgets",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={
        "provider": "anthropic",
        "monthly_limit_usd": 5000,
        "alert_threshold_percent": 80,
        "alert_email": "finance@company.com",
    },
)
```

## Routing Configuration

Configure WaddleAI to intelligently route to Claude:

```python
# Set routing instructions (admin only)
import requests

routing_instructions = """
Route requests to Anthropic Claude models based on these rules:

1. Use Claude 3 Opus for:
   - Complex reasoning tasks
   - Research and analysis
   - Multi-step problem solving
   - Tasks requiring highest accuracy

2. Use Claude 3 Sonnet for:
   - General code generation
   - Content creation
   - Document analysis
   - Most production workloads

3. Use Claude 3 Haiku for:
   - Code completion
   - Quick queries
   - High-volume applications
   - Simple Q&A

4. Prefer Claude for:
   - Creative writing
   - Long-form content
   - Constitutional AI safety requirements
   - Analysis of complex documents
"""

requests.post(
    "https://mgmt.waddleai.com/api/routing/instructions",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={"instructions": routing_instructions, "routing_llm": "llama3.2:1b"},
)
```

## Monitoring Usage

### View Anthropic-Specific Usage

```python
import requests

# Get usage stats filtered by Anthropic
usage = requests.get(
    "https://mgmt.waddleai.com/api/usage?days=30", headers={"Authorization": f"Bearer {token}"}
).json()

anthropic_usage = usage["provider_usage"].get("anthropic", {})
print(f"Anthropic tokens: {anthropic_usage['tokens']}")
print(f"Anthropic requests: {anthropic_usage['requests']}")
```

### Real-time Monitoring

```bash
# Stream usage events (admin only)
curl -N https://mgmt.waddleai.com/api/usage/stream \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | grep anthropic
```

## Troubleshooting

### Connection Issues

**Problem:** Cannot connect to Anthropic API

**Solution:**
```bash
# Test connectivity
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-3-haiku-20240307","max_tokens":10,"messages":[{"role":"user","content":"Hi"}]}'
```

### Authentication Errors

**Problem:** 401 Unauthorized from Anthropic

**Solution:**
1. Verify API key is correct
2. Check API key has credits
3. Ensure API key starts with `sk-ant-`
4. Verify account status at console.anthropic.com

### Rate Limit Errors

**Problem:** 429 Rate Limit Exceeded

**Solution:**
1. Check current rate limits in Anthropic console
2. Reduce concurrent requests in WaddleAI config
3. Implement request queuing
4. Consider upgrading Anthropic tier

### Model Not Found

**Problem:** Model not found error

**Solution:**
1. Verify model ID spelling (exact match required)
2. Check model is in enabled model list
3. Ensure model exists in Anthropic API
4. Use `claude-3-sonnet-20240229` not `claude-3-sonnet`

### Context Length Exceeded

**Problem:** 400 Bad Request - context too long

**Solution:**
```python
# Truncate messages to fit context
MAX_CONTEXT_TOKENS = 180000  # Leave room for response


def truncate_messages(messages, max_tokens):
    total_tokens = sum(len(m["content"]) / 4 for m in messages)
    if total_tokens > max_tokens:
        # Remove oldest messages first
        while total_tokens > max_tokens and len(messages) > 1:
            removed = messages.pop(0)
            total_tokens -= len(removed["content"]) / 4
    return messages


messages = truncate_messages(messages, MAX_CONTEXT_TOKENS)
```

## Best Practices

### 1. Model Selection

- **Use Haiku for**: Quick responses, high volume, cost-sensitive applications
- **Use Sonnet for**: Most production workloads, balanced cost/performance
- **Use Opus for**: Complex reasoning, research, highest accuracy requirements

### 2. Prompt Engineering

Claude responds well to clear, structured prompts:

```python
# Good: Clear, structured prompt
prompt = """Task: Generate a Python function

Requirements:
1. Calculate fibonacci numbers
2. Use memoization for efficiency
3. Include docstring
4. Add type hints
5. Follow PEP 8

Provide only the code, no explanation."""

# Bad: Vague prompt
prompt = "make a fibonacci function"
```

### 3. System Messages

Use system messages for consistent behavior:

```python
system_message = """You are a senior Python developer at ACME Corp.

Code Standards:
- Follow PEP 8
- Use type hints
- Write comprehensive docstrings
- Prefer composition over inheritance
- Write unit tests for all functions

Security:
- Validate all inputs
- Use parameterized queries
- Avoid eval() and exec()
- Implement proper error handling"""
```

### 4. Temperature Settings

Adjust temperature based on use case:

```python
# Creative writing (high variation)
response = client.chat.completions.create(
    model="claude-3-sonnet-20240229", temperature=0.9, messages=[...]
)

# Code generation (low variation, deterministic)
response = client.chat.completions.create(
    model="claude-3-sonnet-20240229", temperature=0.2, messages=[...]
)

# Balanced (default)
response = client.chat.completions.create(
    model="claude-3-sonnet-20240229", temperature=0.7, messages=[...]
)
```

### 5. Error Handling

```python
import openai
from tenacity import retry, stop_after_attempt, wait_exponential


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def call_claude(messages):
    try:
        response = client.chat.completions.create(
            model="claude-3-sonnet-20240229", messages=messages, max_tokens=1000
        )
        return response.choices[0].message.content
    except openai.RateLimitError:
        print("Rate limited, retrying...")
        raise
    except openai.APIError as e:
        print(f"API error: {e}")
        raise
    except Exception as e:
        print(f"Unexpected error: {e}")
        return None
```

## Security Considerations

### API Key Rotation

```bash
#!/bin/bash
# rotate-anthropic-key.sh

# Generate new key in Anthropic console first
NEW_KEY="sk-ant-new-key-here"

# Update in WaddleAI
curl -X PATCH https://mgmt.waddleai.com/api/connection_links/anthropic \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"api_key\": \"$NEW_KEY\"}"

echo "Anthropic API key updated"
```

### Network Security

```yaml
# Restrict to Anthropic IPs only (if using firewall)
allowed_ips:
  - 54.0.0.0/8      # AWS us-east-1
  - 52.0.0.0/8      # AWS us-west-2
  # Add other Anthropic IP ranges
```

### Audit Logging

Enable detailed logging for Claude usage:

```yaml
logging:
  anthropic:
    level: INFO
    log_requests: true
    log_responses: false  # Don't log response content
    log_metadata: true
    retention_days: 90
```

## Next Steps

- [OpenAI Configuration](openai-config.md)
- [Ollama Setup](ollama-setup.md)
- [Management API](../api/management-api.md)
- [Usage Monitoring](../administration/monitoring.md)

## Support

For Anthropic integration support:
- Anthropic Docs: https://docs.anthropic.com
- Anthropic Support: support@anthropic.com
- WaddleAI Support: support@waddleai.com
