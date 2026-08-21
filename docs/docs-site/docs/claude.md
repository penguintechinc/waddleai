# Claude Integration Guide

This guide shows how to integrate WaddleAI with your applications and use it through Claude Code.

## Overview

WaddleAI is an AI proxy and management system that provides OpenAI-compatible APIs with advanced routing, security, and token management. This guide shows how to integrate WaddleAI with your applications and use it through Claude Code.

## Quick Start for Applications

### Using OpenAI-Compatible API

WaddleAI provides a fully compatible OpenAI API that can be used as a drop-in replacement:

```python
import openai

# Configure client to use WaddleAI proxy
client = openai.OpenAI(
    api_key="<your-waddleai-key>", base_url="https://your-waddleai-proxy.com/v1"
)

# Use exactly like OpenAI API
response = client.chat.completions.create(
    model="gpt-4",  # Will be routed by WaddleAI
    messages=[{"role": "user", "content": "Hello, how are you?"}],
)

print(response.choices[0].message.content)
```

### Using with Different Languages

#### Node.js
```javascript
import OpenAI from 'openai';

const openai = new OpenAI({
    apiKey: '<your-waddleai-key>',
    baseURL: 'https://your-waddleai-proxy.com/v1'
});

const completion = await openai.chat.completions.create({
    messages: [{ role: 'user', content: 'Hello!' }],
    model: 'gpt-4',
});

console.log(completion.choices[0].message.content);
```

#### cURL
Set your key first:

```bash
export WADDLEAI_API_KEY="wa-..."   # from the WaddleAI WebUI: Virtual Keys
```

```bash
curl https://your-waddleai-proxy.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Management API for Administration

The management server provides comprehensive APIs for system administration:

### Authentication
```python
import requests

# Login to get JWT token
auth_response = requests.post(
    "https://your-waddleai-mgmt.com/auth/login",
    json={"username": "admin", "password": "your-password"},
)
token = auth_response.json()["access_token"]

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
```

### Token Usage Analytics
```python
# Check WaddleAI token usage
usage = requests.get(
    "https://your-waddleai-mgmt.com/analytics/tokens/waddleai", headers=headers
).json()

print(f"Total WaddleAI tokens used: {usage['total_waddleai_tokens']}")
print(f"LLM breakdown: {usage['llm_breakdown']}")

# Check specific user usage
user_usage = requests.get(
    "https://your-waddleai-mgmt.com/analytics/tokens/123", headers=headers
).json()
```

## Role-Based API Usage

WaddleAI implements comprehensive role-based access control:

### Admin
- **Full system access** via management API
- All endpoints and functionality available
- Cross-organization visibility and control

### Resource Manager
- **Organization-scoped quota management**
- User management within assigned organizations
- Token limit control for assigned organizations

### Reporter
- **Read-only analytics and reporting** for assigned organizations
- Usage trend analysis and reporting
- Security incident reporting

### User
- **OpenAI-compatible API access only**
- Personal API key management
- Own usage statistics

## Dual Token System

WaddleAI uses a sophisticated dual token system for accurate billing and analytics:

### WaddleAI Tokens
- **Normalized billing units** across all LLM providers
- Used for quota enforcement and cost calculation
- Consistent pricing regardless of underlying LLM

### LLM Tokens
- **Raw provider token counts** (input/output)
- Used for detailed analytics and optimization
- Provider-specific insights and debugging

```python
# Usage response includes both token types
{
    "usage": {
        "prompt_tokens": 100,  # Raw LLM input tokens
        "completion_tokens": 50,  # Raw LLM output tokens
        "total_tokens": 150,  # Total LLM tokens
        "waddleai_tokens": 15,  # Normalized WaddleAI tokens
    }
}
```

## Advanced Features

### Model Routing
```python
# WaddleAI automatically routes based on your configuration
response = client.chat.completions.create(
    model="smart-router",  # Uses routing LLM to select best model
    messages=[{"role": "user", "content": "Complex reasoning task..."}],
)

# Force specific provider
response = client.chat.completions.create(
    model="ollama:llama2",  # Route to specific Ollama model
    messages=[{"role": "user", "content": "Local processing needed"}],
)
```

### Memory Integration
```python
# Enable conversation memory
response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Remember my preferences"}],
    extra_headers={
        "X-WaddleAI-Memory": "user-session-123",
        "X-WaddleAI-Memory-Type": "conversation",
    },
)
```

### Security Features
WaddleAI automatically scans all prompts for security threats:

- **Prompt injection detection**
- **Jailbreak attempt prevention**
- **Data extraction blocking**
- **Credential harvesting protection**

Security events are logged and can be monitored:

```python
# Check recent security alerts (admin/reporter only)
alerts = requests.get(
    "https://your-waddleai-proxy.com/api/security/threats", headers=headers
).json()
```

## Error Handling

### Common Error Codes
- `401` - Invalid or expired API key/token
- `403` - Insufficient permissions for operation
- `429` - Rate limit or quota exceeded
- `400` - Blocked by security scanning
- `503` - Service temporarily unavailable

### Example Error Response
```json
{
    "error": {
        "type": "quota_exceeded",
        "message": "Daily token quota exceeded",
        "details": {
            "daily_used": 10000,
            "daily_limit": 10000,
            "monthly_used": 150000,
            "monthly_limit": 200000
        }
    }
}
```

## Best Practices

### API Key Security
- Use environment variables for API keys
- Rotate keys regularly
- Use minimal required permissions
- Monitor usage patterns

### Performance Optimization
- Implement connection pooling
- Cache frequently used data
- Monitor response times
- Use appropriate models for tasks

### Cost Management
- Monitor WaddleAI token consumption
- Set appropriate quotas
- Use cheaper models when possible
- Implement usage alerts

## Support

For additional help:
- Check the full documentation at [docs.waddleai.com](https://docs.waddleai.com)
- Review troubleshooting guides
- Monitor health endpoints
- Check security logs for issues

---

*This guide covers the core integration patterns for WaddleAI. For complete API documentation, see the full documentation site.*
