# Memory Systems Integration

WaddleAI uses mem0 and pgvector (PostgreSQL's vector extension) to provide persistent conversation memory, semantic search, and intelligent context retention across sessions.

> **Note:** WaddleAI previously supported a ChromaDB memory backend. It was removed (PYSEC-2026-311, a pre-authentication code injection vulnerability in chromadb's server component with no fixed release) -- pgvector and mem0 already cover the same ground. If you were running with `backend="chromadb"`, switch to `backend="pgvector"` (the default) or `backend="mem0"`; there is no automated migration tool, so re-index/re-populate memory from source data after switching.

## Overview

WaddleAI's memory system automatically:

- **Stores all conversations** - Every request/response is saved with full metadata
- **Enables semantic search** - Find past conversations by meaning, not just keywords
- **Preserves context** - Models can access relevant past conversations
- **Tracks routing decisions** - Understand why requests were routed to specific models
- **Supports analytics** - Query conversation patterns and usage trends

## Architecture

```
┌─────────────────┐
│  WaddleAI Proxy │
└────────┬────────┘
         │ All requests/responses
         ▼
┌─────────────────────────────────────┐
│     Memory Integration Layer        │
│  (shared/utils/memory_integration)  │
└──────────────┬──────────────────────┘
               │
      ┌────────┴────────┐
      ▼                 ▼
┌──────────┐      ┌──────────┐
│   mem0   │      │ pgvector │
│ (Python) │◄────►│(PostgreSQL)│
└──────────┘      └──────────┘
      │
      └─► Semantic embeddings
          Context search
          Conversation clustering
```

## Configuration

### Vector Storage

pgvector runs as a PostgreSQL extension on the same database WaddleAI already
depends on -- there is no separate memory container to deploy, unlike the
former ChromaDB backend. Enable the extension once per database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### Environment Variables

Configure memory systems in `.env.dev`:

```bash
# mem0 configuration (optional; defaults to the pgvector backend)
MEM0_ENABLED=true

# Conversation retention (days)
CONVERSATION_RETENTION_DAYS=90

# Memory collection name
MEMORY_COLLECTION_NAME=waddleai_conversations

# Embedding model for semantic search
EMBEDDING_MODEL=text-embedding-ada-002
```

## Usage

### Automatic Conversation Storage

All requests through WaddleAI are automatically stored:

```python
# This happens automatically for every request
conversation_entry = {
    "user_id": user.id,
    "organization_id": user.organization_id,
    "api_key_id": api_key.id,
    "model_requested": request_model,
    "model_used": selected_model,
    "messages": messages,
    "response": response_text,
    "routing_decision": routing_info.get("routing_decision"),
    "routing_reasoning": routing_info.get("routing_reasoning"),
    "request_type": routing_info.get("request_type"),
    "waddleai_tokens": waddleai_token_usage,
    "llm_tokens": llm_token_usage,
    "latency_ms": request_duration_ms,
    "timestamp": datetime.utcnow(),
    "metadata": {...}
}

# Stored in mem0/pgvector
memory_integration.store_conversation(conversation_entry)
```

### Semantic Search

Search conversations by meaning, not just keywords:

```python
from shared.utils.memory_integration import MemoryIntegration

memory = MemoryIntegration()

# Search for similar conversations
results = await memory.semantic_search_conversations(
    query="How do I optimize database queries?",
    user_id=user.id,
    limit=5
)

for result in results:
    print(f"Distance: {result['distance']:.3f}")
    print(f"Model: {result['model_used']}")
    print(f"Question: {result['messages'][0]['content']}")
    print(f"Answer: {result['response']}")
    print()
```

### Conversation Retrieval

Retrieve conversations with filters:

```python
# Get recent conversations for a user
conversations = await memory.get_user_conversations(
    user_id=user.id,
    limit=10,
    offset=0,
    start_date="2024-01-01",
    end_date="2024-12-31"
)

# Get conversations for a specific model
claude_convos = await memory.get_conversations_by_model(
    model="claude-3-opus",
    organization_id=org.id,
    limit=20
)

# Get conversations by routing decision
programming_convos = await memory.get_conversations_by_type(
    request_type="programming",
    user_id=user.id
)
```

## Management Portal Integration

### Viewing Conversations

Access the Memory Config page in WaddleAI Management Portal:

1. Navigate to http://localhost:8001/memory-config
2. Use the search interface to find conversations
3. Filter by user, organization, model, or date range
4. View full conversation details including routing decisions

### Conversation Search UI

The management portal provides a rich search interface:

```html
<!-- Semantic search -->
<input type="text" placeholder="Search by meaning..."
       id="semantic-search">
<button onclick="searchConversations()">Search</button>

<!-- Filters -->
<select id="model-filter">
  <option value="">All Models</option>
  <option value="gpt-4">GPT-4</option>
  <option value="claude-3-opus">Claude 3 Opus</option>
  <option value="llama3.2">Llama 3.2</option>
</select>

<!-- Results -->
<div id="results">
  <!-- Conversations with routing decisions and metadata -->
</div>
```

### Analytics

The Memory Config page shows:

- Total conversations stored
- Storage size in pgvector
- Most common request types
- Popular models by conversation count
- Average routing accuracy

## API Endpoints

### Search Conversations

```bash
curl http://localhost:8001/api/memory/conversations \
  -H "Authorization: Bearer <your-admin-token>" \
  -G \
  --data-urlencode "query=database optimization" \
  --data-urlencode "limit=5"
```

Response:

```json
{
  "results": [
    {
      "id": "conv_123",
      "user_id": 1,
      "model_used": "gpt-4",
      "routing_decision": "gpt-4",
      "routing_reasoning": "Complex technical question requires advanced reasoning",
      "request_type": "technical",
      "messages": [...],
      "response": "...",
      "waddleai_tokens": 150,
      "llm_tokens": {"prompt_tokens": 50, "completion_tokens": 100},
      "latency_ms": 1250,
      "timestamp": "2024-01-15T10:30:00Z",
      "similarity_score": 0.92
    }
  ],
  "total": 1,
  "limit": 5,
  "offset": 0
}
```

### Get Conversation Details

```bash
curl http://localhost:8001/api/memory/conversations/conv_123 \
  -H "Authorization: Bearer <your-admin-token>"
```

### Get Memory Statistics

```bash
curl http://localhost:8001/api/memory/stats \
  -H "Authorization: Bearer <your-admin-token>"
```

Response:

```json
{
  "total_conversations": 1543,
  "storage_size_mb": 234.5,
  "collections": [
    {
      "name": "waddleai_conversations",
      "count": 1543,
      "embedding_dimension": 1536
    }
  ],
  "request_types": {
    "programming": 523,
    "analysis": 412,
    "creative": 308,
    "chat": 200,
    "other": 100
  },
  "models_used": {
    "gpt-4": 456,
    "claude-3-opus": 389,
    "llama3.2": 298,
    "codellama": 245,
    "gpt-3.5-turbo": 155
  },
  "retention_days": 90,
  "oldest_conversation": "2024-10-01T00:00:00Z",
  "newest_conversation": "2025-01-15T15:30:00Z"
}
```

### Delete Conversations

```bash
# Delete specific conversation
curl -X DELETE http://localhost:8001/api/memory/conversations/conv_123 \
  -H "Authorization: Bearer <your-admin-token>"

# Delete old conversations (GDPR compliance)
curl -X DELETE http://localhost:8001/api/memory/conversations/cleanup \
  -H "Authorization: Bearer <your-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "older_than_days": 90,
    "user_id": 123
  }'
```

## Advanced Features

### Context Injection

Inject relevant past conversations into current requests:

```python
# Find similar past conversations
past_context = await memory.semantic_search_conversations(
    query=current_user_message,
    user_id=user.id,
    limit=3
)

# Add to system message
system_message = {
    "role": "system",
    "content": f"""You are a helpful assistant.

Relevant context from past conversations:
{format_context(past_context)}

Use this context to provide more informed responses."""
}

messages = [system_message] + user_messages
```

### Conversation Clustering

Group similar conversations:

```python
# Get conversation embeddings
embeddings = await memory.get_conversation_embeddings(
    user_id=user.id,
    limit=100
)

# Cluster using K-means or HDBSCAN
from sklearn.cluster import KMeans

clusters = KMeans(n_clusters=5).fit(embeddings)

# Identify conversation topics
for cluster_id in range(5):
    cluster_convos = [c for i, c in enumerate(conversations)
                      if clusters.labels_[i] == cluster_id]
    print(f"Cluster {cluster_id}: {len(cluster_convos)} conversations")
```

### Routing Optimization

Analyze routing decisions to improve accuracy:

```python
# Get routing accuracy
routing_stats = await memory.get_routing_statistics(
    organization_id=org.id,
    days=30
)

print(f"Total routed: {routing_stats['total']}")
print(f"Routing decisions:")
for model, count in routing_stats['model_distribution'].items():
    print(f"  {model}: {count} ({count/routing_stats['total']*100:.1f}%)")

# Identify misrouted requests
misrouted = await memory.find_misrouted_conversations(
    organization_id=org.id,
    threshold=0.7  # Confidence threshold
)
```

### Conversation Export

Export conversations for analysis or compliance:

```python
# Export to JSON
conversations = await memory.export_conversations(
    user_id=user.id,
    format="json",
    start_date="2024-01-01",
    end_date="2024-12-31"
)

with open("conversations_2024.json", "w") as f:
    json.dump(conversations, f, indent=2)

# Export to CSV
import csv

with open("conversations_2024.csv", "w") as f:
    writer = csv.DictWriter(f, fieldnames=[
        "timestamp", "model_used", "request_type",
        "waddleai_tokens", "latency_ms"
    ])
    writer.writeheader()
    writer.writerows(conversations)
```

## Privacy and Compliance

### Data Retention

Configure automatic cleanup of old conversations:

```python
# In .env.dev
CONVERSATION_RETENTION_DAYS=90

# Automatic cleanup runs daily
# Or manually trigger:
curl -X POST http://localhost:8001/api/memory/cleanup \
  -H "Authorization: Bearer <your-admin-token>"
```

### GDPR Right to Erasure

Delete all data for a specific user:

```bash
curl -X DELETE "http://localhost:8001/api/memory/user/123/all" \
  -H "Authorization: Bearer <your-admin-token>"
```

### Data Anonymization

Anonymize conversations while preserving analytics:

```python
anonymized = await memory.anonymize_conversations(
    user_id=user.id,
    keep_metadata=True  # Keep tokens, latency, routing info
)
# User messages/responses are removed, but usage stats remain
```

### Encryption at Rest

pgvector memory rows live in the same PostgreSQL database as the rest of
WaddleAI's data, so they inherit standard PostgreSQL at-rest encryption
(volume-level or TDE) -- no separate encrypted volume to provision, unlike
the former ChromaDB backend. See the platform's storage encryption baseline
for configuration details.

## Troubleshooting

### pgvector Connection Issues

```bash
# Check WaddleAI's database connection (pgvector shares the primary DB)
docker exec waddleai-proxy pg_isready -h postgres -p 5432

# Confirm the pgvector extension is installed
docker exec -it waddleai-postgres psql -U waddleai -c "\dx vector"

# View PostgreSQL logs
docker logs waddleai-postgres
```

### Slow Search Performance

1. **Check embedding model**: Verify `EMBEDDING_MODEL` is available
2. **Monitor PostgreSQL resources**: Large memory tables may need more RAM/IOPS
3. **Optimize query limits**: Use smaller `limit` values for faster results
4. **Add indexes**: Ensure an IVFFlat/HNSW index exists on the memory embedding column

### Storage Growth

Monitor and manage storage:

```bash
# Check memory table size
docker exec -it waddleai-postgres psql -U waddleai -c \
  "SELECT pg_size_pretty(pg_total_relation_size('memory_embeddings'));"

# Trigger cleanup
curl -X POST http://localhost:8001/api/memory/cleanup \
  -H "Authorization: Bearer <your-admin-token>"

# Reduce retention period
# In .env.dev: CONVERSATION_RETENTION_DAYS=30
```

## Best Practices

1. **Regular cleanup**: Schedule automatic cleanup based on retention policy
2. **Monitor storage**: Set alerts for storage growth
3. **Use semantic search**: More powerful than keyword search
4. **Export backups**: Regularly export conversations for disaster recovery
5. **Analyze routing**: Use conversation data to improve routing accuracy
6. **Respect privacy**: Configure appropriate retention periods for your use case

## See Also

- [pgvector Documentation](https://github.com/pgvector/pgvector)
- [mem0 Documentation](https://docs.mem0.ai)
- [Analytics Dashboard](../administration/monitoring.md)
- [Privacy Configuration](../administration/security-policies.md)
