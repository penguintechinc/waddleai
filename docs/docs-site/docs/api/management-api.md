# Management API Reference

WaddleAI's Management API provides comprehensive administrative functionality for managing organizations, users, API keys, LLM providers, and monitoring system health. All endpoints require authentication via JWT tokens or API keys.

## Base URL

```
https://your-waddleai-mgmt.com
```

## Authentication

All API requests require authentication via Bearer token:

```bash
curl -H "Authorization: Bearer <your-admin-token>" \
  https://your-waddleai-mgmt.com/api/users
```

See [Authentication Guide](authentication.md) for details on obtaining tokens.

## Organizations Management

### List Organizations

```http
GET /api/organizations
```

**Required Permission:** `admin`

**Response:**
```json
{
  "organizations": [
    {
      "id": 1,
      "name": "Acme Corp",
      "description": "Corporate AI usage",
      "token_quota_daily": 100000,
      "token_quota_monthly": 3000000,
      "enabled": true,
      "created_at": "2025-09-01T00:00:00Z",
      "updated_at": "2025-09-29T12:00:00Z"
    }
  ]
}
```

### Create Organization

```http
POST /api/organizations
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "name": "New Organization",
  "description": "Organization description",
  "token_quota_daily": 10000,
  "token_quota_monthly": 300000
}
```

**Response:**
```json
{
  "id": 2,
  "status": "created"
}
```

## User Management

### List Users

```http
GET /api/users
```

**Required Permission:** `admin` (all users) or `resource_manager` (organization users only)

**Response:**
```json
{
  "users": [
    {
      "id": 1,
      "username": "john.doe",
      "email": "john@example.com",
      "role": "user",
      "organization_id": 1,
      "enabled": true,
      "created_at": "2025-09-15T10:00:00Z"
    }
  ]
}
```

### Create User

```http
POST /api/users
```

**Required Permission:** `admin` or `resource_manager`

**Request Body:**
```json
{
  "username": "jane.smith",
  "email": "jane@example.com",
  "password": "secure-password",
  "role": "user",
  "organization_id": 1
}
```

**Response:**
```json
{
  "id": 5,
  "status": "created"
}
```

**Roles:**
- `admin` - Full system access
- `resource_manager` - Organization-level management
- `reporter` - Read-only analytics and reporting
- `user` - Basic API access

## API Key Management

### List API Keys

```http
GET /api/api_keys
```

**Required Permission:** Any authenticated user (scope depends on role)

**Response:**
```json
{
  "api_keys": [
    {
      "id": 1,
      "name": "Production Key",
      "key_hash": "***REDACTED***",
      "user_id": 1,
      "organization_id": 1,
      "permissions": ["chat:completions"],
      "enabled": true,
      "rate_limit": 1000,
      "created_at": "2025-09-20T08:00:00Z",
      "expires_at": "2026-09-20T08:00:00Z"
    }
  ]
}
```

### Create API Key

```http
POST /api/api_keys
```

**Required Permission:** Any authenticated user

**Request Body:**
```json
{
  "name": "New API Key",
  "permissions": ["chat:completions"],
  "rate_limit": 1000,
  "expires_days": 365
}
```

**Response:**
```json
{
  "id": 10,
  "api_key": "<your-waddleai-key>",
  "status": "created",
  "message": "Store this key securely - it won't be shown again"
}
```

!!! warning "Security"
    API keys are only shown once upon creation. Store them securely!

### Delete API Key

```http
DELETE /api/api_keys/{key_id}
```

**Required Permission:** Key owner, `resource_manager` (for org keys), or `admin`

**Response:**
```json
{
  "status": "deleted"
}
```

!!! note
    Keys are disabled rather than deleted to preserve audit trails.

## LLM Provider Management

### List Connection Links

```http
GET /api/connection_links
```

**Required Permission:** `admin`

**Response:**
```json
{
  "connection_links": [
    {
      "id": 1,
      "name": "OpenAI Production",
      "provider": "openai",
      "endpoint_url": "https://api.openai.com/v1",
      "model_list": ["gpt-4", "gpt-3.5-turbo"],
      "enabled": true,
      "rate_limits": {
        "requests_per_minute": 500,
        "tokens_per_minute": 150000
      },
      "created_at": "2025-09-01T00:00:00Z"
    }
  ]
}
```

### Create Connection Link

```http
POST /api/connection_links
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "name": "Anthropic Production",
  "provider": "anthropic",
  "endpoint_url": "https://api.anthropic.com/v1",
  "api_key": "sk-ant-...",
  "model_list": ["claude-3-opus", "claude-3-sonnet"],
  "enabled": true,
  "rate_limits": {
    "requests_per_minute": 100
  },
  "tls_config": {
    "verify": true
  }
}
```

**Response:**
```json
{
  "id": 3,
  "status": "created"
}
```

## Usage Statistics

### Get Usage Stats

```http
GET /api/usage?days=30
```

**Required Permission:** Any authenticated user (scope depends on role)

**Query Parameters:**
- `days` (optional): Number of days to retrieve (default: 30)

**Response:**
```json
{
  "period_days": 30,
  "total_tokens": 1500000,
  "total_requests": 5420,
  "daily_usage": {
    "2025-09-29": {
      "tokens": 50000,
      "requests": 180
    }
  },
  "provider_usage": {
    "openai": {
      "tokens": 800000,
      "requests": 2800
    },
    "anthropic": {
      "tokens": 500000,
      "requests": 1750
    },
    "ollama": {
      "tokens": 200000,
      "requests": 870
    }
  },
  "recent_usage": [
    {
      "id": 1001,
      "user_id": 5,
      "organization_id": 1,
      "provider": "openai",
      "model": "gpt-4",
      "waddleai_tokens": 150,
      "llm_tokens_input": 50,
      "llm_tokens_output": 200,
      "created_at": "2025-09-29T14:30:00Z"
    }
  ]
}
```

## System Health

### Get System Health

```http
GET /api/system/health
```

**Required Permission:** `admin`

**Response:**
```json
{
  "status": "healthy",
  "checks": {
    "database": {
      "status": "healthy",
      "response_time_ms": 5
    },
    "redis": {
      "status": "healthy",
      "response_time_ms": 2
    },
    "llm_providers": {
      "status": "healthy",
      "active_providers": 3,
      "total_providers": 3
    },
    "system_resources": {
      "status": "healthy",
      "cpu_usage_percent": 45.2,
      "memory_usage_percent": 62.8
    }
  },
  "timestamp": "2025-09-29T14:45:00Z"
}
```

## Routing Configuration

### Get Routing Instructions

```http
GET /api/routing/instructions
```

**Required Permission:** Any authenticated user

**Response:**
```json
{
  "instructions": "Route to OpenAI for general queries, Anthropic for creative writing...",
  "routing_llm": "llama3.2:1b",
  "source": "redis"
}
```

### Set Routing Instructions

```http
POST /api/routing/instructions
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "instructions": "Your routing instructions here...",
  "routing_llm": "llama3.2:1b"
}
```

**Response:**
```json
{
  "status": "success",
  "instructions_length": 250,
  "routing_llm": "llama3.2:1b"
}
```

### Test Routing Decision

```http
POST /api/routing/test
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "prompt": "Write a Python function to calculate fibonacci numbers"
}
```

**Response:**
```json
{
  "prompt": "Write a Python function...",
  "routing_decision": "claude-3-sonnet",
  "routing_reasoning": "Programming task detected - routing to Claude Sonnet for code generation",
  "request_type": "programming",
  "confidence": 0.85,
  "alternative_models": ["gpt-4", "llama-70b"]
}
```

## Memory/Conversation Management

### Search Conversations

```http
GET /api/memory/conversations?query=python&limit=20
```

**Required Permission:** `admin` (all conversations) or authenticated user (own conversations)

**Query Parameters:**
- `query` (optional): Search term
- `limit` (optional): Max results (default: 20)
- `user_id` (optional, admin only): Filter by user
- `org_id` (optional, admin only): Filter by organization

**Response:**
```json
{
  "query": "python",
  "count": 5,
  "conversations": [
    {
      "id": "conv_123",
      "user_id": 5,
      "organization_id": 1,
      "created_at": "2025-09-29T10:30:00Z",
      "model_used": "claude-3-sonnet",
      "content_preview": "User: Help me write a Python function...",
      "waddleai_tokens": 150
    }
  ]
}
```

### Get Memory Stats

```http
GET /api/memory/stats
```

**Required Permission:** Any authenticated user (scope depends on role)

**Response:**
```json
{
  "scope": "system",
  "total_conversations": 1247,
  "total_users": 42,
  "storage_size_mb": 156.3,
  "oldest_conversation": "2025-01-15T08:00:00Z",
  "newest_conversation": "2025-09-29T12:00:00Z",
  "models_used": {
    "claude-3-sonnet": 450,
    "gpt-4": 320,
    "llama-70b": 477
  }
}
```

## MCP Server Management

### Get MCP Status

```http
GET /api/mcp/status
```

**Required Permission:** `admin`

**Response:**
```json
{
  "running": true,
  "host": "localhost",
  "port": 8765,
  "active_clients": 3,
  "auto_start": true
}
```

### Start MCP Server

```http
POST /api/mcp/start
```

**Required Permission:** `admin`

**Response:**
```json
{
  "status": "started",
  "message": "MCP server started on ws://localhost:8765"
}
```

### Stop MCP Server

```http
POST /api/mcp/stop
```

**Required Permission:** `admin`

**Response:**
```json
{
  "status": "stopped",
  "message": "MCP server stopped"
}
```

### List MCP Clients

```http
GET /api/mcp/clients
```

**Required Permission:** `admin`

**Response:**
```json
{
  "clients": [
    {
      "remote_address": "127.0.0.1:54321",
      "user_id": 5,
      "username": "john.doe",
      "role": "user",
      "organization_id": 1
    }
  ]
}
```

## Ollama Management

### Pull Model

```http
POST /api/ollama/pull
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "model": "llama3.2:3b"
}
```

**Response:**
```json
{
  "status": "success",
  "model": "llama3.2:3b",
  "size": "1.8GB"
}
```

### Remove Model

```http
DELETE /api/ollama/models/{model_name}
```

**Required Permission:** `admin`

**Response:**
```json
{
  "status": "removed",
  "model": "llama3.2:3b"
}
```

## Performance Monitoring

### Get XDP Status

```http
GET /api/performance/xdp
```

**Required Permission:** `admin`

**Response:**
```json
{
  "enabled": false,
  "interface": "eth0",
  "program_loaded": false,
  "af_xdp_sockets": 0,
  "rate_limits_active": 0,
  "stats": {
    "packets_total": 0,
    "packets_passed": 0,
    "packets_dropped": 0,
    "packets_rate_limited": 0,
    "bytes_processed": 0
  },
  "performance": {
    "drop_rate": 0.0,
    "throughput_mbps": 0.0
  },
  "message": "XDP not enabled. Set ENABLE_XDP=true and run as root to enable."
}
```

### Toggle XDP

```http
POST /api/performance/xdp/enable
```

**Required Permission:** `admin`

**Request Body:**
```json
{
  "enable": true
}
```

**Response:**
```json
{
  "status": "success",
  "message": "XDP acceleration enabled. Restart proxy server to apply changes."
}
```

## Error Responses

All error responses follow this format:

```json
{
  "error": {
    "type": "error_type",
    "message": "Human-readable error message",
    "details": {}
  }
}
```

### Common Error Codes

| Status Code | Error Type | Description |
|------------|------------|-------------|
| 400 | `invalid_request` | Missing or invalid parameters |
| 401 | `authentication_required` | Missing or invalid authentication |
| 403 | `insufficient_permissions` | User lacks required permissions |
| 404 | `not_found` | Resource not found |
| 429 | `rate_limit_exceeded` | Rate limit or quota exceeded |
| 500 | `internal_error` | Server error |
| 503 | `service_unavailable` | Service temporarily unavailable |

## Rate Limiting

API endpoints are rate-limited based on user role and API key configuration. Rate limit headers are included in all responses:

```http
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 998
X-RateLimit-Reset: 1696014000
```

## Pagination

Endpoints that return lists support pagination:

```http
GET /api/users?page=1&per_page=50
```

**Parameters:**
- `page`: Page number (default: 1)
- `per_page`: Items per page (default: 50, max: 200)

## Best Practices

1. **Use appropriate authentication**: API keys for programmatic access, JWT tokens for web applications
2. **Handle rate limits**: Implement exponential backoff when rate limited
3. **Monitor quotas**: Check usage regularly to avoid quota exhaustion
4. **Secure API keys**: Never commit API keys to version control
5. **Use HTTPS**: Always use encrypted connections in production
6. **Cache responses**: Cache non-sensitive data to reduce API calls
7. **Implement retries**: Retry failed requests with exponential backoff

## SDK Examples

See the [Examples](examples.md) page for code examples in Python, Node.js, and other languages.

## Support

For API support and questions:
- Check the [Troubleshooting Guide](../troubleshooting/common-issues.md)
- Review [API Examples](examples.md)
- Contact support at support@waddleai.com
