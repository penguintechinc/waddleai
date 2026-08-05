# Tool Support in PenguinCode

## Overview

PenguinCode provides a comprehensive tool system enabling AI models to execute filesystem operations, shell commands, web searches, and maintain persistent memory. The architecture supports both **local execution** on the client machine and **remote execution** through MCP (Model Context Protocol) servers.

## Ollama Model Compatibility

### Native Tool Calling Support

The following Ollama models natively support tool calling through JSON function definitions:

| Model | Size Variants | Support Level | Notes |
|-------|--------------|---------------|-------|
| **llama3.2** | 1B, 3B, 8B, 11B | Full | Recommended base model. Excellent instruction following. |
| **qwen2.5-coder** | 1.5B, 7B, 32B | Full | Code-aware. Default execution model in PenguinCode. |
| **mistral** | 7B, 8B | Full | Fast inference, reliable tool use. |
| **neural-chat** | 7B | Full | Conversation-optimized with tools. |
| **openhermes** | 7B | Partial | Basic tool support. |
| **deepseek-coder** | 6.7B, 33B | Full | Strong code + tool integration. |
| **command-r** | 35B, 104B | Full | Cohere's dense model. |
| **firefunction-v2** | - | Full | Specifically designed for function calling. |
| **hermes3** | 70B | Full | Fine-tuned for function calling. |

**Note**: Not all models handle tools equally. Test with your specific use case before production deployment.

## Built-in Tools

### File Operations

#### `read` - Read File Contents
**Purpose**: Read files with optional line range selection and metadata.

```json
{
  "name": "read",
  "arguments": {
    "path": "/path/to/file.py",
    "start_line": 10,
    "end_line": 20
  }
}
```

**Parameters**:
- `path` (required): Absolute or relative file path
- `start_line` (optional): Starting line number (1-indexed)
- `end_line` (optional): Ending line number (1-indexed, inclusive)

**Response**: File contents with line numbers, total line count in metadata.

#### `write` - Write/Create Files
**Purpose**: Write content to files, creating parent directories as needed.

```json
{
  "name": "write",
  "arguments": {
    "path": "/path/to/newfile.txt",
    "content": "File contents here"
  }
}
```

**Parameters**:
- `path` (required): File path
- `content` (required): Content to write
- `create_dirs` (optional, default: true): Auto-create parent directories

#### `edit` - Edit Files with Search/Replace
**Purpose**: Modify files using text search and replace.

```json
{
  "name": "edit",
  "arguments": {
    "path": "/path/to/file.py",
    "old_text": "old_function():",
    "new_text": "new_function():",
    "replace_all": false
  }
}
```

**Parameters**:
- `path` (required): File path
- `old_text` (required): Text to find
- `new_text` (required): Replacement text
- `replace_all` (optional, default: false): Replace all occurrences

#### `bash` - Execute Shell Commands
**Purpose**: Run shell commands with timeout and environment support.

```json
{
  "name": "bash",
  "arguments": {
    "command": "python -m pytest tests/",
    "timeout": 60,
    "env": {"PYTHONPATH": "/custom/path"}
  }
}
```

**Parameters**:
- `command` (required): Shell command
- `timeout` (optional, default: 30): Timeout in seconds
- `env` (optional): Environment variables dict

**Response**: stdout/stderr combined, exit code in metadata.

#### `grep` - Search File Contents
**Purpose**: Find patterns in files using regex.

```json
{
  "name": "grep",
  "arguments": {
    "pattern": "def.*function",
    "path": "/src",
    "case_sensitive": true,
    "max_results": 50
  }
}
```

**Parameters**:
- `pattern` (required): Regex pattern
- `path` (optional, default: "."): File or directory
- `case_sensitive` (optional, default: true): Case sensitivity
- `max_results` (optional, default: 100): Result limit

**Response**: Matching lines with file paths and line numbers.

#### `glob` - Find Files by Pattern
**Purpose**: Locate files matching glob patterns.

```json
{
  "name": "glob",
  "arguments": {
    "pattern": "**/*.py",
    "path": "/src",
    "max_results": 100
  }
}
```

**Parameters**:
- `pattern` (required): Glob pattern (e.g., "**/*.py", "src/**/*.ts")
- `path` (optional, default: "."): Base directory
- `max_results` (optional, default: 100): Result limit

**Response**: File paths relative to base directory.

### Web Tools

#### `web_search` - Search the Internet
**Purpose**: Query the web using configured search engines.

```json
{
  "name": "web_search",
  "arguments": {
    "query": "Python async programming best practices",
    "max_results": 5
  }
}
```

**Configured Engines** (from config.yaml):
- `duckduckgo` (default) - Privacy-focused
- `fireplexity` - Fast synthesis
- `sciraai` - Academic search
- `searxng` - Meta-search
- `google` - Full-featured (requires API key)

### Memory Tool

#### `memory` - Persistent Context Retrieval
**Purpose**: Store and retrieve relevant context using mem0 vector memory.

```json
{
  "name": "memory",
  "arguments": {
    "action": "search",
    "query": "user preferences for code style",
    "user_id": "session123",
    "limit": 5
  }
}
```

**Actions**:
- `search`: Find relevant memories by semantic similarity
- `add`: Store new memory entry
- `get_all`: Retrieve all memories for session
- `update`: Modify existing memory
- `delete`: Remove specific memory
- `delete_all`: Clear all session memories

**Vector Store Backends**:
- **Chroma** (default) - Local, file-based
- **Qdrant** - Dedicated vector database
- **PostgreSQL (pgvector)** - Enterprise relational + vectors

## Tool Definition Format for Ollama

When using Ollama with tool calling, use OpenAI-compatible JSON schema:

```json
{
  "model": "llama3.2:8b",
  "messages": [{"role": "user", "content": "Read the config"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read",
        "description": "Read file contents",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "File path"},
            "start_line": {"type": "integer", "description": "Start line"},
            "end_line": {"type": "integer", "description": "End line"}
          },
          "required": ["path"]
        }
      }
    }
  ]
}
```

## How ChatAgent Handles Tool Calls

The ChatAgent orchestrator processes tool calls through this workflow:

1. **Tool Definition**: Tools passed to Ollama in request
2. **Model Response**: Ollama returns structured tool_calls JSON
3. **Tool Routing**: ChatAgent identifies and executes tool
4. **Local vs Remote**: Routes to LocalToolExecutor or MCP server
5. **Result Processing**: Tool output formatted and added to context
6. **Continuation**: Model continues reasoning with results
7. **Final Response**: Model generates final answer

## Local vs Server Tool Execution

### Local Execution (Default - Development)

Tools execute on the **client machine**:

```yaml
client:
  local_tools:
    - read
    - write
    - edit
    - bash
    - grep
    - glob
```

**Advantages**:
- Direct filesystem access
- Full shell command support
- No network latency
- Immediate execution

**Security**: Subject to user's filesystem and shell permissions.

### Remote Execution (Production)

For distributed deployments, tools execute on server:

```yaml
server:
  mode: "remote"
  host: "0.0.0.0"
  port: 50051
  tls_enabled: true

client:
  server_url: "grpc://server:50051"
  local_tools: []
```

**Architecture**: Client → gRPC/TLS → Server → Tool Execution

## Adding Custom Tools

### 1. Create Tool Class

```python
from penguincode.tools.base import BaseTool, ToolResult

class MyCustomTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="my_tool",
            description="Does something useful"
        )

    async def execute(self, my_param: str, **kwargs) -> ToolResult:
        try:
            result = process_data(my_param)
            return ToolResult(
                success=True,
                data=result,
                metadata={"processed": True}
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e)
            )
```

### 2. Register with Executor

Add to `penguincode/client/tool_executor.py`:

```python
self._available_tools.append("my_tool")

elif tool_name == "my_tool":
    return await self._execute_my_tool(arguments, timeout)
```

### 3. Define Ollama Schema

```python
TOOL_SCHEMAS = {
    "my_tool": {
        "type": "function",
        "function": {
            "name": "my_tool",
            "description": "Does something useful",
            "parameters": {
                "type": "object",
                "properties": {
                    "my_param": {
                        "type": "string",
                        "description": "Parameter description"
                    }
                },
                "required": ["my_param"]
            }
        }
    }
}
```

## Tool Result Handling

All tools return the `ToolResult` dataclass:

```python
from penguincode.tools.base import ToolResult

@dataclass
class ToolResult:
    success: bool                   # Did tool succeed?
    data: Any                       # Result data
    error: Optional[str] = None     # Error message if failed
    metadata: Optional[Dict] = None # Additional metadata
```

### Example Results

**Success**:
```python
ToolResult(
    success=True,
    data="File contents here",
    metadata={"path": "/file.txt", "lines": 42}
)
```

**Error**:
```python
ToolResult(
    success=False,
    error="File not found: /missing.txt"
)
```

## Configuration Reference

From `config.yaml`:

```yaml
# Research (web search)
research:
  engine: "duckduckgo"
  use_mcp: true
  max_results: 5

# Memory (persistent context)
memory:
  enabled: true
  vector_store: "chroma"
  embedding_model: "nomic-embed-text"

# MCP Servers (extend tools)
mcp:
  enabled: true
  servers:
    - name: "duckduckgo"
      transport: "stdio"
      command: "npx"
      args: ["-y", "@nickclyde/duckduckgo-mcp-server"]

# Client (local tools)
client:
  local_tools:
    - read
    - write
    - edit
    - bash
    - grep
    - glob
```

## Best Practices

1. **Chain Tools**: Use read → grep → edit workflows for complex operations
2. **Handle Errors**: Always check `success` field in ToolResult
3. **Set Timeouts**: Use appropriate timeout for long operations (default 30s)
4. **Absolute Paths**: Use full paths to avoid ambiguity
5. **Memory Context**: Store important findings for multi-turn sessions
6. **Destructive Check**: Warn before bash operations (rm, mkfs, dd, etc.)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Tool returns empty | Check file exists and is readable |
| Command timeout | Increase timeout parameter for long tasks |
| Permission denied | Verify file permissions and user access |
| Memory disabled error | Enable memory in config.yaml |
| MCP server failed | Verify server running, check command in config |
| Model 404 error | Run `ollama pull <model>` to install |

---

**Related Documentation**:
- [Development Standards](./STANDARDS.md) - Architecture overview
- [Configuration](../config.yaml) - Tool configuration
- [Licensing](./licensing/license-server-integration.md) - Enterprise features
