# PenguinCode Architecture

This document describes the client-server architecture introduced to support remote Ollama deployments and team collaboration.

## Overview

PenguinCode supports three operational modes:

| Mode | Description | Use Case |
|------|-------------|----------|
| **local** | Everything runs in-process | Solo developer, local Ollama |
| **standalone** | gRPC server on localhost | Shared local server, testing |
| **remote** | gRPC server with TLS + JWT auth | Team deployment, remote GPU |

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LOCAL MODE (default)                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐             │
│  │ CLI (REPL)  │───▶│  ChatAgent  │───▶│   Ollama    │             │
│  └─────────────┘    └─────────────┘    └─────────────┘             │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                     STANDALONE / REMOTE MODE                        │
│                                                                     │
│  CLIENT                          SERVER                             │
│  ┌─────────────┐                ┌─────────────────────────────────┐ │
│  │ CLI (REPL)  │                │  PenguinCode gRPC Server        │ │
│  │             │   gRPC/TLS     │  ┌───────────┐  ┌───────────┐  │ │
│  │ ┌─────────┐ │◀──────────────▶│  │ ChatAgent │  │  Ollama   │  │ │
│  │ │ Local   │ │                │  └───────────┘  └───────────┘  │ │
│  │ │ Tools   │ │                │  ┌───────────┐  ┌───────────┐  │ │
│  │ └─────────┘ │                │  │  Memory   │  │ Docs RAG  │  │ │
│  └─────────────┘                │  └───────────┘  └───────────┘  │ │
│                                 └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

### gRPC Services

All services are defined in `penguincode/proto/penguincode.proto`:

| Service | Description |
|---------|-------------|
| `AuthService` | JWT token generation and API key validation |
| `ChatService` | Streaming chat with agent orchestration |
| `ToolCallbackService` | Bidirectional streaming for local tool execution |
| `HealthService` | Liveness and readiness probes |

### Server Components (`penguincode/server/`)

```
penguincode/server/
├── main.py              # gRPC server entry point
├── interceptors.py      # JWT validation interceptor
└── services/
    ├── auth.py          # JWT/API key authentication
    ├── chat.py          # ChatService implementation
    ├── tools.py         # Tool callback handler
    └── health.py        # Health check service
```

### Client Components (`penguincode/client/`)

```
penguincode/client/
├── grpc_client.py       # gRPC connection and streaming
├── tool_executor.py     # Local tool execution (read, write, bash, etc.)
└── auth.py              # Token storage and refresh
```

## Chat Session Storage

`ChatService` (`CreateSession`/`Chat`/`GetHistory`/`CloseSession`) persists
session state to the shared `penguincode` Postgres schema
(`penguincode_cli/sessions/store.py`, table `chat_sessions` --
`db/migrations/0007_chat_sessions.sql`) rather than an in-process dict.
This closes a High-severity cross-pod gap: prod runs `replicas=3`, and
with per-process state a session created on one pod 404'd on every other
pod, and any rolling deploy dropped every in-flight session. Any pod can
now serve any RPC for any session -- `Chat` loads the persisted
conversation at the start of the call and saves it back after the turn,
never holding a DB connection open across the LLM call itself.

- **Scope**: a session is visible to its own tenant AND its owning user
  only (never team- or tenant-shared) -- derived from the caller's
  WaddleAI JWT `ScopeContext`, now required by default for every
  `ChatService` RPC (same RS256 gate `KnowledgeService`/`LessonsService`
  already enforce, see [`CONFIGURATION.md`](./CONFIGURATION.md#chatservice-rs256-gate)).
  Falls back to a fixed single-tenant scope keyed by the legacy HS256
  token's `sub` only when the `penguincode.disable-chat-rs256-gate` opt-out
  kill switch is ON -- an emergency rollback, not the steady-state default.
- **TTL**: `PENGUINCODE_SESSION_TTL_SECONDS` (default 24h) -- refreshed on
  every `Chat`/update; a background sweeper
  (`PENGUINCODE_SESSION_SWEEP_INTERVAL_SECONDS` /
  `_SWEEP_BATCH_SIZE`) deletes expired rows in bounded batches.
  See [`CONFIGURATION.md`](./CONFIGURATION.md#chat-session-storage).
- **Kill switch**: the `penguincode.disable-shared-sessions` PostHog flag
  (opt-out, unseen/OFF = the shared-Postgres mechanism is active) reverts
  to an in-process-only store as an emergency rollback.
- **Observability**: `active_sessions` gauge + `session_store_duration_seconds`
  histogram (label: `op`), emitted from `penguincode_cli/sessions/metrics.py`.

## Tool Execution Model

In remote mode, tools execute **locally on the client** for security:

1. Server sends `ToolCallRequest` via bidirectional stream
2. Client executes tool locally (file read, bash command, etc.)
3. Client sends `ToolCallResponse` back to server
4. Server continues agent processing with tool result

**Local tools** (execute on client):
- `read` - Read files
- `write` - Write files
- `edit` - Edit files
- `bash` - Execute commands
- `grep` - Search file contents
- `glob` - Find files by pattern

**Server tools** (execute on server):
- `web_search` - Research via DuckDuckGo/MCP
- `memory` - mem0 context retrieval
- `docs_rag` - Documentation lookup

## Configuration

Configuration is in `config.yaml`:

```yaml
# Server Configuration
server:
  mode: "local"           # local | standalone | remote
  host: "localhost"       # Bind address
  port: 50051             # gRPC port
  tls_enabled: false      # Enable TLS
  tls_cert_path: ""       # TLS certificate
  tls_key_path: ""        # TLS private key

# Authentication (remote mode)
auth:
  enabled: false          # Enable JWT authentication
  jwt_secret: "${PENGUINCODE_JWT_SECRET}"
  token_expiry: 3600      # 1 hour
  refresh_expiry: 86400   # 24 hours
  api_keys:
    - "${PENGUINCODE_API_KEY}"

# Client Configuration
client:
  server_url: ""          # Remote server URL
  token_path: "~/.penguincode/token"
  local_tools:            # Tools that run on client
    - read
    - write
    - edit
    - bash
    - grep
    - glob
```

## Running the Server

### Standalone Mode (localhost)

```bash
# Start server
python -m penguincode.server.main

# In another terminal, use CLI
penguincode chat --server localhost:50051
```

### Docker Deployment

```bash
# Server only (uses host Ollama)
docker compose up -d

# Server with bundled Ollama
docker compose --profile with-ollama up -d
```

### Remote Mode with TLS

```bash
# Generate certificates
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes

# Set environment
export PENGUINCODE_JWT_SECRET="your-secret-key"
export PENGUINCODE_API_KEY="your-api-key"

# Update config.yaml
server:
  mode: "remote"
  tls_enabled: true
  tls_cert_path: "/path/to/cert.pem"
  tls_key_path: "/path/to/key.pem"

auth:
  enabled: true
```

## Authentication Flow

```
┌────────┐                              ┌────────┐
│ Client │                              │ Server │
└────┬───┘                              └────┬───┘
     │                                       │
     │  1. Authenticate(api_key)             │
     │──────────────────────────────────────▶│
     │                                       │
     │  2. AuthResponse(access_token,        │
     │     refresh_token)                    │
     │◀──────────────────────────────────────│
     │                                       │
     │  3. Chat(token in metadata)           │
     │──────────────────────────────────────▶│
     │                                       │
     │  4. Streaming responses               │
     │◀ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ │
     │                                       │
     │  5. RefreshToken(refresh_token)       │
     │──────────────────────────────────────▶│
     │                                       │
     │  6. New access_token                  │
     │◀──────────────────────────────────────│
```

## Ollama-Embedding Bulkhead

The in-cluster Ollama instance serves two fundamentally different workload
shapes from one process: real-time chat completions (low-latency,
interactive) and bulk embedding (document/code indexing, GraphRAG query
embedding, mem0 memory embedder) -- high-throughput, bursty. With no QoS
separation between them, a document-indexing job can saturate Ollama's
GPU/CPU and degrade or time out live chat requests cluster-wide.

```
                 ┌──────────────────────┐
  chat requests ─┤  Ollama (chat)       │  always the single shared instance
                 │  OLLAMA_API_URL      │
                 └──────────────────────┘

  default (PENGUINCODE_EMBEDDING_OLLAMA_URL unset): embedding calls
  share the SAME instance above -- today's behavior, unchanged.

  opt-in bulkhead (PENGUINCODE_EMBEDDING_OLLAMA_URL set):
                 ┌──────────────────────┐
  doc/code index ─┤  Ollama (embeddings) │  dedicated Deployment
  GraphRAG query ─┤  OLLAMA_EMBEDDING_   │  (k8s/helm/waddleai's
  mem0 embedder  ─┤  OLLAMA_URL          │   ollamaEmbeddings.enabled)
                 └──────────────────────┘
```

- **Opt-in, safe-by-default**: unset `PENGUINCODE_EMBEDDING_OLLAMA_URL` (or
  an unset/disabled `ollamaEmbeddings` Helm value) means embedding calls
  fall back to the single chat-serving Ollama exactly as before -- nothing
  changes unless you explicitly configure the dedicated endpoint.
- **Resolution**: `config.settings.resolve_embedding_url()` is the single
  fallback chokepoint every embedding call site uses (doc indexing,
  GraphRAG, mem0 memory) -- see
  [`docs/penguincode/CONFIGURATION.md`](CONFIGURATION.md#ollama-embedding-bulkhead-penguincode_embedding_ollama_url)
  for the env var and
  [`docs/penguincode/k8s-deployment.md`](k8s-deployment.md) for the Helm
  deployment.
- **Observability**: every embedding call emits
  `penguincode.embedding_call.duration`/`.events` (OTel), labeled
  `endpoint=chat_ollama` or `endpoint=embedding_ollama` -- so saturation of
  each instance is independently visible rather than conflated into one
  signal.

## Security Considerations

1. **TLS Required for Remote**: Always enable TLS for non-localhost deployments
2. **JWT Expiry**: Access tokens expire in 1 hour; use refresh tokens
3. **Local Tool Execution**: Sensitive operations run on client, not server
4. **API Key Rotation**: Support multiple API keys for rotation
5. **No Credential Storage**: Server never stores user credentials

## Future Enhancements

- HTTP/REST fallback for environments that only allow HTTP/1.1
- WebSocket transport for browser-based clients
- Multi-tenant session isolation
- Rate limiting and quota management
- Audit logging for compliance

## Related Documentation

- [Usage Guide](USAGE.md) - Installation and basic usage
- [Agent Architecture](AGENTS.md) - ChatAgent, Explorer, Executor details
- [MCP Integration](MCP.md) - Extending with MCP servers
- [Security](SECURITY.md) - Security best practices
