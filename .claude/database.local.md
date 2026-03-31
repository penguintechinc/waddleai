# Database (WaddleAI-Specific Addendum)

## PostgreSQL-Only Exception

WaddleAI uses **PostgreSQL with pgvector exclusively** — no SQLite, MySQL, or MariaDB support.

**This overrides the multi-DB support requirement in `.claude/rules/database.md` for this project.**

### Rationale

pgvector (the memory and RAG embedding store) requires PostgreSQL. Maintaining multi-DB support
alongside vector embeddings would require separate code paths and separate vector stores for every
database type, defeating the purpose of consolidation and adding complexity without benefit.

### What This Means

- Remove all `DB_TYPE` branching in this project — always PostgreSQL
- No SQLite fallback (not even for tests — use pytest fixtures with mocked DAL)
- Docker Compose always uses `pgvector/pgvector:pg16` image (not plain `postgres:16-bookworm`)
- `DATABASE_URL` is always a `postgresql://` URI
- `.env.example` shows only PostgreSQL connection strings

### Embedding Backends (Separate from DB)

The vector embedding backend (what generates the float vectors stored in pgvector) is configurable
per-organization via `EmbeddingSettings` in the database:

| Backend | Default model | When to use |
|---------|--------------|-------------|
| `ollama` | `nomic-embed-text` | Local deployment, no API key needed (default) |
| `openai` | `text-embedding-3-small` | Higher quality, requires `OPENAI_API_KEY` |
| `anthropic` | `claude-haiku-4-5-20251001` | When only Anthropic credentials are available |

The PostgreSQL-only rule does NOT apply to embedding backends — those are separate services.
