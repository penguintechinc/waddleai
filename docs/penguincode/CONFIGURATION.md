# PenguinCode Configuration Reference

Complete reference for `config.yaml` configuration options.

## Configuration File Location

PenguinCode looks for configuration in this order:
1. `./config.yaml` (current directory)
2. `~/.penguincode/config.yaml` (user home)
3. `/etc/penguincode/config.yaml` (system-wide)

## Environment Variable Substitution

Configuration values support environment variable substitution:
```yaml
api_key: "${MY_API_KEY}"           # Required - fails if not set
api_url: "${API_URL:-http://localhost}"  # Optional - uses default if not set
```

---

## Ollama Configuration

```yaml
ollama:
  api_url: "${OLLAMA_API_URL:-http://localhost:11434}"
  timeout: 120
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `api_url` | string | `http://localhost:11434` | Ollama API endpoint. Supports local or remote instances. |
| `timeout` | integer | `120` | Request timeout in seconds for Ollama API calls. |

**Remote Ollama Examples:**
```bash
# LAN server
export OLLAMA_API_URL="http://192.168.1.100:11434"

# Cloud GPU instance
export OLLAMA_API_URL="http://gpu-server.example.com:11434"

# Docker network
export OLLAMA_API_URL="http://ollama:11434"
```

---

## Model Configuration

### Global Model Roles

```yaml
models:
  planning: "deepseek-coder:6.7b"
  orchestration: "llama3.2:3b"
  research: "llama3.2:3b"
  execution: "qwen2.5-coder:7b"
  execution_lite: "qwen2.5-coder:7b"
  exploration: "llama3.2:3b"
  exploration_lite: "llama3.2:3b"
```

| Key | Default | Description |
|-----|---------|-------------|
| `planning` | `deepseek-coder:6.7b` | Implementation planning, task decomposition. |
| `orchestration` | `llama3.2:3b` | ChatAgent orchestration, routing decisions. |
| `research` | `llama3.2:3b` | Web research, summarization tasks. |
| `execution` | `qwen2.5-coder:7b` | Complex code generation (refactoring, multi-file, features). |
| `execution_lite` | `qwen2.5-coder:7b` | Simple code edits (single file). Use `qwen2.5-coder:1.5b` for lower VRAM. |
| `exploration` | `llama3.2:3b` | Standard codebase exploration, file reading. |
| `exploration_lite` | `llama3.2:3b` | Quick file reads. Use `llama3.2:1b` for lower VRAM. |

### Per-Agent Model Overrides

```yaml
agents:
  executor:
    model: "qwen2.5-coder:7b"
    description: "Code mutations, file writes, bash execution"
  explorer:
    model: "llama3.2:3b"
    description: "Codebase navigation, file reading, search"
  reviewer:
    model: "codellama:7b"
    description: "Code review, quality analysis"
  planner:
    model: "deepseek-coder:6.7b"
    description: "Implementation planning, task decomposition"
  tester:
    model: "qwen2.5-coder:7b"
    description: "Test generation and execution"
  refactor:
    model: "codellama:7b"
    description: "Refactoring suggestions and improvements"
  debugger:
    model: "deepseek-coder:6.7b"
    description: "Error analysis, debugging, fix suggestions"
  docs:
    model: "mistral:7b"
    description: "Documentation generation"
  researcher:
    model: "llama3.2:3b"
    description: "Web research, summarization"
```

| Agent | Purpose | Recommended Models |
|-------|---------|-------------------|
| `executor` | Code mutations, file writes | `qwen2.5-coder:7b`, `codellama:7b` |
| `explorer` | Codebase navigation | `llama3.2:3b`, `llama3.2:1b` |
| `reviewer` | Code review | `codellama:7b`, `deepseek-coder:6.7b` |
| `planner` | Task decomposition | `deepseek-coder:6.7b` |
| `tester` | Test generation | `qwen2.5-coder:7b` |
| `refactor` | Refactoring | `codellama:7b` |
| `debugger` | Error analysis | `deepseek-coder:6.7b` |
| `docs` | Documentation | `mistral:7b` |
| `researcher` | Web research | `llama3.2:3b` |

---

## Default Parameters

```yaml
defaults:
  temperature: 0.7
  max_tokens: 4096
  context_window: 8192
```

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `temperature` | float | `0.7` | 0.0-2.0 | Controls randomness. Lower = more deterministic. |
| `max_tokens` | integer | `4096` | 1-32768 | Maximum tokens in model response. |
| `context_window` | integer | `8192` | 2048-131072 | Context window size (model dependent). |

---

## Security Configuration

```yaml
security:
  level: 2
```

| Level | Name | Behavior |
|-------|------|----------|
| `1` | Strict | Prompt for ALL operations (read, write, bash, etc.) |
| `2` | Moderate | Prompt only for destructive operations (write, delete, bash) |
| `3` | Permissive | No prompts (use with caution in trusted environments) |

---

## History Configuration

```yaml
history:
  enabled: true
  location: "per-project"
  max_sessions: 50
```

| Key | Type | Default | Options | Description |
|-----|------|---------|---------|-------------|
| `enabled` | boolean | `true` | `true`, `false` | Enable/disable session history. |
| `location` | string | `per-project` | `per-project`, `global` | Where to store history files. |
| `max_sessions` | integer | `50` | 1-1000 | Maximum sessions to retain per location. |

**Storage Locations:**
- `per-project`: `./.penguincode/history/`
- `global`: `~/.penguincode/history/`

---

## Research Configuration

```yaml
research:
  engine: "duckduckgo"
  use_mcp: true
  max_results: 5

  engines:
    duckduckgo:
      safesearch: "moderate"
      region: "wt-wt"

    fireplexity:
      firecrawl_api_key: "${FIRECRAWL_API_KEY}"

    sciraai:
      api_key: "${SCIRA_API_KEY}"
      endpoint: "https://api.scira.ai"

    searxng:
      url: "https://searx.be"
      categories: ["general"]

    google:
      api_key: "${GOOGLE_API_KEY}"
      cx_id: "${GOOGLE_CX_ID}"
```

### Main Research Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `engine` | string | `duckduckgo` | Active search engine. |
| `use_mcp` | boolean | `true` | Use MCP server when available. |
| `max_results` | integer | `5` | Maximum search results to return. |

### Available Engines

| Engine | API Key Required | Description |
|--------|------------------|-------------|
| `duckduckgo` | No | Privacy-focused search (default). |
| `fireplexity` | Yes (`FIRECRAWL_API_KEY`) | Firecrawl-powered search with AI summarization. |
| `sciraai` | Yes (`SCIRA_API_KEY`) | Scira AI scientific search. |
| `searxng` | No | Self-hosted or public SearXNG instance. |
| `google` | Yes (`GOOGLE_API_KEY`, `GOOGLE_CX_ID`) | Google Custom Search API. |

### DuckDuckGo Options

| Key | Options | Description |
|-----|---------|-------------|
| `safesearch` | `off`, `moderate`, `strict` | SafeSearch filtering level. |
| `region` | `wt-wt`, `us-en`, `uk-en`, etc. | Region for search results. |

### SearXNG Options

| Key | Type | Description |
|-----|------|-------------|
| `url` | string | SearXNG instance URL. |
| `categories` | list | Search categories: `general`, `images`, `news`, `science`, etc. |

---

## Memory Configuration (mem0)

```yaml
memory:
  enabled: true
  vector_store: "chroma"
  embedding_model: "nomic-embed-text"

  stores:
    chroma:
      path: "./.penguincode/memory"
      collection: "penguincode_memory"

    qdrant:
      url: "http://localhost:6333"
      collection: "penguincode_memory"

    pgvector:
      connection_string: "${PGVECTOR_URL}"
      table: "penguincode_memory"
```

### Main Memory Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Enable/disable persistent memory. |
| `vector_store` | string | `chroma` | Vector database backend. |
| `embedding_model` | string | `nomic-embed-text` | Ollama model for embeddings. |

### Vector Store Options

| Store | Requirements | Best For |
|-------|--------------|----------|
| `chroma` | None (local) | Development, single-user deployments. |
| `qdrant` | Qdrant server | Production, distributed deployments. |
| `pgvector` | PostgreSQL + pgvector | Existing PostgreSQL infrastructure. |

### ChromaDB Options

| Key | Type | Description |
|-----|------|-------------|
| `path` | string | Local storage directory. |
| `collection` | string | Collection name for memories. |

### Qdrant Options

| Key | Type | Description |
|-----|------|-------------|
| `url` | string | Qdrant server URL. |
| `collection` | string | Collection name for memories. |

### PGVector Options

| Key | Type | Description |
|-----|------|-------------|
| `connection_string` | string | PostgreSQL connection URL with pgvector extension. |
| `table` | string | Table name for storing vectors. |

---

## GPU Regulators

```yaml
regulators:
  auto_detect: true
  gpu_type: "auto"
  gpu_model: ""
  vram_mb: 8192
  max_concurrent_requests: 2
  max_models_loaded: 1
  request_queue_size: 10
  min_request_interval_ms: 100
  cooldown_after_error_ms: 1000
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auto_detect` | boolean | `true` | Auto-detect GPU capabilities. |
| `gpu_type` | string | `auto` | GPU type: `auto`, `nvidia`, `amd`, `apple`, `cpu`. |
| `gpu_model` | string | `""` | Specific GPU model (for manual config). |
| `vram_mb` | integer | `8192` | Available VRAM in MB. |
| `max_concurrent_requests` | integer | `2` | Max parallel Ollama requests. |
| `max_models_loaded` | integer | `1` | Max models in VRAM simultaneously. |
| `request_queue_size` | integer | `10` | Request queue buffer size. |
| `min_request_interval_ms` | integer | `100` | Minimum ms between requests. |
| `cooldown_after_error_ms` | integer | `1000` | Cooldown after GPU error. |

**Recommended Settings by GPU:**

| GPU | VRAM | `max_concurrent_requests` | `max_models_loaded` |
|-----|------|---------------------------|---------------------|
| RTX 4060 Ti (8GB) | 8192 | 2 | 1 |
| RTX 4070 (12GB) | 12288 | 3 | 2 |
| RTX 4080 (16GB) | 16384 | 4 | 2 |
| RTX 4090 (24GB) | 24576 | 6 | 3 |

---

## Documentation RAG

```yaml
docs_rag:
  enabled: true
  auto_detect_on_start: true
  auto_detect_on_request: true
  auto_index_on_detect: true
  auto_index_on_request: true

  languages_manual:
    python: false
    javascript: false
    typescript: false
    go: false
    rust: false
    hcl: false
    ansible: false

  libraries_manual: []

  cache_dir: "./.penguincode/docs"
  cache_max_age_days: 7
  max_pages_per_library: 50
  max_libraries_to_index: 20
```

### Main RAG Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Enable documentation RAG. |
| `auto_detect_on_start` | boolean | `true` | Detect languages from project files at startup. |
| `auto_detect_on_request` | boolean | `true` | Detect languages from request content. |
| `auto_index_on_detect` | boolean | `true` | Index docs when languages detected. |
| `auto_index_on_request` | boolean | `true` | Index docs on-demand when needed. |

### Manual Language Configuration

Set to `true` to always index docs for a language even if not auto-detected:

| Language | Detection Files |
|----------|-----------------|
| `python` | `pyproject.toml`, `requirements.txt`, `*.py` |
| `javascript` | `package.json`, `*.js` |
| `typescript` | `tsconfig.json`, `*.ts` |
| `go` | `go.mod`, `*.go` |
| `rust` | `Cargo.toml`, `*.rs` |
| `hcl` | `*.tf`, `*.tofu`, `.terraform.lock.hcl` |
| `ansible` | `ansible.cfg`, `playbook.yml`, `requirements.yml` |

### Cache Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cache_dir` | string | `./.penguincode/docs` | Documentation cache directory. |
| `cache_max_age_days` | integer | `7` | Days before re-fetching docs. |
| `max_pages_per_library` | integer | `50` | Max pages to index per library. |
| `max_libraries_to_index` | integer | `20` | Max libraries to auto-index. |

---

## Usage API (Optional)

```yaml
usage_api:
  enabled: false
  endpoint: "https://ollama.example.com/api/usage"
  jwt_token: "${OLLAMA_USAGE_JWT}"
  refresh_interval: 300
  show_warnings_at: 80
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Enable usage tracking API. |
| `endpoint` | string | - | Usage API endpoint URL. |
| `jwt_token` | string | - | JWT token for authentication. |
| `refresh_interval` | integer | `300` | Seconds between quota refreshes. |
| `show_warnings_at` | integer | `80` | Quota percentage to show warnings. |

---

## MCP Server Configuration

```yaml
mcp:
  enabled: true
  servers: []
```

### MCP Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `true` | Enable MCP protocol support. |
| `servers` | list | `[]` | List of MCP server configurations. |

### Server Configuration

**Stdio Transport (subprocess):**
```yaml
- name: "duckduckgo"
  transport: "stdio"
  command: "npx"
  args: ["-y", "@nickclyde/duckduckgo-mcp-server"]
  timeout: 30
```

**HTTP Transport (remote):**
```yaml
- name: "custom-server"
  transport: "http"
  url: "http://localhost:8080"
  headers:
    Authorization: "Bearer ${MCP_API_TOKEN}"
  timeout: 30
```

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `name` | string | Yes | Unique server identifier. |
| `transport` | string | Yes | `stdio` or `http`. |
| `command` | string | stdio only | Command to execute. |
| `args` | list | stdio only | Command arguments. |
| `env` | object | No | Environment variables for subprocess. |
| `url` | string | http only | HTTP endpoint URL. |
| `headers` | object | No | HTTP headers (for auth, etc.). |
| `timeout` | integer | No | Request timeout in seconds (default: 30). |

---

## Server Configuration (Client-Server Mode)

```yaml
server:
  mode: "local"
  host: "localhost"
  port: 50051
  tls_enabled: false
  tls_cert_path: ""
  tls_key_path: ""
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mode` | string | `local` | Server mode: `local`, `standalone`, `remote`. |
| `host` | string | `localhost` | Server bind address. |
| `port` | integer | `50051` | gRPC server port. |
| `tls_enabled` | boolean | `false` | Enable TLS encryption. |
| `tls_cert_path` | string | `""` | Path to TLS certificate file. |
| `tls_key_path` | string | `""` | Path to TLS private key file. |

### Server Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `local` | Everything runs in-process | Solo developer, local Ollama |
| `standalone` | gRPC server on localhost | Shared local server, testing |
| `remote` | gRPC server with TLS + JWT auth | Team deployment, remote GPU |

---

## Authentication Configuration

```yaml
auth:
  enabled: false
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  token_expiry: 3600
  refresh_expiry: 86400
  api_keys:
    - "${PENGUINCODE_API_KEY}"
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | boolean | `false` | Enable JWT authentication. |
| `jwt_secret` | string | - | Secret key for JWT signing (min 32 chars). |
| `token_expiry` | integer | `3600` | Access token expiry in seconds (1 hour). |
| `refresh_expiry` | integer | `86400` | Refresh token expiry in seconds (24 hours). |
| `api_keys` | list | `[]` | Valid API keys for authentication. |

**Security Notes:**
- Always set `jwt_secret` via environment variable, never in config file
- Use strong, random secrets (minimum 32 characters)
- Rotate API keys periodically

---

## Client Configuration

```yaml
client:
  server_url: ""
  token_path: "~/.penguincode/token"
  local_tools:
    - read
    - write
    - edit
    - bash
    - grep
    - glob
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `server_url` | string | `""` | Remote server URL (e.g., `grpc://server:50051`). |
| `token_path` | string | `~/.penguincode/token` | Path to store JWT token. |
| `local_tools` | list | see below | Tools that execute locally on client. |

### Local Tools

Tools in `local_tools` execute on the client machine for security:

| Tool | Description |
|------|-------------|
| `read` | Read files from local filesystem |
| `write` | Write files to local filesystem |
| `edit` | Edit existing files |
| `bash` | Execute shell commands |
| `grep` | Search file contents |
| `glob` | Find files by pattern |

---

## Docker Configuration

PenguinCode provides Docker support for containerized deployments. **The Docker server is fully configured via environment variables** - no config.yaml file is required.

On startup, the entrypoint script automatically generates `/app/config.yaml` from environment variables, making deployment simple and 12-factor compliant.

### Quick Start

```bash
# Minimal - uses host Ollama
docker compose up -d

# With remote Ollama
OLLAMA_HOST=192.168.1.100 docker compose up -d

# With authentication
JWT_SECRET=your-32-char-secret PENGUINCODE_API_KEY=your-key docker compose up -d

# Custom models
PENGUINCODE_MODEL_EXECUTION=codellama:13b docker compose up -d
```

### Docker Compose

```yaml
# docker-compose.yml
services:
  penguincode-server:
    build:
      context: .
      dockerfile: Dockerfile.server
    ports:
      - "${PENGUINCODE_SERVER_PORT:-50051}:50051"
    environment:
      # All config via ENV - no config.yaml needed!
      - OLLAMA_HOST=${OLLAMA_HOST:-host.docker.internal}
      - PENGUINCODE_MODEL_EXECUTION=${PENGUINCODE_MODEL_EXECUTION:-qwen2.5-coder:7b}
      - PENGUINCODE_MEMORY_ENABLED=${PENGUINCODE_MEMORY_ENABLED:-true}
      - PENGUINCODE_USE_ENV_CONFIG=true
    volumes:
      - penguincode-data:/app/.penguincode
```

### Environment Variables

**Docker environment variables take precedence over config.yaml values.** This allows the same image to be deployed with different configurations without modifying the config file.

#### Ollama Connection

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `OLLAMA_HOST` | `host.docker.internal` | `ollama.api_url` | Ollama server hostname. |
| `OLLAMA_API_URL` | `http://localhost:11434` | `ollama.api_url` | Full Ollama API URL (overrides `OLLAMA_HOST`). |
| `OLLAMA_TIMEOUT` | `120` | `ollama.timeout` | Request timeout in seconds. |

#### Server Configuration

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_SERVER_HOST` | `0.0.0.0` | `server.host` | gRPC server bind address. |
| `PENGUINCODE_SERVER_PORT` | `50051` | `server.port` | gRPC server port. |
| `PENGUINCODE_SERVER_MODE` | `local` | `server.mode` | Server mode: `local`, `standalone`, `remote`. |
| `PENGUINCODE_TLS_ENABLED` | `false` | `server.tls_enabled` | Enable TLS encryption. |
| `PENGUINCODE_TLS_CERT_PATH` | - | `server.tls_cert_path` | Path to TLS certificate. |
| `PENGUINCODE_TLS_KEY_PATH` | - | `server.tls_key_path` | Path to TLS private key. |

#### Authentication

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_AUTH_ENABLED` | `false` | `auth.enabled` | Enable JWT authentication. |
| `PENGUINCODE_JWT_SECRET` | - | `auth.jwt_secret` | JWT signing secret (min 32 chars). |
| `JWT_SECRET` | - | `auth.jwt_secret` | Alias for docker-compose compatibility. |
| `PENGUINCODE_API_KEY` | - | `auth.api_keys[0]` | Primary API key for authentication. |
| `PENGUINCODE_TOKEN_EXPIRY` | `3600` | `auth.token_expiry` | Access token expiry in seconds. |
| `PENGUINCODE_REFRESH_EXPIRY` | `86400` | `auth.refresh_expiry` | Refresh token expiry in seconds. |

#### Model Configuration

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_MODEL_PLANNING` | `deepseek-coder:6.7b` | `models.planning` | Planning model. |
| `PENGUINCODE_MODEL_ORCHESTRATION` | `llama3.2:3b` | `models.orchestration` | Orchestration model. |
| `PENGUINCODE_MODEL_EXECUTION` | `qwen2.5-coder:7b` | `models.execution` | Execution model. |
| `PENGUINCODE_MODEL_EXECUTION_LITE` | `qwen2.5-coder:7b` | `models.execution_lite` | Lite execution model. |
| `PENGUINCODE_MODEL_EXPLORATION` | `llama3.2:3b` | `models.exploration` | Exploration model. |
| `PENGUINCODE_MODEL_EXPLORATION_LITE` | `llama3.2:3b` | `models.exploration_lite` | Lite exploration model. |
| `PENGUINCODE_MODEL_RESEARCH` | `llama3.2:3b` | `models.research` | Research model. |

#### Per-Agent Model Overrides

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_AGENT_EXECUTOR` | `qwen2.5-coder:7b` | `agents.executor.model` | Executor agent model. |
| `PENGUINCODE_AGENT_EXPLORER` | `llama3.2:3b` | `agents.explorer.model` | Explorer agent model. |
| `PENGUINCODE_AGENT_REVIEWER` | `codellama:7b` | `agents.reviewer.model` | Reviewer agent model. |
| `PENGUINCODE_AGENT_PLANNER` | `deepseek-coder:6.7b` | `agents.planner.model` | Planner agent model. |
| `PENGUINCODE_AGENT_TESTER` | `qwen2.5-coder:7b` | `agents.tester.model` | Tester agent model. |
| `PENGUINCODE_AGENT_REFACTOR` | `codellama:7b` | `agents.refactor.model` | Refactor agent model. |
| `PENGUINCODE_AGENT_DEBUGGER` | `deepseek-coder:6.7b` | `agents.debugger.model` | Debugger agent model. |
| `PENGUINCODE_AGENT_DOCS` | `mistral:7b` | `agents.docs.model` | Docs agent model. |
| `PENGUINCODE_AGENT_RESEARCHER` | `llama3.2:3b` | `agents.researcher.model` | Researcher agent model. |

#### Memory Configuration

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_MEMORY_ENABLED` | `true` | `memory.enabled` | Enable persistent memory. |
| `PENGUINCODE_MEMORY_STORE` | `chroma` | `memory.vector_store` | Vector store: `chroma`, `qdrant`, `pgvector`. |
| `PENGUINCODE_EMBEDDING_MODEL` | `nomic-embed-text` | `memory.embedding_model` | Embedding model name. |
| `QDRANT_URL` | `http://localhost:6333` | `memory.stores.qdrant.url` | Qdrant server URL. |
| `PGVECTOR_URL` | - | `memory.stores.pgvector.connection_string` | PostgreSQL connection string. |

#### Security & Defaults

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_SECURITY_LEVEL` | `2` | `security.level` | Security level (1-3). |
| `PENGUINCODE_TEMPERATURE` | `0.7` | `defaults.temperature` | Default temperature. |
| `PENGUINCODE_MAX_TOKENS` | `4096` | `defaults.max_tokens` | Default max tokens. |

#### Research Configuration

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_RESEARCH_ENGINE` | `duckduckgo` | `research.engine` | Search engine. |
| `FIRECRAWL_API_KEY` | - | `research.engines.fireplexity.firecrawl_api_key` | Firecrawl API key. |
| `SCIRA_API_KEY` | - | `research.engines.sciraai.api_key` | Scira AI API key. |
| `GOOGLE_API_KEY` | - | `research.engines.google.api_key` | Google API key. |
| `GOOGLE_CX_ID` | - | `research.engines.google.cx_id` | Google Custom Search ID. |

#### GPU Regulators

| Variable | Default | Config Equivalent | Description |
|----------|---------|-------------------|-------------|
| `PENGUINCODE_VRAM_MB` | `8192` | `regulators.vram_mb` | Available VRAM in MB. |
| `PENGUINCODE_MAX_CONCURRENT` | `2` | `regulators.max_concurrent_requests` | Max parallel requests. |
| `PENGUINCODE_MAX_MODELS` | `1` | `regulators.max_models_loaded` | Max models loaded. |

### Python Environment Variables (Dockerfile)

| Variable | Value | Description |
|----------|-------|-------------|
| `PYTHONDONTWRITEBYTECODE` | `1` | Prevent Python from writing `.pyc` files. |
| `PYTHONUNBUFFERED` | `1` | Force stdout/stderr to be unbuffered for real-time logs. |
| `PIP_NO_CACHE_DIR` | `1` | Disable pip cache to reduce image size. |
| `PIP_DISABLE_PIP_VERSION_CHECK` | `1` | Skip pip version check during install. |

### Running with Docker Compose

**Server only (uses host Ollama):**
```bash
docker compose up -d
```

**Server with bundled Ollama:**
```bash
docker compose --profile with-ollama up -d
```

**With authentication enabled:**
```bash
export JWT_SECRET="your-secret-key-min-32-characters-long"
export PENGUINCODE_API_KEY="your-api-key"
docker compose up -d
```

**Connect to remote Ollama:**
```bash
export OLLAMA_HOST="192.168.1.100"
# or
export OLLAMA_API_URL="http://gpu-server.local:11434"
docker compose up -d
```

### Docker Volumes

| Volume | Container Path | Description |
|--------|----------------|-------------|
| `penguincode-data` | `/app/.penguincode` | Persistent storage for memory, cache, and history. |
| `ollama-models` | `/root/.ollama` | Ollama model storage (with-ollama profile). |
| `./config.yaml` | `/app/config.yaml:ro` | Configuration file (read-only mount). |

### Health Check

The container includes a built-in health check:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import grpc; ch = grpc.insecure_channel('localhost:50051'); grpc.channel_ready_future(ch).result(timeout=5)"
```

| Parameter | Value | Description |
|-----------|-------|-------------|
| `interval` | 30s | Time between health checks. |
| `timeout` | 10s | Maximum time for health check to complete. |
| `start_period` | 5s | Grace period before first check. |
| `retries` | 3 | Failures before marking unhealthy. |

### GPU Support (Ollama Container)

When using the `with-ollama` profile, GPU access is configured:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

**Requirements:**
- NVIDIA Container Toolkit installed
- Docker configured for GPU access
- Compatible NVIDIA GPU

### Network Configuration

**Accessing host services from container:**
```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

This allows the container to reach services on the host machine (like a local Ollama instance) using `host.docker.internal`.

### Production Deployment

**With TLS and authentication:**
```bash
# Generate certificates
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes

# Set environment
export JWT_SECRET="$(openssl rand -base64 32)"
export PENGUINCODE_API_KEY="$(openssl rand -base64 24)"

# Update config.yaml for TLS
# server:
#   mode: "remote"
#   tls_enabled: true
#   tls_cert_path: "/app/certs/cert.pem"
#   tls_key_path: "/app/certs/key.pem"

# Mount certificates
docker compose -f docker-compose.prod.yml up -d
```

### Exposed Ports

| Port | Protocol | Description |
|------|----------|-------------|
| `50051` | gRPC | PenguinCode server (configurable via `PENGUINCODE_SERVER_PORT`). |
| `11434` | HTTP | Ollama API (with-ollama profile only). |

### Building the Image

```bash
# Build server image
docker build -f Dockerfile.server -t penguincode-server .

# Build with specific tag
docker build -f Dockerfile.server -t penguincode-server:v0.1.0 .

# Multi-arch build
docker buildx build --platform linux/amd64,linux/arm64 \
  -f Dockerfile.server -t penguincode-server:latest --push .
```

---

## Complete Example Configuration

```yaml
# Minimal development configuration
ollama:
  api_url: "http://localhost:11434"
  timeout: 120

models:
  planning: "deepseek-coder:6.7b"
  orchestration: "llama3.2:3b"
  execution: "qwen2.5-coder:7b"

defaults:
  temperature: 0.7
  max_tokens: 4096

security:
  level: 2

memory:
  enabled: true
  vector_store: "chroma"

server:
  mode: "local"
```

```yaml
# Production remote server configuration
ollama:
  api_url: "${OLLAMA_API_URL}"
  timeout: 300

models:
  planning: "deepseek-coder:6.7b"
  orchestration: "llama3.2:3b"
  execution: "qwen2.5-coder:7b"

security:
  level: 1

memory:
  enabled: true
  vector_store: "qdrant"
  stores:
    qdrant:
      url: "${QDRANT_URL}"
      collection: "penguincode_prod"

server:
  mode: "remote"
  host: "0.0.0.0"
  port: 50051
  tls_enabled: true
  tls_cert_path: "/etc/penguincode/cert.pem"
  tls_key_path: "/etc/penguincode/key.pem"

auth:
  enabled: true
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  api_keys:
    - "${PENGUINCODE_API_KEY}"
```

---

## Related Documentation

- [Usage Guide](USAGE.md) - Installation and setup
- [Architecture](ARCHITECTURE.md) - Client-server architecture
- [Security](SECURITY.md) - Security best practices
- [Memory](MEMORY.md) - Memory integration details
- [MCP Integration](MCP.md) - MCP server setup
