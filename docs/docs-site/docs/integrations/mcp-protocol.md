# MCP Protocol Integration

Model Context Protocol (MCP) support in WaddleAI enables seamless integration with Claude Code, Cursor IDE, and other MCP-compatible tools.

## Overview

MCP (Model Context Protocol) is a standardized protocol for connecting AI development tools to language models. WaddleAI implements MCP to provide:

- **Bidirectional communication** - Real-time streaming between tools and LLMs
- **Context sharing** - Share workspace context, files, and metadata
- **Tool integration** - Enable AI assistants to use external tools
- **Multi-model support** - Route MCP requests to any configured LLM
- **Authentication** - Secure MCP connections with API keys

## Architecture

```
┌──────────────┐          WebSocket          ┌─────────────────┐
│ Claude Code  │◄─────────────────────────►│  WaddleAI MCP   │
│   /Cursor    │      MCP Protocol          │     Server      │
└──────────────┘                             └────────┬────────┘
                                                      │
┌──────────────┐          WebSocket          ┌───────▼─────────┐
│   VS Code    │◄─────────────────────────►│  WaddleAI Proxy │
│  Extension   │      MCP Protocol          │  (LLM Routing)  │
└──────────────┘                             └────────┬────────┘
                                                      │
                                              ┌───────▼────────┐
                                              │  OpenAI, Claude│
                                              │  Ollama, etc.  │
                                              └────────────────┘
```

## MCP Server Configuration

### Enable MCP in WaddleAI

MCP is configured in `.env.dev`:

```bash
# Auto-start MCP WebSocket server
MCP_AUTO_START=true

# MCP WebSocket port
MCP_PORT=8765

# MCP authentication (use WaddleAI API keys)
MCP_AUTH_REQUIRED=true

# MCP protocol version
MCP_VERSION=1.0.0
```

### Start MCP Server

MCP server runs alongside WaddleAI management server:

```bash
# With docker-compose
docker-compose -f docker-compose.env.yml up -d

# MCP server starts automatically on port 8765
# WebSocket endpoint: ws://localhost:8765/mcp
```

### Manual MCP Server Control

Control MCP server via management portal or API:

```bash
# Start MCP server
curl -X POST http://localhost:8001/api/mcp/control \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"action": "start", "port": 8765}'

# Stop MCP server
curl -X POST http://localhost:8001/api/mcp/control \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"action": "stop"}'

# Get MCP server status
curl http://localhost:8001/api/mcp/status \
  -H "Authorization: Bearer <your-admin-token>"
```

## Client Configuration

### Claude Code

Configure Claude Code to use WaddleAI's MCP endpoint:

**Linux/macOS**: `~/.config/claude-code/settings.json`
**Windows**: `%APPDATA%\claude-code\settings.json`

```json
{
  "mcp": {
    "servers": {
      "waddleai": {
        "url": "ws://localhost:8765/mcp",
        "apiKey": "wa-your-api-key-here",
        "name": "WaddleAI Multi-Model Proxy",
        "models": ["auto", "gpt-4", "claude-3-opus", "llama3.2"]
      }
    },
    "defaultServer": "waddleai"
  }
}
```

### Cursor IDE

Configure Cursor to use WaddleAI's MCP:

**Settings** → **Extensions** → **MCP Settings**

```json
{
  "mcp.servers": [
    {
      "name": "WaddleAI",
      "url": "ws://localhost:8765/mcp",
      "apiKey": "wa-your-api-key-here"
    }
  ],
  "mcp.defaultServer": "WaddleAI"
}
```

### VS Code Extension

Install MCP extension and configure:

```json
// settings.json
{
  "mcp.endpoint": "ws://localhost:8765/mcp",
  "mcp.apiKey": "wa-your-api-key-here",
  "mcp.autoConnect": true
}
```

### Custom Integration

Connect your own application via WebSocket:

```python
import asyncio
import websockets
import json

async def connect_mcp():
    uri = "ws://localhost:8765/mcp"
    headers = {
        "X-API-Key": "wa-your-api-key-here"
    }

    async with websockets.connect(uri, extra_headers=headers) as websocket:
        # Send MCP handshake
        await websocket.send(json.dumps({
            "type": "mcp.init",
            "version": "1.0.0",
            "capabilities": ["chat", "tools", "context"]
        }))

        # Receive server response
        response = await websocket.recv()
        print(f"Server: {response}")

        # Send chat message
        await websocket.send(json.dumps({
            "type": "mcp.chat",
            "model": "auto",  # Use intelligent routing
            "messages": [
                {"role": "user", "content": "Hello, WaddleAI!"}
            ]
        }))

        # Receive response
        async for message in websocket:
            data = json.loads(message)
            if data["type"] == "mcp.chat.response":
                print(f"Assistant: {data['content']}")
                break

asyncio.run(connect_mcp())
```

## MCP Protocol Messages

### Initialization

```json
// Client → Server
{
  "type": "mcp.init",
  "version": "1.0.0",
  "client": "claude-code/1.5.0",
  "capabilities": ["chat", "tools", "context", "streaming"]
}

// Server → Client
{
  "type": "mcp.init.ack",
  "version": "1.0.0",
  "server": "waddleai/1.0.0",
  "capabilities": ["chat", "tools", "context", "streaming", "multi-model"],
  "models": ["auto", "gpt-4", "claude-3-opus", "llama3.2", "codellama"]
}
```

### Chat Message

```json
// Client → Server
{
  "type": "mcp.chat",
  "id": "req_123",
  "model": "auto",
  "messages": [
    {"role": "user", "content": "Write a Python function to sort a list"}
  ],
  "temperature": 0.7,
  "max_tokens": 500,
  "stream": true
}

// Server → Client (streaming)
{
  "type": "mcp.chat.delta",
  "id": "req_123",
  "delta": "Here's a Python function...",
  "model_used": "codellama:34b",
  "routing_reasoning": "Programming task detected"
}

// Server → Client (complete)
{
  "type": "mcp.chat.response",
  "id": "req_123",
  "content": "Here's a Python function that sorts a list...",
  "model_used": "codellama:34b",
  "routing_decision": "codellama:34b",
  "routing_reasoning": "Programming task detected, routing to code-specialized model",
  "usage": {
    "waddleai_tokens": 150,
    "llm_tokens": {"prompt_tokens": 45, "completion_tokens": 105}
  },
  "latency_ms": 1250
}
```

### Context Sharing

```json
// Client → Server (share workspace context)
{
  "type": "mcp.context",
  "id": "ctx_123",
  "context": {
    "type": "workspace",
    "files": [
      {
        "path": "/project/main.py",
        "language": "python",
        "content": "def main():\n    pass"
      }
    ],
    "selected_text": "def main():",
    "cursor_position": {"line": 1, "column": 12}
  }
}

// Server → Client (acknowledge)
{
  "type": "mcp.context.ack",
  "id": "ctx_123",
  "stored": true
}
```

### Tool Calls

```json
// Server → Client (request tool execution)
{
  "type": "mcp.tool.call",
  "id": "tool_123",
  "tool": "read_file",
  "arguments": {
    "path": "/project/config.json"
  }
}

// Client → Server (tool result)
{
  "type": "mcp.tool.result",
  "id": "tool_123",
  "result": {
    "success": true,
    "content": "{\"api_key\": \"...\"}"
  }
}
```

### Error Handling

```json
{
  "type": "mcp.error",
  "id": "req_123",
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Daily token quota exceeded",
    "details": {
      "quota": 100000,
      "used": 100523,
      "reset_at": "2024-01-16T00:00:00Z"
    }
  }
}
```

## Features

### Model Selection via MCP

Clients can specify models in MCP messages:

```json
// Use specific model
{"model": "claude-3-opus"}

// Use intelligent routing
{"model": "auto"}

// Use header preference
// X-Preferred-Model: gpt-4
{"model": "auto"}
```

WaddleAI's routing hierarchy applies:
1. Request model parameter
2. X-Preferred-Model header
3. API key default model
4. Routing LLM decision

### Streaming Responses

Enable streaming for real-time output:

```json
{
  "type": "mcp.chat",
  "stream": true,
  "messages": [...]
}
```

Server sends incremental deltas:

```json
{"type": "mcp.chat.delta", "delta": "Here"}
{"type": "mcp.chat.delta", "delta": "'s"}
{"type": "mcp.chat.delta", "delta": " a"}
{"type": "mcp.chat.delta", "delta": " solution"}
{"type": "mcp.chat.complete", "usage": {...}}
```

### Context Persistence

WaddleAI stores MCP context in mem0:

- Workspace files
- Selected text
- Cursor positions
- Previous messages

Context is used for:
- Better routing decisions
- More accurate responses
- Cross-session continuity

### Tool Integration

MCP tools allow AI to interact with your environment:

```python
# Example: File reading tool
{
  "type": "mcp.tool.register",
  "tools": [
    {
      "name": "read_file",
      "description": "Read contents of a file",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "File path"}
        },
        "required": ["path"]
      }
    }
  ]
}
```

## Management Portal

### MCP Management Page

Access at `http://localhost:8001/mcp-management`

**Features**:
- Start/stop MCP server
- View connected clients
- Monitor MCP message traffic
- Configure MCP settings
- View error logs

### Client Monitoring

See all connected MCP clients:

```bash
curl http://localhost:8001/api/mcp/clients \
  -H "Authorization: Bearer <your-admin-token>"
```

Response:

```json
{
  "clients": [
    {
      "id": "client_abc123",
      "type": "claude-code",
      "version": "1.5.0",
      "connected_at": "2024-01-15T10:30:00Z",
      "user_id": 5,
      "api_key_id": 12,
      "messages_sent": 42,
      "messages_received": 45
    }
  ],
  "total": 1
}
```

## Security

### Authentication

MCP connections require valid WaddleAI API keys:

```json
// WebSocket header
{
  "X-API-Key": "wa-your-api-key-here"
}

// Or in handshake message
{
  "type": "mcp.init",
  "auth": {
    "type": "bearer",
    "token": "wa-your-api-key-here"
  }
}
```

### Rate Limiting

MCP connections respect API key rate limits:

- Quota checks per message
- Rate limiting at XDP layer (if enabled)
- Automatic disconnection on quota exceeded

### TLS/SSL

For production, use WSS (WebSocket Secure):

```nginx
# Nginx reverse proxy
server {
    listen 443 ssl;
    server_name waddleai.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location /mcp {
        proxy_pass http://localhost:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Client configuration:

```json
{
  "mcp": {
    "url": "wss://waddleai.example.com/mcp",
    "apiKey": "wa-your-api-key-here"
  }
}
```

## Troubleshooting

### Connection Refused

**Problem**: Cannot connect to MCP WebSocket

**Solutions**:
1. Verify MCP server is running: Check management portal
2. Check port 8765 is not blocked by firewall
3. Verify `MCP_AUTO_START=true` in `.env.dev`
4. Check MCP server logs: `docker logs waddleai-management`

### Authentication Failed

**Problem**: "Invalid API key" or 401 errors

**Solutions**:
1. Verify API key format: `wa-`
2. Check API key is active in management portal
3. Ensure API key has quota remaining
4. Verify API key in client configuration

### Messages Not Streaming

**Problem**: Responses appear all at once instead of streaming

**Solutions**:
1. Verify `"stream": true` in request
2. Check client supports streaming
3. Verify WebSocket connection is not buffered
4. Check for proxies that might buffer WebSocket messages

### Slow Responses

**Problem**: MCP messages take too long

**Solutions**:
1. Check WaddleAI routing LLM performance
2. Monitor proxy server resource usage
3. Enable XDP acceleration for better network performance
4. Use faster models for routing (llama3.2:1b)

## Best Practices

1. **Use streaming**: Enable streaming for better UX in IDEs
2. **Share context**: Send workspace context for better routing decisions
3. **Handle errors**: Implement reconnection logic for dropped connections
4. **Monitor usage**: Track MCP message counts in analytics
5. **Use intelligent routing**: Let WaddleAI's routing LLM choose the best model
6. **Secure in production**: Always use WSS with valid certificates

## See Also

- [Claude Code Integration](claude-code.md)
- [Cursor IDE Integration](cursor-ide.md)
- [VS Code Extension](vscode-extension.md)
- [Authentication Guide](../api/authentication.md)
- [MCP Protocol Specification](https://modelcontextprotocol.io)
