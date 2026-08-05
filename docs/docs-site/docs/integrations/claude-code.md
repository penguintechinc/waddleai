# Connect Claude Code to WaddleAI

Use WaddleAI as the backend for Claude Code CLI to get intelligent routing, cost tracking, and multi-provider support.

## Prerequisites

- WaddleAI running (see [Quick Start](../getting-started/quick-start.md))
- Claude Code CLI installed
- WaddleAI API key

## Configuration

### Step 1: Get Your API Key

1. Open WaddleAI Management Portal: http://localhost:8001
2. Navigate to "API Keys"
3. Click "Generate New API Key"
4. Name it "Claude Code"
5. Copy the generated key (starts with `wai_`)

### Step 2: Configure Claude Code

Create or edit `~/.config/claude/config.json`:

```json
{
  "api": {
    "type": "openai",
    "base_url": "http://localhost:8000/v1",
    "api_key": "wai_your_api_key_here"
  },
  "model": "claude-3-sonnet-20240229",
  "features": {
    "mcp": {
      "enabled": true,
      "servers": {
        "waddleai": {
          "type": "websocket",
          "url": "ws://localhost:8765"
        }
      }
    }
  }
}
```

### Step 3: Test Connection

```bash
# Test with a simple command
claude "Hello, are you using WaddleAI?"

# Check routing in Management Portal
# Navigate to Analytics → Routing Decisions
```

## Advanced Configuration

### Use WaddleAI Routing

Let WaddleAI choose the best model:

```json
{
  "api": {
    "type": "openai",
    "base_url": "http://localhost:8000/v1",
    "api_key": "wai_your_key_here"
  },
  "model": "auto",
  "routing": {
    "enabled": true,
    "prefer_local": true,
    "cost_optimization": true
  }
}
```

### Model Preferences

Set model preferences per task:

```json
{
  "models": {
    "code": "codellama",
    "chat": "claude-3-sonnet",
    "analysis": "gpt-4"
  }
}
```

### MCP Integration

Use WaddleAI's MCP endpoint for tool use:

```json
{
  "mcp": {
    "enabled": true,
    "endpoint": "ws://localhost:8765",
    "auth": {
      "type": "bearer",
      "token": "wai_your_key_here"
    },
    "tools": [
      "file_operations",
      "web_search",
      "code_execution"
    ]
  }
}
```

## Usage Examples

### Basic Usage

```bash
# Simple query (routes to fast local model)
claude "What is 2+2?"

# Code generation (routes to CodeLlama)
claude "Write a Python function to sort a list"

# Analysis (routes to GPT-4)
claude "Analyze this business plan: ..."
```

### With MCP Tools

```bash
# File operations via MCP
claude "Read the contents of README.md and summarize"

# Web search via MCP
claude "Search for the latest Python best practices"

# Code execution via MCP
claude "Run this Python code and show results"
```

### Streaming Responses

```bash
# Enable streaming for real-time output
claude --stream "Write a long story about AI"
```

## Monitoring Usage

### View Usage in Management Portal

1. Navigate to Analytics
2. Filter by your API key
3. View:
   - Token usage per request
   - Models used
   - Routing decisions
   - Cost breakdown

### CLI Usage Stats

```bash
# View token usage
claude --usage

# View last 10 requests
claude --history
```

## Cost Optimization

### Local-First Strategy

Configure to prefer local Ollama models:

```json
{
  "routing": {
    "strategy": "local_first",
    "fallback": "cloud",
    "max_cost_per_request": 0.01
  }
}
```

### Set Budget Limits

In Management Portal:
1. Edit your API key
2. Set daily/monthly limits
3. Claude Code will respect these limits

## Troubleshooting

### "Connection refused"

```bash
# Check WaddleAI is running
curl http://localhost:8000/healthz

# Verify API key
curl -H "Authorization: Bearer wai_your_key" \
  http://localhost:8000/v1/models
```

### "Model not found"

Check available models:
```bash
curl -H "Authorization: Bearer wai_your_key" \
  http://localhost:8000/v1/models
```

Add providers in Management Portal.

### Slow Responses

1. Check routing LLM is running (Ollama with llama3.2:1b)
2. Verify Redis is connected
3. Check provider response times in Analytics

## Environment Variables

Alternative to config file:

```bash
export CLAUDE_API_BASE_URL="http://localhost:8000/v1"
export CLAUDE_API_KEY="wai_your_key_here"
export CLAUDE_MODEL="auto"

claude "Hello!"
```

## Next Steps

- [Connect Cursor IDE](cursor-ide.md)
- [VS Code Extension](vscode-extension.md)
- [API Documentation](../api/openai-compatible.md)