#!/bin/bash
# PenguinCode Docker Entrypoint
# Generates config.yaml from environment variables on startup

set -e

CONFIG_FILE="/app/config.yaml"

# Generate config.yaml from environment variables
generate_config() {
    cat > "$CONFIG_FILE" << EOF
# PenguinCode Configuration
# Auto-generated from Docker environment variables
# Generated at: $(date -Iseconds)

# Ollama Configuration
ollama:
  api_url: "${OLLAMA_API_URL:-${OLLAMA_HOST:+http://${OLLAMA_HOST}:11434}}"
  timeout: ${OLLAMA_TIMEOUT:-120}

# Model Configuration
models:
  planning: "${PENGUINCODE_MODEL_PLANNING:-deepseek-coder:6.7b}"
  orchestration: "${PENGUINCODE_MODEL_ORCHESTRATION:-llama3.2:3b}"
  research: "${PENGUINCODE_MODEL_RESEARCH:-llama3.2:3b}"
  execution: "${PENGUINCODE_MODEL_EXECUTION:-qwen2.5-coder:7b}"
  execution_lite: "${PENGUINCODE_MODEL_EXECUTION_LITE:-qwen2.5-coder:7b}"
  exploration: "${PENGUINCODE_MODEL_EXPLORATION:-llama3.2:3b}"
  exploration_lite: "${PENGUINCODE_MODEL_EXPLORATION_LITE:-llama3.2:3b}"

# Per-Agent Model Overrides
agents:
  executor:
    model: "${PENGUINCODE_AGENT_EXECUTOR:-qwen2.5-coder:7b}"
    description: "Code mutations, file writes, bash execution"
  explorer:
    model: "${PENGUINCODE_AGENT_EXPLORER:-llama3.2:3b}"
    description: "Codebase navigation, file reading, search"
  reviewer:
    model: "${PENGUINCODE_AGENT_REVIEWER:-codellama:7b}"
    description: "Code review, quality analysis"
  planner:
    model: "${PENGUINCODE_AGENT_PLANNER:-deepseek-coder:6.7b}"
    description: "Implementation planning, task decomposition"
  tester:
    model: "${PENGUINCODE_AGENT_TESTER:-qwen2.5-coder:7b}"
    description: "Test generation and execution"
  refactor:
    model: "${PENGUINCODE_AGENT_REFACTOR:-codellama:7b}"
    description: "Refactoring suggestions and improvements"
  debugger:
    model: "${PENGUINCODE_AGENT_DEBUGGER:-deepseek-coder:6.7b}"
    description: "Error analysis, debugging, fix suggestions"
  docs:
    model: "${PENGUINCODE_AGENT_DOCS:-mistral:7b}"
    description: "Documentation generation"
  researcher:
    model: "${PENGUINCODE_AGENT_RESEARCHER:-llama3.2:3b}"
    description: "Web research, summarization"

# Default Parameters
defaults:
  temperature: ${PENGUINCODE_TEMPERATURE:-0.7}
  max_tokens: ${PENGUINCODE_MAX_TOKENS:-4096}
  context_window: ${PENGUINCODE_CONTEXT_WINDOW:-8192}

# Security Configuration
security:
  level: ${PENGUINCODE_SECURITY_LEVEL:-2}

# History Configuration
history:
  enabled: ${PENGUINCODE_HISTORY_ENABLED:-true}
  location: "${PENGUINCODE_HISTORY_LOCATION:-per-project}"
  max_sessions: ${PENGUINCODE_HISTORY_MAX_SESSIONS:-50}

# Research Configuration
research:
  engine: "${PENGUINCODE_RESEARCH_ENGINE:-duckduckgo}"
  use_mcp: ${PENGUINCODE_RESEARCH_USE_MCP:-true}
  max_results: ${PENGUINCODE_RESEARCH_MAX_RESULTS:-5}

  engines:
    duckduckgo:
      safesearch: "${PENGUINCODE_DDG_SAFESEARCH:-moderate}"
      region: "${PENGUINCODE_DDG_REGION:-wt-wt}"

    fireplexity:
      firecrawl_api_key: "${FIRECRAWL_API_KEY:-}"

    sciraai:
      api_key: "${SCIRA_API_KEY:-}"
      endpoint: "${SCIRA_ENDPOINT:-https://api.scira.ai}"

    searxng:
      url: "${SEARXNG_URL:-https://searx.be}"
      categories: ["general"]

    google:
      api_key: "${GOOGLE_API_KEY:-}"
      cx_id: "${GOOGLE_CX_ID:-}"

# Memory Configuration (mem0)
memory:
  enabled: ${PENGUINCODE_MEMORY_ENABLED:-true}
  vector_store: "${PENGUINCODE_MEMORY_STORE:-chroma}"
  embedding_model: "${PENGUINCODE_EMBEDDING_MODEL:-nomic-embed-text}"

  stores:
    chroma:
      path: "${PENGUINCODE_CHROMA_PATH:-./.penguincode/memory}"
      collection: "${PENGUINCODE_CHROMA_COLLECTION:-penguincode_memory}"

    qdrant:
      url: "${QDRANT_URL:-http://localhost:6333}"
      collection: "${QDRANT_COLLECTION:-penguincode_memory}"

    pgvector:
      connection_string: "${PGVECTOR_URL:-}"
      table: "${PGVECTOR_TABLE:-penguincode_memory}"

# GPU Regulators
regulators:
  auto_detect: ${PENGUINCODE_GPU_AUTO_DETECT:-true}
  gpu_type: "${PENGUINCODE_GPU_TYPE:-auto}"
  gpu_model: "${PENGUINCODE_GPU_MODEL:-}"
  vram_mb: ${PENGUINCODE_VRAM_MB:-8192}
  max_concurrent_requests: ${PENGUINCODE_MAX_CONCURRENT:-2}
  max_models_loaded: ${PENGUINCODE_MAX_MODELS:-1}
  request_queue_size: ${PENGUINCODE_QUEUE_SIZE:-10}
  min_request_interval_ms: ${PENGUINCODE_MIN_INTERVAL_MS:-100}
  cooldown_after_error_ms: ${PENGUINCODE_COOLDOWN_MS:-1000}

# Documentation RAG
docs_rag:
  enabled: ${PENGUINCODE_DOCS_RAG_ENABLED:-true}
  auto_detect_on_start: ${PENGUINCODE_DOCS_AUTO_DETECT_START:-true}
  auto_detect_on_request: ${PENGUINCODE_DOCS_AUTO_DETECT_REQUEST:-true}
  auto_index_on_detect: ${PENGUINCODE_DOCS_AUTO_INDEX_DETECT:-true}
  auto_index_on_request: ${PENGUINCODE_DOCS_AUTO_INDEX_REQUEST:-true}
  cache_dir: "${PENGUINCODE_DOCS_CACHE_DIR:-./.penguincode/docs}"
  cache_max_age_days: ${PENGUINCODE_DOCS_CACHE_MAX_AGE:-7}
  max_pages_per_library: ${PENGUINCODE_DOCS_MAX_PAGES:-50}
  max_libraries_to_index: ${PENGUINCODE_DOCS_MAX_LIBRARIES:-20}

# Usage API (Optional)
usage_api:
  enabled: ${PENGUINCODE_USAGE_API_ENABLED:-false}
  endpoint: "${PENGUINCODE_USAGE_API_ENDPOINT:-}"
  jwt_token: "${OLLAMA_USAGE_JWT:-}"
  refresh_interval: ${PENGUINCODE_USAGE_REFRESH_INTERVAL:-300}
  show_warnings_at: ${PENGUINCODE_USAGE_WARN_AT:-80}

# MCP Configuration
mcp:
  enabled: ${PENGUINCODE_MCP_ENABLED:-true}
  servers: []

# Server Configuration
server:
  mode: "${PENGUINCODE_SERVER_MODE:-standalone}"
  host: "${PENGUINCODE_SERVER_HOST:-0.0.0.0}"
  port: ${PENGUINCODE_SERVER_PORT:-50051}
  tls_enabled: ${PENGUINCODE_TLS_ENABLED:-false}
  tls_cert_path: "${PENGUINCODE_TLS_CERT_PATH:-}"
  tls_key_path: "${PENGUINCODE_TLS_KEY_PATH:-}"

# Authentication Configuration
auth:
  enabled: ${PENGUINCODE_AUTH_ENABLED:-false}
  jwt_secret: "${PENGUINCODE_JWT_SECRET:-${JWT_SECRET:-}}"
  token_expiry: ${PENGUINCODE_TOKEN_EXPIRY:-3600}
  refresh_expiry: ${PENGUINCODE_REFRESH_EXPIRY:-86400}
  api_keys:
    - "${PENGUINCODE_API_KEY:-}"

# Client Configuration
client:
  server_url: "${PENGUINCODE_CLIENT_SERVER_URL:-}"
  token_path: "${PENGUINCODE_CLIENT_TOKEN_PATH:-~/.penguincode/token}"
  local_tools:
    - read
    - write
    - edit
    - bash
    - grep
    - glob
EOF

    echo "[entrypoint] Generated config.yaml from environment variables"
}

# Check if user mounted their own config.yaml
if [ -f "/app/config.yaml.mounted" ]; then
    echo "[entrypoint] Using mounted config.yaml"
    cp /app/config.yaml.mounted /app/config.yaml
elif [ "${PENGUINCODE_USE_ENV_CONFIG:-true}" = "true" ]; then
    generate_config
else
    echo "[entrypoint] No config.yaml found and PENGUINCODE_USE_ENV_CONFIG=false"
    exit 1
fi

# Set default Ollama URL if not set and OLLAMA_HOST is provided
if [ -z "$OLLAMA_API_URL" ] && [ -n "$OLLAMA_HOST" ]; then
    export OLLAMA_API_URL="http://${OLLAMA_HOST}:11434"
fi

# Default to host.docker.internal if no Ollama URL configured
if [ -z "$OLLAMA_API_URL" ]; then
    export OLLAMA_API_URL="http://host.docker.internal:11434"
fi

echo "[entrypoint] Ollama API URL: $OLLAMA_API_URL"
echo "[entrypoint] Server mode: ${PENGUINCODE_SERVER_MODE:-standalone}"
echo "[entrypoint] Server port: ${PENGUINCODE_SERVER_PORT:-50051}"

# Execute the main command
exec "$@"
