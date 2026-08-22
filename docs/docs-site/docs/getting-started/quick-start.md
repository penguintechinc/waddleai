# Quick Start Guide

Get WaddleAI up and running in 5 minutes!

## Prerequisites

- Docker and Docker Compose installed
- 4GB RAM minimum
- Internet connection for pulling images

## Step 1: Clone and Configure (1 minute)

```bash
# Clone repository
git clone https://github.com/yourusername/WaddleAI.git
cd WaddleAI

# Copy environment file
cp .env.example .env.dev

# Quick edit (use defaults for testing)
nano .env.dev
```

**Minimal .env.dev configuration**:
```bash
DATABASE_URL=postgresql://waddleai:waddleai@postgres:5432/waddleai
REDIS_HOST=redis
REDIS_PORT=6379
JWT_SECRET=change_this_in_production_please_use_openssl_rand_hex
ROUTING_LLM_PROVIDER=ollama
ROUTING_LLM_MODEL=llama3.2:1b
ROUTING_LLM_ENDPOINT=http://ollama:11434
```

## Step 2: Start Services (2 minutes)

```bash
# Start all services
docker-compose -f docker-compose.env.yml up -d

# Wait for services to initialize
docker-compose -f docker-compose.env.yml logs -f
```

**Wait for these log messages**:
- `proxy_1        | Server started on port 8000`
- `management_1   | Server started on port 8001`
- `postgres_1     | database system is ready to accept connections`
- `redis_1        | Ready to accept connections`

Press `Ctrl+C` to exit logs.

## Step 3: Create Admin User (30 seconds)

```bash
# Create admin account
docker-compose -f docker-compose.env.yml exec proxy python -m management.create_admin

# Follow prompts:
# Username: admin
# Email: admin@example.com
# Password: (choose secure password)
# Organization: My Organization
```

## Step 4: Access Management Portal (30 seconds)

1. Open browser to http://localhost:8001
2. Login with admin credentials
3. Navigate to "API Keys" section
4. Click "Generate New API Key"
5. Fill in:
   - Name: "My First Key"
   - Default Model: (leave empty for routing)
   - Daily Limit: 100000
   - Click "Generate Key"
6. **IMPORTANT**: Copy the API key shown (you won't see it again!)

Example key: `<your-waddleai-key>`

## Step 5: Test Your First Request (30 seconds)

```bash
# Set your API key
export WADDLEAI_API_KEY="<your-waddleai-key>"

# Test OpenAI-compatible endpoint
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [
      {"role": "user", "content": "Say hello to WaddleAI!"}
    ]
  }'
```

**Expected response**:
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
      "content": "Hello! Welcome to WaddleAI..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 15,
    "total_tokens": 25,
    "waddleai_tokens": 30
  }
}
```

## Step 6: Configure Your First Provider (1 minute)

Before WaddleAI can route to external LLMs, you need to add provider credentials.

### Option A: Ollama (Local, Free)

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull models
ollama pull llama3.2:1b   # Fast routing model
ollama pull llama3.2:3b   # General purpose
```

In Management Portal:
1. Navigate to "LLM Providers"
2. Click "Add Provider"
3. Fill in:
   - Name: "Local Ollama"
   - Type: ollama
   - Base URL: http://host.docker.internal:11434 (or http://localhost:11434)
   - Enable: ✓
4. Click "Test" to verify connection
5. Click "Save"

### Option B: OpenAI (Paid)

In Management Portal:
1. Navigate to "LLM Providers"
2. Click "Add Provider"
3. Fill in:
   - Name: "OpenAI"
   - Type: openai
   - Base URL: https://api.openai.com/v1
   - API Key: sk-your_openai_key_here
   - Enable: ✓
4. Click "Test"
5. Click "Save"

### Option C: Anthropic Claude (Paid)

1. Navigate to "LLM Providers"
2. Click "Add Provider"
3. Fill in:
   - Name: "Anthropic"
   - Type: anthropic
   - Base URL: https://api.anthropic.com
   - API Key: <your-anthropic-key>
   - Enable: ✓
4. Click "Test"
5. Click "Save"

## Step 7: Test Intelligent Routing (30 seconds)

```bash
# Programming query (should route to CodeLlama if available)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Write a Python function to calculate fibonacci"}
    ]
  }'

# Analysis query (should route to GPT-4 if available)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Analyze the economic impact of AI"}
    ]
  }'

# Simple query (should route to Ollama)
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $WADDLEAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ]
  }'
```

Check the Management Portal → Analytics to see routing decisions!

## What's Next?

### Connect Your IDE

- [Connect Claude Code](../integrations/claude-code.md) - Use WaddleAI with Claude Code CLI
- [Connect Cursor IDE](../integrations/cursor-ide.md) - Use WaddleAI in Cursor
- [VS Code Extension](../integrations/vscode-extension.md) - Configure VS Code extensions

### Configure Routing

1. Navigate to "Routing Configuration"
2. Edit routing instructions:
   ```
   Route programming questions to codellama.
   Route data analysis questions to GPT-4.
   Route creative writing to claude-3-opus.
   Route simple questions to llama3.2:3b.
   Consider cost and response time.
   ```
3. Click "Save Instructions"
4. Click "Test Decision" to verify

### Add Team Members

1. Navigate to "Users"
2. Click "Add User"
3. Fill in details:
   - Username: teammate
   - Email: teammate@example.com
   - Role: User
   - Organization: (same as yours)
4. Set token quotas
5. User receives invitation email

### Monitor Usage

1. Navigate to "Analytics"
2. View:
   - Token usage over time
   - Cost breakdown by provider
   - Routing decision statistics
   - Performance metrics

### Explore Features

- **Memory Integration**: Enable conversation memory in Memory Configuration
- **Rate Limiting**: Set per-key rate limits in API Keys
- **Security**: Enable prompt injection detection in settings
- **Monitoring**: Access Grafana at http://localhost:3333

## Common Issues

### "Connection refused" error

```bash
# Check services are running
docker-compose -f docker-compose.env.yml ps

# Restart if needed
docker-compose -f docker-compose.env.yml restart
```

### "API key invalid" error

- Verify you copied the full API key (starts with `wai_`)
- Check key is enabled in Management Portal
- Ensure no extra spaces in Authorization header

### "No providers available" error

- Add at least one LLM provider in Management Portal
- Verify provider connection with "Test" button
- Check provider API key is valid

### Slow routing decisions

- Ensure Ollama is running and llama3.2:1b is pulled
- Check Redis is connected (routing cache)
- Verify ROUTING_LLM_ENDPOINT in .env

## Quick Commands

```bash
# View logs
docker-compose -f docker-compose.env.yml logs -f

# Restart services
docker-compose -f docker-compose.env.yml restart

# Stop services
docker-compose -f docker-compose.env.yml down

# Stop and remove data
docker-compose -f docker-compose.env.yml down -v

# Update images
docker-compose -f docker-compose.env.yml pull

# Rebuild after code changes
docker-compose -f docker-compose.env.yml up -d --build
```

## Production Checklist

Before deploying to production:

- [ ] Change all default passwords in .env
- [ ] Generate secure JWT_SECRET: `openssl rand -hex 32`
- [ ] Enable HTTPS/TLS
- [ ] Set up database backups
- [ ] Configure firewall rules
- [ ] Enable XDP acceleration (Linux)
- [ ] Set up monitoring alerts
- [ ] Review rate limits and quotas
- [ ] Enable prompt injection detection
- [ ] Set up log rotation
- [ ] Configure Redis persistence

See [Production Deployment Guide](../deployment/production-checklist.md) for details.

## Need Help?

- [Troubleshooting Guide](../troubleshooting/common-issues.md)
- [API Documentation](../api/openai-compatible.md)
- [Configuration Reference](configuration.md)
- [GitHub Issues](https://github.com/yourusername/WaddleAI/issues)
