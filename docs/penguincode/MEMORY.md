# PenguinCode Memory Integration

## Overview

PenguinCode uses **mem0**, an open-source memory layer, to provide persistent contextual memory across conversations. Memory is stored in a vector database (pgvector or Qdrant) shared with the WaddleAI platform, scoped by tenant and visibility. Semantic search retrieves relevant context automatically.

## What Gets Stored in Memory

- **Interaction history** — past solutions, debugging steps, code examples
- **User preferences** — favorite tools, coding patterns, framework choices
- **Project context** — architecture decisions, naming conventions, key libraries
- **Lessons learned** — patterns that worked, anti-patterns to avoid, team insights
- **Domain knowledge** — terminology, business rules, integration patterns

All memories tagged with `user_id` and `tenant_id` for isolation.

## Vector Store Backends

### PGVector (Platform Default)

**Best for**: Production, shared infrastructure, multi-tenant deployments

Stores embeddings in PostgreSQL `penguincode.memory_vectors` table. No external service dependency; native pgvector ops (cosine distance, indexing).

```yaml
memory:
  enabled: true
  vector_store: "pgvector"
  stores:
    pgvector:
      connection_string: "${PGVECTOR_URL}"  # postgres://user:pass@host:5432/waddleai
      collection: "penguincode_memory"
```

**Prerequisites**: PostgreSQL 15+ with pgvector extension enabled.

### Qdrant

**Best for**: Standalone deployments, scalability, managed cloud

Standalone vector database service with REST API.

```yaml
memory:
  enabled: true
  vector_store: "qdrant"
  stores:
    qdrant:
      url: "http://localhost:6333"
      collection: "penguincode_memory"
```

**Running locally**:
```bash
docker run -p 6333:6333 qdrant/qdrant:latest
```

## Configuration

### Memory Settings in config.yaml

```yaml
memory:
  enabled: true                      # Enable/disable memory entirely
  vector_store: "pgvector"           # pgvector | qdrant
  embedding_model: "nomic-embed-text" # Ollama embedding model

  stores:
    pgvector:
      connection_string: "${PGVECTOR_URL}"
      collection: "penguincode_memory"

    qdrant:
      url: "http://localhost:6333"
      collection: "penguincode_memory"
```

### Disabling Memory

```bash
export PENGUINCODE_MEMORY_ENABLED=false
```

## Embedding Model

**Default**: `nomic-embed-text` (1.4B params, 768-dim, via Ollama)

Lightweight, fast, excellent semantic understanding. Used by mem0 for all vector operations.

**Alternative models**:
- `embeddinggemma:768` (768-dim, high quality)
- `mxbai-embed-large` (384-dim, larger)

Configure via:
```yaml
memory:
  embedding_model: "embeddinggemma:768"
```

## Scope & Visibility

Memory scoped by:
- **Tenant** (hard boundary) — different orgs never cross-contaminate
- **Team** (optional, scope narrower) — shared within team
- **User** (optional, narrowest) — only the author sees it
- **Visibility** — `user` | `team` | `tenant`

**Default for new memories**: team-scoped (visibility=team) — "we all learn" model. Team members proposing lessons for firm-wide sharing promote via `/lesson approve` for tenant visibility.

## How Memory Enhances Conversations

### Initialization

1. Memory manager loads with configured backend (pgvector/Qdrant)
2. Embedding model starts via Ollama
3. LLM model from `models.orchestration` initialized for memory operations

### Retrieval (Before Responding)

1. Embed user input via `PENGUINCODE_EMBEDDING_MODEL`
2. Query vector store (tenant + visibility scope) for top-k similar memories (default k=5)
3. Rank by cosine distance
4. Inject matches into system prompt for LLM awareness

### Storage (After Exchanges)

1. Extract important facts/decisions from conversation
2. Embed extracted memory
3. Store with `user_id`, `tenant_id`, metadata, timestamp
4. Future queries find it via semantic similarity

## Data Privacy

### User Isolation

Memories strictly isolated by `user_id` and `tenant_id` — no cross-tenant access.

### Data Storage

- **PGVector**: Stored in PostgreSQL, subject to DB encryption + access controls
- **Qdrant**: Network-accessible, use TLS in production

### Sensitive Information

Avoid storing:
- Credentials, API keys, secrets
- PII (names, emails, IDs) — use UUIDs where possible
- Confidential business logic — lessons promotion includes a PII/confidentiality scrubber

## Memory API Reference

### Python Interface

```python
from penguincode.tools.memory import MemoryManager

# Search relevant memories
results = await memory_manager.search_memories(
    query=user_input,
    user_id=session_id,
    limit=5
)

# Add new memory
result = await memory_manager.add_memory(
    content="Lesson learned",
    user_id=session_id,
    metadata={"type": "lesson", "topic": "auth"}
)

# List all memories for user
all_memories = await memory_manager.get_all_memories(user_id)

# Update memory content
updated = await memory_manager.update_memory(
    memory_id=mem_id,
    content="Refined content"
)

# Delete operations
await memory_manager.delete_memory(memory_id=mem_id)
await memory_manager.delete_all_memories(user_id=session_id)

# Status
is_enabled = manager.is_enabled()
```

### gRPC Interface

```protobuf
service LessonsService {
  rpc MemoryAdd(MemoryAddRequest) returns (MemoryAddResponse);
  rpc MemorySearch(MemorySearchRequest) returns (MemorySearchResponse);
  rpc ListPending(ListPendingRequest) returns (ListPendingResponse);
  rpc Approve(ApproveRequest) returns (ApproveResponse);
  rpc Reject(RejectRequest) returns (RejectResponse);
}
```

## Lessons-Learned Promotion

Team members can propose lessons for firm-wide sharing:

```bash
# Capture lesson
/lesson add "Don't use bare except clauses"

# List pending review
/lesson pending

# Approve for firm-wide visibility
/lesson approve <lesson_id>

# Reject with explanation
/lesson reject <lesson_id> "Too specific to this project"
```

All lessons scrubbed for confidentiality (PII, secrets, confidential context removed) before approval. Approved lessons become `visibility=tenant` and searchable by all team members.

## Clearing Memory

### Clear All Memories for User

```bash
PENGUINCODE_MEMORY_ENABLED=false penguincode chat
```

Or programmatically:
```python
await memory_manager.delete_all_memories(user_id="session-123")
```

### Reset Entire Database

**PGVector**:
```sql
DELETE FROM penguincode.memory_vectors;
```

**Qdrant** (via API):
```bash
curl -X DELETE http://localhost:6333/collections/penguincode_memory
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PENGUINCODE_MEMORY_ENABLED` | `true` | Enable/disable memory |
| `PENGUINCODE_MEMORY_STORE` | `pgvector` | Backend: pgvector, qdrant |
| `PENGUINCODE_EMBEDDING_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `PGVECTOR_URL` | — | PostgreSQL connection string |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |

---

**Last Updated**: 2026-09-26
**Memory Framework**: mem0 (open-source)
**Default Backend**: pgvector (shared WaddleAI PostgreSQL)
