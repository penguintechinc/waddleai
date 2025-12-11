# Configuration Reference

Complete reference for configuring WaddleAI.

## Environment Variables

### Database Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | - | PostgreSQL connection string (required) |
| `DATABASE_POOL_SIZE` | 20 | Connection pool size |
| `DATABASE_POOL_TIMEOUT` | 30 | Pool timeout in seconds |

Example:
```bash
DATABASE_URL=postgresql://user:pass@host:5432/waddleai
```

### Redis Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | localhost | Redis server host |
| `REDIS_PORT` | 6379 | Redis server port |
| `REDIS_PASSWORD` | - | Redis password (optional) |
| `REDIS_DB` | 0 | Redis database number |
| `REDIS_POOL_SIZE` | 50 | Connection pool size |

### Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_PORT` | 8000 | Proxy server port |
| `PROXY_HOST` | 0.0.0.0 | Proxy server bind address |
| `MANAGEMENT_PORT` | 8001 | Management portal port |
| `MANAGEMENT_HOST` | 0.0.0.0 | Management portal bind address |
| `WORKERS` | 4 | Number of worker processes |

### Security Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET` | - | Secret for JWT tokens (required, 32+ chars) |
| `JWT_EXPIRY_HOURS` | 24 | JWT token expiration |
| `ENABLE_PROMPT_INJECTION_DETECTION` | true | Enable security scanning |
| `MAX_REQUEST_SIZE_MB` | 10 | Maximum request size |
| `RATE_LIMIT_PER_MINUTE` | 60 | Default rate limit |
| `MAX_DAILY_TOKENS_DEFAULT` | 100000 | Default daily token limit |
| `MAX_MONTHLY_TOKENS_DEFAULT` | 3000000 | Default monthly token limit |

### Routing Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ROUTING_LLM_PROVIDER` | ollama | Provider for routing LLM |
| `ROUTING_LLM_MODEL` | llama3.2:1b | Model for routing decisions |
| `ROUTING_LLM_ENDPOINT` | http://localhost:11434 | Routing LLM endpoint |
| `ROUTING_INSTRUCTIONS` | - | Default routing instructions |
| `ROUTING_CACHE_TTL` | 300 | Cache routing decisions (seconds) |

Example routing instructions:
```bash
ROUTING_INSTRUCTIONS="Route programming queries to codellama. Route analysis to GPT-4. Route simple queries to ollama."
```

### Memory Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_MEMORY` | false | Enable mem0 integration |
| `CHROMADB_HOST` | localhost | ChromaDB server host |
| `CHROMADB_PORT` | 8000 | ChromaDB server port |
| `MEMORY_RETENTION_DAYS` | 90 | Conversation retention period |
| `MEMORY_MAX_CONVERSATIONS` | 1000 | Max conversations per user |

### MCP Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_PORT` | 8765 | MCP server port |
| `MCP_ENABLE_WEBSOCKET` | true | Enable WebSocket support |
| `MCP_MAX_CONNECTIONS` | 100 | Maximum concurrent connections |
| `MCP_TIMEOUT` | 30 | Request timeout (seconds) |
| `MCP_REQUIRE_AUTH` | true | Require authentication |

### XDP Configuration (Linux only)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_XDP` | false | Enable XDP acceleration |
| `XDP_INTERFACE` | eth0 | Network interface for XDP |
| `XDP_RATE_LIMIT_PPS` | 10000 | Packets per second limit |
| `XDP_MODE` | native | XDP mode (native/skb/offload) |

### Monitoring Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_METRICS` | true | Enable Prometheus metrics |
| `METRICS_PORT` | 9090 | Prometheus metrics port |
| `LOG_LEVEL` | INFO | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `LOG_FORMAT` | json | Log format (json/text) |

## Configuration Files

### Routing Instructions (Redis)

Stored in Redis key: `waddleai:routing_instructions`

Set via Management Portal or API:

```bash
curl -X POST http://localhost:8001/api/routing/instructions \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instructions": "Route programming to codellama. Route analysis to GPT-4."
  }'
```

### Provider Configuration (Database)

Managed via Management Portal or API:

```python
{
  "name": "OpenAI Production",
  "provider_type": "openai",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-...",
  "enabled": true,
  "priority": 1,
  "config": {
    "timeout": 60,
    "max_retries": 3,
    "models": ["gpt-4", "gpt-3.5-turbo"]
  }
}
```

### User Defaults

Set per user/organization in database:

```python
{
  "default_model": "gpt-3.5-turbo",
  "daily_token_limit": 50000,
  "monthly_token_limit": 1000000,
  "rate_limit_per_minute": 100,
  "allowed_providers": ["openai", "ollama"]
}
```

## Docker Compose Configuration

Example `docker-compose.env.yml`:

```yaml
version: '3.8'

services:
  proxy:
    build: ./proxy
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://waddleai:pass@postgres:5432/waddleai
      - REDIS_HOST=redis
      - ENABLE_XDP=false
    depends_on:
      - postgres
      - redis

  management:
    build: ./management
    ports:
      - "8001:8001"
    environment:
      - DATABASE_URL=postgresql://waddleai:pass@postgres:5432/waddleai
    depends_on:
      - postgres

  postgres:
    image: postgres:16
    environment:
      - POSTGRES_DB=waddleai
      - POSTGRES_USER=waddleai
      - POSTGRES_PASSWORD=pass
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

## Security Best Practices

### Production Checklist

1. **Strong Secrets**
   ```bash
   # Generate secure JWT secret
   JWT_SECRET=$(openssl rand -hex 32)

   # Generate secure database password
   DB_PASSWORD=$(openssl rand -base64 24)
   ```

2. **TLS/SSL**
   - Use HTTPS for all external connections
   - Configure TLS certificates
   - Enable HTTP Strict Transport Security (HSTS)

3. **Firewall Rules**
   ```bash
   # Allow only necessary ports
   ufw allow 443/tcp   # HTTPS
   ufw allow 8000/tcp  # Proxy (behind reverse proxy)
   ufw deny 8001/tcp   # Management (internal only)
   ```

4. **Rate Limiting**
   ```bash
   RATE_LIMIT_PER_MINUTE=60
   RATE_LIMIT_PER_HOUR=1000
   RATE_LIMIT_PER_DAY=10000
   ```

5. **Security Scanning**
   ```bash
   ENABLE_PROMPT_INJECTION_DETECTION=true
   PROMPT_INJECTION_ACTION=block  # or sanitize/log
   ```

## Performance Tuning

### Database Optimization

```bash
DATABASE_POOL_SIZE=50
DATABASE_POOL_TIMEOUT=30
DATABASE_MAX_OVERFLOW=10
```

### Redis Optimization

```bash
REDIS_POOL_SIZE=100
REDIS_SOCKET_KEEPALIVE=true
REDIS_SOCKET_TIMEOUT=5
```

### Worker Configuration

```bash
WORKERS=8  # 2x CPU cores
THREADS_PER_WORKER=4
MAX_REQUESTS_PER_WORKER=1000
```

### XDP Acceleration

```bash
ENABLE_XDP=true
XDP_INTERFACE=eth0
XDP_MODE=native  # Fastest
XDP_RATE_LIMIT_PPS=50000
```

## Monitoring Configuration

### Prometheus Metrics

```bash
ENABLE_METRICS=true
METRICS_PORT=9090
METRICS_PATH=/metrics
```

### Logging

```bash
LOG_LEVEL=INFO
LOG_FORMAT=json
LOG_FILE=/var/log/waddleai/proxy.log
LOG_ROTATION=daily
LOG_RETENTION_DAYS=30
```

### Health Checks

```bash
HEALTH_CHECK_INTERVAL=30
HEALTH_CHECK_TIMEOUT=5
HEALTH_CHECK_RETRIES=3
```

## Example Configurations

### Development

```bash
DATABASE_URL=postgresql://localhost/waddleai_dev
REDIS_HOST=localhost
ENABLE_XDP=false
LOG_LEVEL=DEBUG
ENABLE_MEMORY=false
```

### Production

```bash
DATABASE_URL=postgresql://waddleai:$DB_PASS@db-prod:5432/waddleai
REDIS_HOST=redis-prod
REDIS_PASSWORD=$REDIS_PASS
ENABLE_XDP=true
LOG_LEVEL=INFO
ENABLE_MEMORY=true
JWT_SECRET=$JWT_SECRET
RATE_LIMIT_PER_MINUTE=1000
```

### High Availability

```bash
DATABASE_URL=postgresql://waddleai:$DB_PASS@db-cluster:5432/waddleai?target_session_attrs=read-write
REDIS_HOST=redis-sentinel
REDIS_SENTINEL_SERVICE=mymaster
WORKERS=16
DATABASE_POOL_SIZE=100
```

## Next Steps

- [API Documentation](../api/openai-compatible.md)
- [Deployment Guide](../deployment/docker-compose.md)
- [Troubleshooting](../troubleshooting/common-issues.md)