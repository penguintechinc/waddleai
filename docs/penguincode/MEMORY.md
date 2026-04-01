# PenguinCode Memory Integration

## Overview

PenguinCode uses **mem0**, an open-source memory layer, to provide persistent contextual memory across conversations. This enables the AI to remember user preferences, project context, architectural decisions, and conversation history to deliver more personalized and coherent assistance.

Memory is stored in a vector database and uses semantic search to retrieve relevant context, making interactions more intelligent and contextualized over time.

## What Gets Stored in Memory

Memory captures critical information for enhanced conversations:

- **User Preferences**: Coding style, language preferences, preferred tools and frameworks
- **Project Context**: Architecture decisions, technology choices, naming conventions, project structure
- **Development Decisions**: Feature flags, deprecated patterns, architectural patterns to avoid
- **Conversation History**: Important discussion points, solutions to past problems, lessons learned
- **Domain Knowledge**: Domain-specific terminology, business rules, integration patterns used
- **Performance Patterns**: Optimization approaches that worked, bottlenecks encountered

All memories are tagged with `user_id` for isolation and personalization.

## Vector Store Backends

### ChromaDB (Default)

**Best for**: Local development, single-machine deployments

```yaml
memory:
  enabled: true
  vector_store: "chroma"

  stores:
    chroma:
      path: "./.penguincode/memory"
      collection: "penguincode_memory"
```

- Local SQLite-based vector database
- No external dependencies or network setup
- Data persisted in `./.penguincode/memory` directory
- Suitable for development and small teams

### Qdrant

**Best for**: Scalable, self-hosted, or cloud deployments

```yaml
memory:
  enabled: true
  vector_store: "qdrant"

  stores:
    qdrant:
      url: "http://localhost:6333"
      collection: "penguincode_memory"
```

- Standalone vector database service
- Self-hosted or managed cloud options
- Horizontal scaling capability
- Production-ready with REST API

**Running Qdrant locally**:
```bash
docker run -p 6333:6333 qdrant/qdrant:latest
```

### PGVector

**Best for**: Existing PostgreSQL infrastructure

```yaml
memory:
  enabled: true
  vector_store: "pgvector"

  stores:
    pgvector:
      connection_string: "${PGVECTOR_URL}"  # postgres://user:pass@host/db
      table: "penguincode_memory"
```

- Uses PostgreSQL pgvector extension
- Integrates with existing database infrastructure
- Set `PGVECTOR_URL` environment variable
- Requires pgvector extension: `CREATE EXTENSION IF NOT EXISTS vector;`

## Configuration

### Memory Settings in config.yaml

```yaml
memory:
  enabled: true                      # Enable/disable memory entirely
  vector_store: "chroma"             # chroma | qdrant | pgvector
  embedding_model: "nomic-embed-text" # Ollama embedding model

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

### Disabling Memory

Set `memory.enabled: false` in config.yaml or via environment:
```bash
PENGUINCODE_MEMORY_ENABLED=false
```

## Embedding Model

**Default**: `nomic-embed-text` (via Ollama)

- Lightweight, fast embeddings (1.4B parameters)
- Excellent semantic understanding
- Compatible with all vector stores
- Uses local Ollama instance configured in `ollama.api_url`

Alternative models:
- `mxbai-embed-large` - Larger, higher quality (384-dim)
- `all-minilm` - Minimal overhead (384-dim)

Configure via:
```yaml
memory:
  embedding_model: "mxbai-embed-large"
```

## How Memory Enhances Conversations

### Initialization
1. Memory manager loads with configured vector store
2. Embedding model starts via Ollama
3. LLM model (from `models.orchestration`) initialized for memory operations

### Retrieval
Before responding, PenguinCode:
1. Searches memory for relevant context using semantic similarity
2. Limits to 5 most relevant memories by default
3. Injects context into system prompt for LLM awareness
4. Uses memory context to provide personalized responses

### Storage
After exchanges:
1. Extracts important facts and decisions from conversation
2. Generates semantic embeddings for each memory
3. Stores with user_id, timestamp, and metadata
4. Enables future semantic search and retrieval

## Memory Retrieval During Chat

**Automatic Retrieval Process**:
```python
memories = await memory_manager.search_memories(
    query=user_input,
    user_id=session_id,
    limit=5
)
# Memories ranked by relevance score
# Top matches injected into context
```

**What's Searched**:
- Current user input semantically matched against all stored memories
- Temporal recency considered (recent memories weighted higher)
- Metadata filters available (type, domain, tags)

## Memory Extraction & Storage

**Extraction Trigger**: After each significant exchange

```python
# Example memory extraction
await memory_manager.add_memory(
    content="User prefers Flask + PyDAL for Python backends",
    user_id=session_id,
    metadata={
        "type": "preference",
        "domain": "frameworks",
        "tags": ["flask", "python"]
    }
)
```

**What Gets Extracted**:
- Explicit preferences stated by user
- Technical decisions made during session
- Architectural patterns discussed
- Debugging solutions discovered
- Integration patterns used

## Auto-Compaction and Cleanup

Memory management operations:

```python
# Get all memories for a user
memories = await memory_manager.get_all_memories(user_id)

# Update memory with refined content
await memory_manager.update_memory(
    memory_id=memory_id,
    content="Updated content",
    metadata={"refined": True}
)

# Delete specific memory
await memory_manager.delete_memory(memory_id)

# Delete all memories (reset)
await memory_manager.delete_all_memories(user_id)
```

**Compaction Strategy**:
- Older, less-relevant memories automatically weighted lower
- Duplicate concepts merged through semantic similarity
- Metadata tags used for organization and filtering
- Manual updates refine memory content over time

## Data Privacy Considerations

### User Isolation
- Memories strictly isolated by `user_id`
- No cross-user memory access
- Session-based user_id prevents unauthorized access

### Data Storage
- **ChromaDB**: Local file system only, full user control
- **Qdrant**: Network-accessible, use TLS in production
- **PGVector**: Stored in PostgreSQL, subject to database security

### Sensitive Information
- Avoid storing credentials, API keys, or secrets
- Exclude PII unless necessary for context
- Use generic references for sensitive data
- Clear memory if containing sensitive data

### Data Retention
- No automatic expiration by default
- Manually delete memories as needed
- `delete_all_memories()` wipes user's memory
- Consider GDPR/privacy regulations for your use case

## Clearing and Resetting Memory

### Clear All Memories for User
```python
await memory_manager.delete_all_memories(user_id="session-123")
```

### Delete Specific Memory
```python
await memory_manager.delete_memory(memory_id="mem-456")
```

### Reset Full Database
Remove the vector store directory:
```bash
# ChromaDB
rm -rf ./.penguincode/memory

# Qdrant (via API)
curl -X DELETE http://localhost:6333/collections/penguincode_memory
```

### Disable Memory Temporarily
```bash
PENGUINCODE_MEMORY_ENABLED=false penguincode chat
```

## Memory API Reference

### MemoryManager Class

```python
from penguincode.tools.memory import MemoryManager

manager = MemoryManager(config, ollama_url)

# Search relevant memories
results = await manager.search_memories(query, user_id, limit=5)

# Store new memory
result = await manager.add_memory(content, user_id, metadata={})

# Get all memories
all_memories = await manager.get_all_memories(user_id)

# Update memory
updated = await manager.update_memory(memory_id, new_content)

# Delete operations
await manager.delete_memory(memory_id)
await manager.delete_all_memories(user_id)

# Check status
is_enabled = manager.is_enabled()
```

---

**Last Updated**: 2025-12-28
**PenguinCode Version**: 1.3.0+
**Memory Framework**: mem0 (open-source)
