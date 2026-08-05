# PenguinCode Usage Guide

Complete guide to installing, configuring, and using PenguinCode - an AI-powered CLI assistant using local Ollama models.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Initial Setup](#initial-setup)
- [Configuration](#configuration)
- [CLI Commands](#cli-commands)
- [Environment Variables](#environment-variables)
- [Running Modes](#running-modes)
- [GPU Optimization](#gpu-optimization)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- **Python**: 3.12+ (tested on 3.12, 3.13)
- **Ollama**: Latest from [ollama.ai](https://ollama.ai)
- **GPU**: NVIDIA with 8GB+ VRAM (optimized for RTX 4060 Ti)
- **RAM**: 16GB+ system RAM recommended
- **Disk**: 20GB+ for models

### Starting Ollama

```bash
# macOS/Linux
ollama serve

# Verify connectivity
curl http://localhost:11434/api/tags
```

---

## Installation

### From Source (Development)

```bash
git clone https://github.com/penguintechinc/penguin-code.git
cd penguin-code
pip install -e ".[dev]"
```

### From PyPI

```bash
pip install penguincode
```

### Docker

```bash
docker build -t penguincode .
docker run -it -v ~/.config/penguincode:/root/.config/penguincode penguincode
```

---

## Initial Setup

Run the interactive setup command:

```bash
penguincode setup
```

This command:
1. Creates config directories at `~/.config/penguincode/`
2. Checks Ollama connectivity
3. Pulls default models (llama3.2:3b, qwen2.5-coder:7b, nomic-embed-text)
4. Creates `.penguincode/` directory in your project

**Options:**
```bash
penguincode setup --skip-ollama-check     # Skip connection check
penguincode setup --no-pull-models        # Don't download models
penguincode setup --ollama-url http://remote:11434  # Custom Ollama URL
```

### Pull Ollama Models

**Recommended models for RTX 4060 Ti (8GB)**:

```bash
# Required models
ollama pull llama3.2:3b          # Research, orchestration (2GB)
ollama pull qwen2.5-coder:7b     # Code execution (4.7GB)
ollama pull nomic-embed-text     # Required for docs RAG (274MB)

# Optional additional models
ollama pull deepseek-coder:6.7b  # Planning, debugging (3.8GB)
ollama pull codellama:7b         # Code review (3.8GB)
ollama pull mistral:7b           # Documentation (4.1GB)
```

**Note**: `nomic-embed-text` is required for documentation RAG indexing. Without it, you'll see "Indexed 0 chunks" errors.

**Model selection by task**:
- **Chat/Research**: llama3.2:3b (fast, general purpose)
- **Code Generation**: qwen2.5-coder:7b (best code quality)
- **Planning**: deepseek-coder:6.7b (best architecture)
- **Review**: codellama:7b (code analysis)

### Install VS Code Extension

**Option 1: From Releases (Recommended)**

1. Download latest VSIX from [Releases](https://github.com/penguintechinc/penguin-code/releases)
2. In VS Code: **Extensions** → **···** → **Install from VSIX**
3. Select downloaded file
4. Restart VS Code

**Option 2: Build from Source**

```bash
cd vsix-extension
npm install
npm run compile
npx vsce package
code --install-extension penguin-code-*.vsix
```

---

## Configuration

Config file is in your project root: `./config.yaml`

### Ollama Connection

```yaml
ollama:
  api_url: "http://localhost:11434"  # Ollama API endpoint
  timeout: 120                        # Request timeout in seconds
```

### Model Roles

Assign different models for different tasks:

```yaml
models:
  planning: "deepseek-coder:6.7b"      # Complex planning tasks
  orchestration: "llama3.2:3b"         # Task coordination
  research: "llama3.2:3b"              # Web research
  execution: "qwen2.5-coder:7b"        # Code generation (complex)
  execution_lite: "qwen2.5-coder:1.5b" # Code generation (simple)
  exploration: "llama3.2:3b"           # Code exploration
  exploration_lite: "llama3.2:1b"      # Fast file reads
```

### Agent Configuration

Override models per specialized agent:

```yaml
agents:
  executor:
    model: "qwen2.5-coder:7b"
    description: "Code mutations, file writes, bash execution"
  explorer:
    model: "llama3.2:3b"
    description: "Codebase navigation, file reading"
  reviewer:
    model: "codellama:7b"
    description: "Code review and quality analysis"
  planner:
    model: "deepseek-coder:6.7b"
    description: "Implementation planning and decomposition"
  tester:
    model: "qwen2.5-coder:7b"
    description: "Test generation and execution"
  debugger:
    model: "deepseek-coder:6.7b"
    description: "Error analysis and debugging"
  docs:
    model: "mistral:7b"
    description: "Documentation generation"
```

### Generation Parameters

```yaml
defaults:
  temperature: 0.7     # 0.0-1.0 (lower=deterministic, higher=creative)
  max_tokens: 4096     # Maximum response length
  context_window: 8192 # Model's context window size
```

### Security & History

```yaml
security:
  level: 2  # 1=always prompt, 2=prompt for destructive, 3=no prompts

history:
  enabled: true
  location: "per-project"  # Store history with each project
  max_sessions: 50
```

### Research Configuration

```yaml
research:
  engine: "duckduckgo"  # duckduckgo | google | sciraai | searxng | fireplexity
  use_mcp: true         # Use MCP servers when available
  max_results: 5

  engines:
    duckduckgo:
      safesearch: "moderate"  # off, moderate, strict
      region: "wt-wt"

    google:
      api_key: "${GOOGLE_API_KEY}"
      cx_id: "${GOOGLE_CX_ID}"

    sciraai:
      api_key: "${SCIRA_API_KEY}"
      endpoint: "https://api.scira.ai"
```

### Memory & Long-term Context

```yaml
memory:
  enabled: true
  vector_store: "chroma"  # chroma | qdrant | pgvector
  embedding_model: "nomic-embed-text"  # Required for indexing

  stores:
    chroma:
      path: "./.penguincode/memory"
      collection: "penguincode_memory"
```

### Documentation RAG (Auto-indexing)

```yaml
docs_rag:
  enabled: true
  auto_detect_on_start: true    # Detect from project files
  auto_detect_on_request: true   # Detect from user queries
  auto_index_on_detect: true     # Index when detected
  auto_index_on_request: true    # Index on-demand
  cache_dir: "./.penguincode/docs"
  max_pages_per_library: 50
  max_libraries_to_index: 20
  languages_manual:
    python: false
    javascript: false
```

---

## CLI Commands

### Interactive Chat

```bash
penguincode chat                                    # Start interactive session
penguincode chat --project /path/to/project        # Specify project directory
penguincode chat --config /path/to/config.yaml     # Custom config file
penguincode chat --debug                           # Enable verbose logging
```

### Configuration Management

```bash
penguincode config show                            # Display current settings
penguincode config set --key models.planning \
  --value "deepseek-coder:6.7b"                    # Update a setting
```

### Session History

```bash
penguincode history                                # Show last 10 sessions
penguincode history --limit 50                     # Show last 50 sessions
penguincode history --project /path/to/project     # Specific project history
```

### Setup & Installation

```bash
penguincode setup                                  # Initial setup wizard
penguincode install-extension                      # Install VS Code extension
penguincode install-extension \
  --vscode-path /custom/path                       # Custom installation path
```

### Server Mode

```bash
penguincode serve                                  # Start server (port 8420)
penguincode serve --port 8421 --host 0.0.0.0      # Custom host/port
```

---

## Environment Variables

Use environment variables to set configuration without editing config.yaml. Reference them with `${VAR_NAME}` syntax:

### Core Variables

```bash
# Ollama
OLLAMA_API_URL="http://localhost:11434"
OLLAMA_TIMEOUT="120"

# Research API Keys
GOOGLE_API_KEY="your-key"
GOOGLE_CX_ID="your-cx"
SCIRA_API_KEY="your-key"
FIRECRAWL_API_KEY="your-key"

# Vector Store (for remote PostgreSQL)
PGVECTOR_URL="postgresql://user:pass@localhost/db"

# Authentication (for remote server mode)
PENGUINCODE_JWT_SECRET="your-secret"
PENGUINCODE_API_KEY="your-key"
```

### Example: Using Environment Variables

```yaml
# In config.yaml
research:
  engines:
    google:
      api_key: "${GOOGLE_API_KEY}"
      cx_id: "${GOOGLE_CX_ID}"

auth:
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  api_keys:
    - "${PENGUINCODE_API_KEY}"
```

```bash
# In shell
export GOOGLE_API_KEY="AIza..."
export GOOGLE_CX_ID="001234..."
export PENGUINCODE_JWT_SECRET="mysecret123"
penguincode chat
```

---

## Running Modes

### Local Mode (Default)

All processing happens in-process on your machine:

```bash
penguincode chat --project .
```

**Best for**: Development, rapid iteration, full data privacy

### Standalone Mode

Run as local gRPC server (for other tools/extensions to connect):

```yaml
server:
  mode: "standalone"
  host: "localhost"
  port: 50051
  tls_enabled: false
```

### Remote Mode

Deploy on server with authentication for team access:

```yaml
server:
  mode: "remote"
  host: "0.0.0.0"
  port: 50051
  tls_enabled: true
  tls_cert_path: "/etc/ssl/certs/server.crt"
  tls_key_path: "/etc/ssl/private/server.key"

auth:
  enabled: true
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  token_expiry: 3600
```

---

## GPU Optimization

### RTX 4060 Ti Settings (8GB VRAM)

Default configuration is optimized for RTX 4060 Ti:

```yaml
regulators:
  auto_detect: true
  gpu_type: "auto"
  vram_mb: 8192                  # 8GB total
  max_concurrent_requests: 2     # Never >2 requests
  max_models_loaded: 1           # Only 1 model in VRAM
  max_concurrent_agents: 5       # Parallel agents
  min_request_interval_ms: 100
  cooldown_after_error_ms: 1000
```

### VRAM Management Tips

1. **Use lite models for simple tasks**:
   ```yaml
   execution_lite: "qwen2.5-coder:1.5b"  # Instead of 7b
   exploration_lite: "llama3.2:1b"
   ```

2. **Reduce context window**:
   ```yaml
   defaults:
     context_window: 4096  # Instead of 8192
   ```

3. **Lower max_tokens**:
   ```yaml
   defaults:
     max_tokens: 2048  # Faster responses
   ```

4. **Limit concurrent requests**:
   ```yaml
   regulators:
     max_concurrent_requests: 1
     max_models_loaded: 1
   ```

### Monitoring GPU Usage

```bash
# Watch in real-time
nvidia-smi -l 1

# Check specific process
nvidia-smi pmon -c 1

# Get temperature and memory
nvidia-smi --query-gpu=index,name,memory.total,memory.used,temperature.gpu \
  --format=csv,noheader
```

### Larger GPU Configuration (16GB+)

```yaml
regulators:
  vram_mb: 16384
  max_concurrent_requests: 4
  max_models_loaded: 2
  max_concurrent_agents: 10
```

---

## VS Code Extension

### Features

- **Inline Suggestions** - AI-powered code completions
- **Chat Panel** - Interactive coding assistant
- **Code Actions** - Explain, fix, refactor selected code
- **Research** - Web search integrated into chat

### Configuration

In VS Code settings (`settings.json`):

```json
{
  "penguincode.server.url": "http://localhost:8420",
  "penguincode.server.autoStart": true,
  "penguincode.completions.enabled": true,
  "penguincode.chat.defaultModel": "qwen2.5-coder:7b"
}
```

### Keyboard Shortcuts

- `Ctrl+Shift+P` → "Penguin Code: Start Chat"
- `Ctrl+Shift+P` → "Penguin Code: Explain Code"
- `Ctrl+Shift+P` → "Penguin Code: Fix Code"
- `Ctrl+Shift+P` → "Penguin Code: Refactor Code"

---

## Search Engines

### Engine Comparison

| Engine | Setup | Speed | Quality | API Key | Safe Search |
|--------|-------|-------|---------|---------|-------------|
| **DuckDuckGo** | None | Fast | Good | No | Yes |
| **Google** | API Key | Fast | Excellent | Yes | Yes |
| **SciraAI** | API Key | Medium | Good | Yes | Yes |
| **SearXNG** | Self-host | Medium | Good | No | Yes |
| **Fireplexity** | Self-host | Slow | Excellent | No | Yes |

### DuckDuckGo (Default)

**Best for**: General research, privacy-focused

```yaml
research:
  engine: "duckduckgo"
  use_mcp: true
  engines:
    duckduckgo:
      safesearch: "moderate"
      region: "wt-wt"
```

**No setup required** - works out of the box.

### Google Custom Search

**Best for**: High-quality results, comprehensive coverage

```yaml
research:
  engine: "google"
  use_mcp: true
  engines:
    google:
      api_key: "${GOOGLE_API_KEY}"
      cx_id: "${GOOGLE_CX_ID}"
```

**Setup**:
1. Create [Google Cloud Project](https://console.cloud.google.com)
2. Enable Custom Search API
3. Create [Custom Search Engine](https://programmablesearchengine.google.com/)
4. Get API key and CX ID

### SciraAI

**Best for**: Academic/scientific research

```yaml
research:
  engine: "sciraai"
  engines:
    sciraai:
      api_key: "${SCIRA_API_KEY}"
      endpoint: "https://api.scira.ai"
```

**Setup**: Get API key from [SciraAI](https://scira.ai)

### SearXNG

**Best for**: Privacy, metasearch, self-hosted

```yaml
research:
  engine: "searxng"
  use_mcp: true
  engines:
    searxng:
      url: "https://searx.be"  # Or self-hosted
      categories: ["general"]
```

**Public instances**: [searx.be](https://searx.be), [searx.xyz](https://searx.xyz)

**Self-host**: See [SearXNG docs](https://docs.searxng.org/)

### MCP Protocol

**MCP-enabled engines**: DuckDuckGo, Google (limited), SearXNG

**Advantages**:
- Better context preservation
- Structured tool calling
- Enhanced error handling

**Setup**:
```bash
# DuckDuckGo MCP
npx -y @nickclyde/duckduckgo-mcp-server

# SearXNG MCP
uvx mcp-searxng
```

**Disable MCP** (use direct API):
```yaml
research:
  use_mcp: false
```

---

## Memory Layer

### How It Works

mem0 stores conversation context and learnings in a vector database for semantic search and retrieval.

**What gets stored**:
- User preferences
- Project-specific context
- Previous conversations
- Code patterns and decisions

### Vector Stores

**ChromaDB (Default)**:
```yaml
memory:
  vector_store: "chroma"
  stores:
    chroma:
      path: "./.penguincode/memory"
      collection: "penguincode_memory"
```

✅ No external services needed
✅ Simple file-based storage
❌ Single-machine only

**Qdrant**:
```yaml
memory:
  vector_store: "qdrant"
  stores:
    qdrant:
      url: "http://localhost:6333"
      collection: "penguincode_memory"
```

✅ High performance
✅ Scalable
❌ Requires Qdrant server

**PGVector**:
```yaml
memory:
  vector_store: "pgvector"
  stores:
    pgvector:
      connection_string: "${PGVECTOR_URL}"
      table: "penguincode_memory"
```

✅ Uses existing PostgreSQL
✅ ACID compliance
❌ Requires PostgreSQL with pgvector extension

### Programmatic Usage

```python
from penguincode.tools.memory import create_memory_manager
from penguincode.config.settings import load_settings

settings = load_settings("config.yaml")
manager = create_memory_manager(
    config=settings.memory,
    ollama_url=settings.ollama.api_url,
    llm_model=settings.models.research
)

# Add memory
await manager.add_memory(
    content="User prefers FastAPI over Flask",
    user_id="project_123",
    metadata={"category": "preferences", "topic": "frameworks"}
)

# Search memories
results = await manager.search_memories(
    query="web framework preferences",
    user_id="project_123",
    limit=5
)

# Get all memories
all_memories = await manager.get_all_memories("project_123")

# Delete memory
await manager.delete_memory(memory_id="mem_abc123")
```

---

## Agents

PenguinCode uses a ChatAgent orchestrator that delegates to specialized agents.

### Agent Roles

| Agent | Model | Purpose | When to Use |
|-------|-------|---------|-------------|
| **ChatAgent** | llama3.2:3b | Orchestration, knowledge base | Always (main interface) |
| **Explorer** | llama3.2:3b | Search, analyze code | Understanding codebase |
| **Executor** | qwen2.5-coder:7b | Write/modify code | Implementing features |
| **Planner** | deepseek-coder:6.7b | Break down complex tasks | Multi-step implementations |

### Resource-Smart Model Selection

Agents automatically select lite or full models based on task complexity:

| Complexity | Explorer | Executor |
|------------|----------|----------|
| Simple | llama3.2:1b | qwen2.5-coder:1.5b |
| Moderate/Complex | llama3.2:3b | qwen2.5-coder:7b |

### Agent Configuration

```yaml
models:
  orchestration: "llama3.2:3b"
  exploration: "llama3.2:3b"
  exploration_lite: "llama3.2:1b"
  execution: "qwen2.5-coder:7b"
  execution_lite: "qwen2.5-coder:1.5b"
  planning: "deepseek-coder:6.7b"

agents:
  max_concurrent: 5        # Max parallel agents
  timeout_seconds: 300     # Agent timeout
  max_rounds: 10          # Max supervision rounds
```

See [AGENTS.md](AGENTS.md) for detailed architecture documentation.

---

## Examples

### Example 1: Quick Chat

```bash
penguincode chat
> Generate a Python function to sort a list of dictionaries by date
```

### Example 2: Custom Configuration

```bash
# Use different models for specific task
penguincode chat --config my-config.yaml
```

### Example 3: Debug a Session

```bash
# Enable verbose logging to troubleshoot
penguincode chat --debug
# Check logs: cat /tmp/penguincode.log
```

---

## Troubleshooting

### Ollama Not Connected

**Error**: `Connection refused to localhost:11434`

```bash
# Verify Ollama is running
curl http://localhost:11434/api/tags

# Start Ollama (macOS/Linux)
ollama serve

# Check config
penguincode config show
```

### Out of Memory (VRAM)

**Error**: `CUDA out of memory or Model timeout`

```yaml
# Option 1: Reduce concurrent requests
regulators:
  max_concurrent_requests: 1
  max_models_loaded: 1

# Option 2: Use smaller models
models:
  execution: "llama3.2:3b"  # Instead of 7b

# Option 3: Lower context window
defaults:
  context_window: 4096  # Instead of 8192
```

### Model Not Found

**Error**: `Model not found: model-name`

```bash
# List available models
ollama list

# Pull the model
ollama pull llama3.2:3b

# Or run setup
penguincode setup
```

### Config Loading Error

**Error**: `Config file not found`

```bash
# Use default config in project root
# OR specify custom path
penguincode chat --config /path/to/config.yaml
```

### Memory/ChromaDB Issues

**Error**: `ChromaDB collection not found`

```bash
# Reset memory database
rm -rf .penguincode/memory

# Restart
penguincode chat
```

### Enable Debug Logging

```bash
# Show detailed logs
penguincode chat --debug

# View logs
tail -f /tmp/penguincode.log
```

---

**For more details**: See [AGENTS.md](AGENTS.md), [DOCS_RAG.md](DOCS_RAG.md), [MEMORY.md](MEMORY.md)

**Last Updated**: 2025-12-28
