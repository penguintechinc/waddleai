# PenguinCode Knowledge Platform

## Overview

PenguinCode's knowledge platform combines three graph-based knowledge stores (code, knowledge, memory) with a retrieval-augmented generation (RAG) pipeline for documentation. All data persists in a shared **PostgreSQL with pgvector** instance, scoped by tenant and visibility. The platform gates all features behind **PostHog feature flags**, defaulted OFF until explicitly enabled per environment.

## Architecture

### Three Knowledge Graphs

All three graphs share a single `GraphStore` driver (Postgres default) and are queryable via REST or gRPC. Each graph has separate **node_type** namespaces within the same tables:

| Graph | Purpose | Source | Node Types | Use Case |
|---|---|---|---|---|
| **Code** | AST + dependency trees via tree-sitter | Indexing `IndexCode` RPC or `/docs/index` | `file`, `function`, `class`, `variable` | Codebase navigation, dependency analysis |
| **Knowledge** | Extracted entities + relationships from docs | `KnowledgeService` LLM triple extraction | `concept`, `pattern`, `library`, `framework` | Q&A, semantic search over docs |
| **Memory** | Lessons learned + team insights via mem0 | `/lesson promote|approve|reject` + auto-extraction | `lesson`, `pattern`, `insight` | Persistent institutional knowledge |

All nodes include: `id` (UUID), `tenant_id` (hard boundary), `org_id` (optional), `team_id` (optional), `owner_user_id` (optional), **visibility** (user\|team\|tenant), `created_at`. Edges reference source/target node IDs and carry **weight** (relevance score) + **relation_type** metadata.

### Scope & Visibility Model

**Tenant = firm** (hard boundary, immutable); **team** = client engagement (shared within team); **visibility**:
- `user` — only the author sees it
- `team` — the author's team sees it
- `tenant` — firm-wide, visible after lessons promotion approval

**Queries always scoped to tenant + authenticated user's visibility** (SQL checked at `GraphStore.neighbors()` / `.subgraph()`).

### GraphRAG Hybrid Retrieval

Combines dense (pgvector embeddings) + sparse (graph traversal) search:

1. **Dense search**: embed user query, find top-k similar nodes by cosine distance
2. **Graph traversal**: follow edges from top-k nodes to expand context (BFS, depth-limited)
3. **Rerank** by combined score (embedding distance + graph centrality)

Invoked via `IndexQueryService.query()` (gRPC) or `POST /api/v1/knowledge/query`.

## Components

### Database Migrations

Located: `penguincode_cli/db/migrations/*.sql` (idempotent SQL, applied via `python -m penguincode_cli.db.migrate`):

| File | Purpose |
|---|---|
| `0001_schema_and_extension.sql` | Create `penguincode` schema; enable pgvector |
| `0002_docs_vectors.sql` | `docs_vectors` — embeddings for RAG docs cache |
| `0003_memory_vectors.sql` | `memory_vectors` — mem0 pgvector backend |
| `0004_graph_nodes.sql` | `graph_nodes` (code, knowledge, memory) |
| `0005_graph_edges.sql` | `graph_edges` with uniqueness + cascade delete |
| `0006_pending_lessons.sql` | `pending_lessons` — approval queue for tenant-wide promotion |

All tables soft-scoped to tenant (queried via `WHERE tenant_id`); `graph_nodes.visibility` determines per-row visibility.

### gRPC Services

Server runs at port 50051 (configurable `PENGUINCODE_SERVER_PORT`). Proto: `penguincode_cli/proto/penguincode.proto`. Services:

| Service | RPC | Purpose |
|---|---|---|
| `KnowledgeService` | `Index(IndexRequest)` | Trigger docs/codebase indexing; returns job ID |
| | `Query(QueryRequest)` | Hybrid search (embedding + graph) |
| | `IndexStatus(IndexStatusRequest)` | Job status (pending\|running\|done\|failed) |
| | `ClearIndex(ClearIndexRequest)` | Flush all indexed data for a tenant |
| | `CleanupIndex(CleanupIndexRequest)` | Archive old data, vacuum |
| `LessonsService` | `MemoryAdd(MemoryAddRequest)` | Capture a lesson (returns ID, status=pending) |
| | `MemorySearch(MemorySearchRequest)` | Search lessons by embedding + scope |
| | `ListPending(ListPendingRequest)` | Tenant-wide review queue (status=pending) |
| | `Approve(ApproveRequest)` | Mark as approved → tenant-visible |
| | `Reject(RejectRequest)` | Reject with reason |
| `AuthService` | `Authenticate(AuthRequest)` | Exchange API key → JWT access token |
| | `ValidateToken(ValidateRequest)` | Check token validity, scopes |
| | `RefreshToken(RefreshRequest)` | Mint new access token |

### Documentation RAG

Indexes project docs (Sphinx, MkDocs, markdown trees) into `docs_vectors` on demand:

1. **Auto-detect** language/framework via filesystem (pyproject.toml, package.json, go.mod, etc.)
2. **Fetch official docs** from CDN/registry
3. **Chunk by semantic boundaries** (section headers, code blocks)
4. **Embed each chunk** via Ollama (default: `nomic-embed-text`)
5. **Store in pgvector**, scoped to tenant+user

Triggered via `POST /docs/index` REST (blocks) or `KnowledgeService.Index` gRPC (async job).

### Lessons-Learned Promotion

Team members propose lessons for firm-wide sharing:

1. **Capture**: `/lesson add "..."` (CLI) or `MemoryAdd` (gRPC) → stored in `pending_lessons` as status=pending
2. **Scrub**: `lessons/scrub.py` generalize + verify client-agnostic; if CLEAN, stay pending; if BLOCKED (contains PII/secrets/confidential), reject
3. **Tenant Review**: `/lesson pending` lists pending for tenant admins; `/lesson approve <id>` after human review
4. **Live**: approved lessons copied to `memory_vectors` with visibility=tenant; now searchable via `MemorySearch`

Scope enforcement: proposer+team recorded; approval is tenant-admin action; final lesson visible to all team members.

### Flags

All features behind **PostHog feature flags** (Community Edition, env-configurable `POSTHOG_HOST`/`POSTHOG_KEY`). Default OFF; env override via `PENGUINCODE_FLAG_<NAME>`:

| Flag | Env Override | Default | Gates |
|---|---|---|---|
| `penguincode.rag` | `PENGUINCODE_FLAG_RAG` | OFF | `POST /docs/index`, docs embedding |
| `penguincode.code-graph` | `PENGUINCODE_FLAG_CODE_GRAPH` | OFF | Tree-sitter indexing, code AST |
| `penguincode.knowledge-graph` | `PENGUINCODE_FLAG_KNOWLEDGE_GRAPH` | OFF | LLM triple extraction, concept graph |
| `penguincode.memory-graph` | `PENGUINCODE_FLAG_MEMORY_GRAPH` | OFF | Mem0 extraction, institutional memory |
| `penguincode.lessons-promotion` | `PENGUINCODE_FLAG_LESSONS_PROMOTION` | OFF | `/lesson` commands, approval queue |

Client (gRPC + REST): check flag before invoking service. Service (gRPC): re-check flag on receive (fail-closed); client re-check is performance, not security.

### Authentication

Server-side JWT validation via **JWKS or static public key**; client obtains token via machine-key exchange. No self-signed tokens in production.

**JWKS-based** (preferred, external issuer):
```bash
export WADDLEAI_JWT_JWKS_URL="https://issuer.example.com/.well-known/jwks.json"
export WADDLEAI_JWT_ISSUER="https://issuer.example.com"
export WADDLEAI_JWT_AUDIENCE="penguincode"
```

**Static public key** (dev/local):
```bash
export WADDLEAI_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----..."
# or
export WADDLEAI_JWT_PUBLIC_KEY_FILE="/etc/secrets/jwt-pubkey.pem"
```

**Headless/CI machine mode** (no interactive login):
```bash
export WADDLEAI_API_KEY="sk-..."  # or WADDLEAI_API_KEY_FILE
```
Exchanged for JWT via `POST /api/v1/auth/token`, then used for all subsequent gRPC/REST calls.

Precedence: machine key → interactive login → local dev (fail-closed to PenguinTech domains).

### Observability (OTel)

All signals (logs, metrics, traces) via OpenTelemetry; endpoint env-configurable:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318"
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_SERVICE_NAME="penguincode"
```

Logs: structured via `penguintechinc_utils.logging` + automatic PII redaction. Metrics: histograms (indexing latency, query time), counters (documents indexed). Traces: span per gRPC call + DB query.

## Deployment

### Prerequisites

- **PostgreSQL 17+** with `pgvector` extension enabled
- **Ollama** accessible at `OLLAMA_API_URL` (default: `http://localhost:11434`)
- **WaddleAI auth server** reachable for JWKS (or static key supplied)

### Helm Chart

Located: `services/penguincode/k8s/helm/penguincode`

**Pre-install**: Run migration Job to create schema/tables:
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: penguincode-migrate
  namespace: penguincode
spec:
  template:
    spec:
      containers:
      - name: migrate
        image: ghcr.io/penguintechinc/waddleai/penguincode:beta-<epoch>
        command: ["python3", "-m", "penguincode_cli.db.migrate"]
        env:
        - name: PGVECTOR_URL
          valueFrom:
            secretKeyRef:
              name: penguincode-postgres
              key: dsn
      restartPolicy: Never
```

**Values** (alpha.yml / beta.yml):
```yaml
flags:
  posthog:
    host: "https://license.penguintech.io"
    existingSecret: "posthog-key"  # kubectl create secret generic posthog-key --from-literal=api-key=...

server:
  env:
    PGVECTOR_URL: "postgres://penguincode:...@postgres.waddleai:5432/waddleai"
    PENGUINCODE_MEMORY_STORE: "pgvector"
    PENGUINCODE_MEMORY_ENABLED: "true"
    WADDLEAI_JWT_JWKS_URL: "https://waddleai.example.com/.well-known/jwks.json"
    WADDLEAI_JWT_ISSUER: "https://waddleai.example.com"
    WADDLEAI_JWT_AUDIENCE: "penguincode"
    OTEL_EXPORTER_OTLP_ENDPOINT: "http://collector.monitoring:4318"
    # Flags
    PENGUINCODE_FLAG_RAG: "true"
    PENGUINCODE_FLAG_CODE_GRAPH: "true"
    PENGUINCODE_FLAG_KNOWLEDGE_GRAPH: "false"  # Coming soon
    PENGUINCODE_FLAG_MEMORY_GRAPH: "true"
    PENGUINCODE_FLAG_LESSONS_PROMOTION: "true"
```

**NetworkPolicy**: Cross-namespace access to Postgres, Ollama (set via `cilium.topology.penguincodeIngress` in waddleai chart).

### Least-Privilege DB Role

```sql
-- Create read-write role for penguincode
CREATE ROLE penguincode WITH LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA penguincode TO penguincode;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA penguincode TO penguincode;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA penguincode TO penguincode;
-- pgvector ops
GRANT EXECUTE ON FUNCTION vector(text) TO penguincode;
```

## Operator Runbook

### Indexing Stalls or Fails

Check `penguincode.schema_migrations` for applied jobs:
```sql
SELECT * FROM penguincode.schema_migrations ORDER BY applied_at DESC;
```

Trigger manual reindex:
```bash
kubectl -n penguincode exec -it deploy/penguincode-server -- \
  python3 -c "from penguincode_cli.server.services.knowledge import KnowledgeService; await KnowledgeService().Index(...)"
```

### JWKS Cache Stale

JWKS cached for 5 min (configurable `WADDLEAI_JWT_JWKS_CACHE_TTL_SECONDS`). Force refresh by restarting pod:
```bash
kubectl -n penguincode rollout restart deploy/penguincode-server
```

### Lessons Not Appearing

Check flag is enabled: `PENGUINCODE_FLAG_LESSONS_PROMOTION=true`. If approved lessons don't show in queries, check `memory_vectors.visibility='tenant'`:
```sql
SELECT id, visibility, created_at FROM penguincode.memory_vectors
  WHERE tenant_id = '...' ORDER BY created_at DESC;
```

### Query Performance

Check graph traversal cost. If slow, increase BFS depth limit (default 2):
```bash
kubectl set env deploy/penguincode-server PENGUINCODE_GRAPH_DEPTH=3
```

## See Also

- [Memory Integration](MEMORY.md) — mem0 backends (pgvector, Qdrant)
- [Configuration Reference](CONFIGURATION.md) — env vars, feature flags, auth modes
- [Architecture](ARCHITECTURE.md) — gRPC server, client modes, deployment patterns
