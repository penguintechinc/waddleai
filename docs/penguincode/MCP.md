# Model Context Protocol (MCP) Integration Guide

## What is MCP?

The **Model Context Protocol (MCP)** is a standardized, open protocol for connecting AI systems to external data sources and tools. MCP enables PenguinCode to extend its capabilities by integrating with specialized services without embedding them directly.

**Key benefits:**
- **Modular architecture**: Tools run as independent processes
- **Protocol-agnostic**: Supports stdio and HTTP transports
- **Scalable**: Add new tools without modifying core code
- **Flexible**: Mix specialized and custom tools seamlessly

Learn more: https://modelcontextprotocol.io/

## How PenguinCode Uses MCP

PenguinCode extends functionality through MCP servers that provide specialized tools:

```
PenguinCode ─────┬─────→ DuckDuckGo (Search)
                 ├─────→ SearXNG (Meta-search)
                 ├─────→ N8N (Workflow automation)
                 ├─────→ Flowise (AI workflows)
                 └─────→ Custom Servers
```

**Core components:**
- `MCPClient`: Stdio-based communication (subprocess)
- `HTTPMCPClient`: HTTP-based communication (remote services)
- Tool discovery and invocation
- Automatic lifecycle management

## Configuration in config.yaml

Enable MCP and configure servers:

```yaml
mcp:
  enabled: true              # Master enable/disable
  servers: []                # List of server configurations
```

### Server Configuration Schema

```yaml
mcp:
  servers:
    - name: "unique-id"           # Server identifier
      transport: "stdio"           # "stdio" or "http"

      # Stdio transport properties
      command: "npx"               # Executable to run
      args: ["-y", "package"]      # Command arguments
      env:                         # Environment variables
        VAR_NAME: "${ENV_VAR}"
      timeout: 30                  # Request timeout (seconds)

      # HTTP transport properties
      url: "http://localhost:8080" # Server URL
      headers:                     # HTTP headers (auth, custom)
        Authorization: "Bearer ${API_TOKEN}"
      timeout: 30                  # Request timeout (seconds)
```

## Supported Transports

### Stdio Transport

Command-line servers communicating via stdin/stdout using JSON-RPC.

**Characteristics:**
- Automatic server startup/shutdown
- Built-in process lifecycle management
- No network overhead
- JSON-RPC 2.0 protocol

**When to use:**
- Official npm/pip MCP packages
- Local development
- Ephemeral server needs

**Example:**
```yaml
- name: "duckduckgo"
  transport: "stdio"
  command: "npx"
  args: ["-y", "@nickclyde/duckduckgo-mcp-server"]
  timeout: 30
```

### HTTP Transport

HTTP endpoint-based servers with REST-like interface.

**Characteristics:**
- Server independence
- Network transparency
- Custom authentication support
- Persistent across sessions

**When to use:**
- Hosted services (N8N, Flowise)
- Custom HTTP servers
- Authentication required
- Server must persist

**Example:**
```yaml
- name: "custom-api"
  transport: "http"
  url: "http://localhost:8080"
  headers:
    Authorization: "Bearer ${API_TOKEN}"
  timeout: 30
```

## Example: DuckDuckGo Search via MCP

Search engine integration using stdio MCP server.

**Configuration:**
```yaml
mcp:
  enabled: true
  servers:
    - name: "duckduckgo"
      transport: "stdio"
      command: "npx"
      args: ["-y", "@nickclyde/duckduckgo-mcp-server"]
      timeout: 30

research:
  engine: "duckduckgo"
  use_mcp: true
  max_results: 5
```

**Implementation details** (see `tools/engines/duckduckgo_mcp.py`):
1. Initializes `MCPClient` with server command
2. Starts subprocess on first search
3. Calls `duckduckgo_search` tool via JSON-RPC
4. Parses results into `SearchResult` objects
5. Manages process lifecycle

**Requirements:** Node.js installed

## Example: SearXNG Search via MCP

Privacy-focused meta-search aggregating multiple providers.

**Configuration:**
```yaml
mcp:
  enabled: true
  servers:
    - name: "searxng"
      transport: "stdio"
      command: "uvx"
      args: ["mcp-searxng"]
      env:
        SEARXNG_URL: "https://searx.be"
      timeout: 30

research:
  engine: "searxng"
  use_mcp: true
  engines:
    searxng:
      url: "https://searx.be"
      categories: ["general"]
```

**Implementation** (see `tools/engines/searxng_mcp.py`):
- Configurable SearXNG instance URL
- Supports multiple search categories
- Safe search enabled by default
- Environment variable configuration

**Requirements:** Python with `uv` package manager

## Example: N8N Workflow Automation

Integrate N8N for complex multi-step automation.

**Setup:**
```bash
npm install -g n8n
n8n start      # Runs on localhost:5678
```

**Configuration:**
```yaml
mcp:
  servers:
    - name: "n8n"
      transport: "http"
      url: "http://localhost:5678/mcp"
      headers:
        X-N8N-API-KEY: "${N8N_API_KEY}"
      timeout: 60
```

**Environment:**
```bash
export N8N_API_KEY="your-api-key-from-n8n-ui"
```

**Use cases:**
- Email automation (send, parse)
- Database operations (insert, update, query)
- Slack/Teams notifications
- Data transformations
- Webhook integrations

**Tool pattern:**
```
PenguinCode → N8N MCP Endpoint → N8N Workflows → External APIs
```

## Example: Flowise AI Workflow Integration

Visual AI workflow builder for specialized chains.

**Setup:**
```bash
npm install -g flowise
flowise start  # Runs on localhost:3000
```

**Configuration:**
```yaml
mcp:
  servers:
    - name: "flowise"
      transport: "http"
      url: "http://localhost:3000/api/v1/mcp"
      headers:
        Authorization: "Bearer ${FLOWISE_API_KEY}"
      timeout: 60
```

**Environment:**
```bash
export FLOWISE_API_KEY="key-from-flowise-settings"
```

**Capabilities:**
- Chain multiple LLMs (cascading)
- Retrieval-augmented generation (RAG)
- Custom prompt engineering
- Memory and conversation history
- Multi-turn interactions

## Example: Custom HTTP MCP Server with Auth

Build a custom HTTP server with Bearer token authentication.

**FastAPI server example:**
```python
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthCredential

app = FastAPI()
security = HTTPBearer()

async def verify_token(credentials: HTTPAuthCredential = Depends(security)):
    if credentials.credentials != "${EXPECTED_TOKEN}":
        raise HTTPException(status_code=403, detail="Invalid token")
    return credentials

@app.post("/tools/call")
async def call_tool(request: dict, _=Depends(verify_token)):
    tool_name = request["name"]
    arguments = request["arguments"]

    if tool_name == "my_tool":
        result = process_tool(arguments)
        return {"result": result}

    return {"error": "Unknown tool"}

@app.get("/tools/list")
async def list_tools(_=Depends(verify_token)):
    return {
        "tools": [
            {
                "name": "my_tool",
                "description": "Custom tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}}
                }
            }
        ]
    }
```

**PenguinCode config:**
```yaml
mcp:
  servers:
    - name: "custom-server"
      transport: "http"
      url: "http://localhost:8080"
      headers:
        Authorization: "Bearer ${CUSTOM_MCP_TOKEN}"
      timeout: 30
```

**Environment:**
```bash
export CUSTOM_MCP_TOKEN="secure-bearer-token"
```

## Creating Custom MCP Servers

### Stdio Server Template

JSON-RPC 2.0 server reading from stdin, writing to stdout.

```python
import json
import sys

def list_tools():
    """Return available tools."""
    return {
        "tools": [
            {
                "name": "my_tool",
                "description": "Does something useful",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"]
                }
            }
        ]
    }

def call_tool(name, arguments):
    """Execute tool."""
    if name == "my_tool":
        return {"result": f"Processed: {arguments['query']}"}
    return {"error": f"Unknown tool: {name}"}

def handle_request(request):
    """Handle JSON-RPC request."""
    method = request.get("method")

    if method == "tools/list":
        return {"id": request["id"], "result": list_tools()}

    if method == "tools/call":
        name = request["params"]["name"]
        args = request["params"]["arguments"]
        return {"id": request["id"], "result": call_tool(name, args)}

    return {"id": request.get("id"), "error": "Unknown method"}

# Main loop
for line in sys.stdin:
    request = json.loads(line)
    response = handle_request(request)
    print(json.dumps(response))
```

### HTTP Server Template

FastAPI-based MCP HTTP server.

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/tools/list")
async def list_tools():
    return {
        "tools": [
            {
                "name": "my_tool",
                "description": "Does something useful",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}}
                }
            }
        ]
    }

@app.post("/tools/call")
async def call_tool(request: dict):
    tool_name = request["name"]
    arguments = request["arguments"]

    if tool_name == "my_tool":
        return {"result": f"Processed: {arguments['query']}"}

    return {"error": f"Unknown tool: {tool_name}"}
```

### Best Practices

1. **Implement both endpoints:**
   - `tools/list`: Return available tool definitions
   - `tools/call`: Execute tool with arguments

2. **Tool definitions:**
   - Include name, description, inputSchema
   - Provide clear error messages
   - Document all parameters

3. **Error handling:**
   - Catch exceptions gracefully
   - Return proper error responses
   - Include debugging information

4. **Performance:**
   - Keep requests <30 seconds
   - Implement timeouts for long operations
   - Consider caching results

5. **Authentication:**
   - Validate credentials on every request
   - Use environment variables for secrets
   - Support multiple auth methods

## Troubleshooting MCP Connections

### Server Fails to Start (Stdio)

**Error:** "MCP server not started"

**Solutions:**
```bash
# Verify command exists
npx -y @nickclyde/duckduckgo-mcp-server --help

# Check Node.js installation
node --version

# Check Python for uvx-based servers
python3 --version
uv --version
```

### Connection Refused (HTTP)

**Error:** "Connection refused" or "Network unreachable"

**Solutions:**
```bash
# Verify server is running
curl -v http://localhost:8080/tools/list

# Check port is accessible
netstat -tuln | grep 8080

# Restart server
pkill -f "n8n\|flowise"
```

### Authentication Failures

**Error:** "403 Forbidden" or "401 Unauthorized"

**Solutions:**
```bash
# Verify token is set
echo $CUSTOM_MCP_TOKEN

# Test with curl
curl -H "Authorization: Bearer $CUSTOM_MCP_TOKEN" http://localhost:8080/tools/list

# Check header format is correct (Bearer prefix, space)
```

### Timeout Errors

**Error:** "Request timeout" or "Tool call timeout"

**Solutions:**
```yaml
# Increase timeout in config
mcp:
  servers:
    - name: "slow-server"
      timeout: 60  # Increase from default 30
```

```bash
# Check server logs
docker logs flowise  # or relevant container
```

### Tool Call Failures

**Error:** "MCP tool call error" with details

**Solutions:**
1. Verify tool name matches server implementation
2. Check argument types and structure
3. Review server logs for error details
4. Test endpoint directly with curl/Postman

### Cleanup Stuck Servers

```bash
# Find MCP processes
ps aux | grep -E "duckduckgo|mcp|n8n|flowise"

# Kill specific server
pkill -f "@nickclyde/duckduckgo-mcp-server"

# Kill all MCP processes
pkill -f "mcp"

# Force cleanup
killall -9 npx uvx python3
```

### Debug Mode

```bash
# Enable verbose logging
export PENGUINCODE_LOG_LEVEL=debug
export MCP_DEBUG=1

# Start PenguinCode with debug output
RUST_BACKTRACE=1 penguincode --debug
```

---

**Resources:**
- [Model Context Protocol Spec](https://modelcontextprotocol.io/)
- [MCP Server Registry](https://github.com/modelcontextprotocol/servers)
- [USAGE.md](USAGE.md) - Full configuration reference
