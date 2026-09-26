# PenguinCode Knowledge Platform — Design Spec

- **Status:** Draft for review
- **Date:** 2026-09-25
- **Author:** Claude (with Justin Bowen)
- **Target branch:** `feature/penguincode-knowledge-platform` → `release/v0.2.X`
- **Supersedes:** the standalone "switch chromadb → pgvector + neo4j" ask (see Motivation)

## 1. Summary

Replace penguincode's chromadb-based vector store with **pgvector on the shared WaddleAI
Postgres**, and add a **graph layer with three logical graphs — code, knowledge, and
memory — on top of the RAG (vector) layer**, behind a single **swappable, permissively-licensed
`GraphStore` driver**. Everything is isolated by **tenant → org/team → user** at the query layer,
and every subsystem is behind a **PostHog feature flag** (default OFF, ship dark).

Primary driver: **remove the `chromadb` dependency** (no CVE-clean release exists; embedded-only
use today) while building toward the roadmap knowledge-graph platform.

## 2. Goals / Non-Goals

**Goals**
- Remove `chromadb` entirely (the CVE fix) — both consumers move to pgvector.
- Vector RAG (docs + memory) on the shared WaddleAI Postgres (pgvector already enabled there).
- Three graphs on top of RAG: **code graph**, **knowledge graph**, **memory graph**.
- One swappable `GraphStore` driver; default backend permissively licensed and colocated in Postgres.
- Hard isolation by **tenant**, **org/team (OU)**, and **user/visibility**, enforced at the store layer.
- Per-subsystem PostHog feature flags with graceful degradation.
- Full OTel (logs+metrics+traces) and real test coverage (there is ~none today).

**Non-Goals (this spec)**
- Cross-product graph platform sharing (other products consuming the same driver) — the driver is
  built to allow it, but only penguincode is wired now.
- Advanced graph algorithms (PageRank, community detection). The Postgres backend targets
  neighbor / k-hop / subgraph retrieval, which is what GraphRAG needs.
- mem0 Platform (managed SaaS) — rejected (external dependency / data egress).
- Migrating penguincode onto penguin-dal or the main `shared/vectorstore` package (keeps it standalone).

## 3. Motivation & Current State

**Why now:** the 2026-09-23 release audit flagged `chromadb` — every release `0.4.17`→`1.5.9`
carries ≥1 open CVE (incl. a critical pre-auth RCE). penguincode only uses the embedded in-process
client, so the CVEs aren't reachable today, but the dependency must go.

**Current footprint (verified 2026-09-25):**
- `docs_rag/indexer.py` talks to **chromadb directly** (`PersistentClient`, `add`/`query`/`get`/`delete`,
  `hnsw:space=cosine`) — no abstraction. Embeddings via Ollama `nomic-embed-text` (768-dim).
- `tools/memory.py` uses chromadb **via mem0**; `_get_vector_store_config` already implements a
  `pgvector` branch (`url`, `table_name`) — unused by default.
- **mem0 2.0.18 (penguincode's venv) has `vector_stores/pgvector.py` but NO graph subsystem** —
  no `graphs/` module, no `GraphStoreConfig`, no neo4j/memgraph drivers (verified by direct package
  inspection). So graph memory **cannot** ride on mem0; it is net-new, built by us.
- penguincode is a **standalone** service: own Helm chart/namespace, **no penguin-dal, no shared DB
  layer, no Postgres/Neo4j reachable, no Alembic, no JWT/tenant middleware, no PostHog client**.
- The **main WaddleAI chart already runs Postgres 17 with the `vector` extension enabled**.
- Embeddings are `nomic-embed-text` 768-dim cosine everywhere → no dimension mismatch.
- **Zero test coverage** on either store path today.

## 4. Licensing Decision (permissive only: BSD / MIT / Apache)

| Backend | License | Verdict |
|---|---|---|
| **Postgres graph module** (our tables in shared PG) | PostgreSQL (BSD/MIT-class) | **DEFAULT** — permissive, colocated, multi-replica-safe, per-tenant |
| Kùzu / LadybugDB | MIT | **Pluggable escape hatch** — embedded single-writer (multi-replica needs single replica / shared RW volume) |
| Apache AGE | Apache-2.0 | **Excluded** — prior bad experience |
| Neo4j Community | GPLv3 | Excluded — copyleft |
| Memgraph | BSL 1.1 | Excluded — forbids bundling/distribution to customers without OEM license |
| NebulaGraph | Apache-2.0 | Excluded — PRC-origin (vesoft), supply-chain rule |

Rationale: the only backend satisfying {permissive + multi-replica + bundleable + not-PRC + not-AGE}
is a Postgres-native edge/node model. The `GraphStore` driver keeps Kùzu/LadybugDB (MIT) available
if the code graph later outgrows recursive-CTE traversal.

## 5. Architecture

```
              ┌─────────────────────── penguincode ───────────────────────┐
  caller ──▶  │  JWT/tenant middleware (tenant, teams, sub, scope)          │
   (JWT)      │        │                                                     │
              │        ▼   scope-aware query builder (single chokepoint)     │
              │  ┌───────────────┐        ┌──────────────────────────────┐   │
              │  │  VectorStore   │        │  GraphStore driver (swappable)│   │
              │  │  (PgVector)    │        │   default: PostgresGraphStore │   │
              │  └──────┬────────┘        └───────┬──────────────────────┘   │
              │         │  vector hits            │  code / knowledge / memory │
              │         └──────────┬──────────────┘  (namespaced sub-graphs)   │
              │              GraphRAG retrieval (vector + graph expansion)      │
              └───────────────────────────┬────────────────────────────────┘
                                          ▼
                        shared WaddleAI Postgres (pgvector + graph tables)
                        schema: `penguincode`   (least-priv role)
```

- **VectorStore** — new in-repo interface + `PgVectorStore` impl (psycopg + pgvector). Consumed by
  the docs-RAG indexer and (via mem0's pgvector provider) by memory.
- **GraphStore** — new in-repo driver interface; default `PostgresGraphStore`. Three logical graphs
  share the backend via a `graph_kind` discriminator (`code` | `knowledge` | `memory`).
- **GraphRAG retrieval** — vector similarity → expand through the relevant graph(s) within scope →
  assemble augmented context.

## 6. Components

### 6.1 Vector / RAG (pgvector) — flag `penguincode.rag`
- `docs_rag/indexer.py`: replace direct chromadb with the `VectorStore` interface; `PgVectorStore`
  uses `penguincode.docs_vectors(id, embedding vector(768), document text, metadata jsonb, <scope cols>)`
  with a cosine index (HNSW preferred, ivfflat fallback). Query = `ORDER BY embedding <=> :q LIMIT :n`
  + scope + metadata filters. Relevance = `1 - distance` (unchanged).
- `tools/memory.py`: default `vector_store` → `pgvector`, `url` = shared-PG connection, table
  `penguincode.memory_vectors`. Scope columns injected via metadata + enforced on read.
- **Remove** `chromadb` from `pyproject.toml`, `ChromaStoreConfig`, config.yaml/entrypoint/helm chroma.
- **Flag off / PostHog unreachable:** vector retrieval disabled → no RAG augmentation (CLI still works);
  last-known cached value, never crash.

### 6.2 GraphStore driver
- Interface (minimum): `upsert_nodes`, `upsert_edges`, `neighbors(node, depth, kinds)`,
  `subgraph(seed_nodes, depth)`, `delete_by_scope`. Every method takes a `ScopeContext`.
- `PostgresGraphStore` (default): tables in §7; traversal via recursive CTEs with **scope filters
  re-applied at every hop**.
- Swappable via config/factory (`graph_backend: postgres | kuzu`), mirroring the vector-store factory.

### 6.3 The three graphs
| Graph | `graph_kind` | Source & builder | Flag |
|---|---|---|---|
| Code | `code` | tree-sitter parse of the ingested repo → files/symbols/functions/classes + imports/calls/defines/references. Built during code indexing (penguincode already uses tree-sitter). | `penguincode.code-graph` |
| Knowledge | `knowledge` | LLM entity/relation extraction over indexed docs/knowledge (GraphRAG-style). Built during docs indexing. | `penguincode.knowledge-graph` |
| Memory | `memory` | LLM entity/relation extraction over conversational memory (the mem0-graph capability we now own). Built on memory write. | `penguincode.memory-graph` |

Extraction uses the orchestration LLM (Ollama). Extractors are isolated units (one per kind) writing
through the same `GraphStore` under the caller's scope.

### 6.4 GraphRAG retrieval
Vector top-k (scoped) → seed nodes → `subgraph`/`neighbors` expansion (scoped, depth-bounded) →
merge + rank → augmented context. Which graphs participate depends on which flags are on.

## 7. Data Model (schema `penguincode` in the shared Postgres)

All tables carry the scope columns: `tenant_id uuid NOT NULL`, `org_id uuid`, `team_id uuid`,
`owner_user_id uuid`, `visibility text CHECK (visibility IN ('user','team','tenant')) NOT NULL`.

- `docs_vectors(id, embedding vector(768), document, metadata jsonb, <scope>, created_at)`
- `memory_vectors(...)` — or mem0-managed table with scope in metadata + a scope-enforcing view.
- `graph_nodes(id, graph_kind, node_type, key, props jsonb, <scope>, created_at)` — unique
  `(tenant_id, graph_kind, node_type, key)`.
- `graph_edges(id, graph_kind, src_id, dst_id, rel_type, props jsonb, <scope>, created_at)`.

**Indexes:** vector cosine index per vector table; composite indexes **leading with scope columns**
(`tenant_id`, then `team_id`/`graph_kind`) so filtered vector and graph queries stay fast.

## 8. Multi-Tenancy & Access Control

Standard hierarchy: **Global → Tenant (hard boundary) → Team/OU → User/Resource**.
- **Writes** stamp scope from the validated JWT (`tenant`, `teams`, `sub`) — never client-supplied.
- **Reads** filtered at the **store/driver layer** (single chokepoint, cannot be bypassed per callsite):
  tenant match first (mismatch → 403) → team allowlist (`teams` ∩ row.team) → visibility.
  Applied to vector queries **and every graph-traversal hop** (no cross-scope leakage through edges).
- **Auth wiring:** add WaddleAI **JWT validation + tenant/team middleware** to penguincode's gRPC/API
  surface; make it **SPIFFE-ready**. A `ScopeContext` is derived once per request and threaded to the
  stores.

## 9. Cross-Namespace Postgres Wiring
- **CiliumNetworkPolicy:** allow penguincode namespace → waddleai Postgres `:5432`; default-deny else.
- **Least-priv DB role** for penguincode owning the `penguincode` schema only (not the `waddleai`
  app role). Connection string from a K8s **Secret/env** (never CLI; token/secret hygiene).
- **Migrations:** penguincode-owned **idempotent SQL** run by an init job (it has no Alembic; do not
  couple to management's Alembic). Creates schema, `vector` extension guard, tables, indexes.

## 10. Feature Flags
- `penguincode.rag`, `penguincode.code-graph`, `penguincode.knowledge-graph`, `penguincode.memory-graph`.
- PostHog (self-hosted CE); add a flag client to penguincode (none today). Graph flags depend on
  `penguincode.rag` (graphs augment RAG).
- **Default OFF, ship dark**; flip on per environment after validation. Graceful degradation: flag
  server unreachable → last-known cached value (never-seen → OFF), never crash.

## 11. Config & Deployment
- `config/settings.py`: default `vector_store` → `pgvector`; add `graph_backend` + `PostgresGraphStoreConfig`;
  remove `ChromaStoreConfig`. New env: `PGVECTOR_URL` (shared PG), graph + flag config.
- `docker-entrypoint.sh`: generate pgvector+graph config; drop chroma path.
- `k8s/helm/penguincode`: remove chroma PVC; add PG connection Secret/env, the CiliumNetworkPolicy,
  the migration/init job, and flag-client config.

## 12. Observability (mandatory)
OTel logs + metrics + traces on every vector query, graph query/traversal, and extraction call
(histograms for query/extraction latency; spans across DB calls; penguin logging). Telemetry
validated in smoke tests (≥1 log, ≥1 metric, ≥1 span). No PII/secrets in spans/labels.

## 13. Testing (zero today → must add)
- **Unit:** `PgVectorStore` round-trip; `PostgresGraphStore` node/edge/traversal; each extractor
  (mocked LLM); scope enforcement (cross-tenant/cross-team denied; visibility honored); flag on/off +
  outage degradation. Against an **ephemeral CI Postgres** (service container / testcontainers).
- **Integration:** index→retrieve (pgvector); write→extract→hybrid GraphRAG retrieve; scope isolation
  end-to-end.
- **Regression:** chromadb absent; `# regression:` markers on the CVE removal and scope-leak tests.
- **Telemetry validation** as §12.

## 14. Rollout
- Land flag-gated (all OFF) so it ships dark. Unit/integration run against ephemeral Postgres in CI.
- Full cluster validation deferred while **beta/dal2 is offline**; flip flags on per environment after
  validation there.

## 15. Implementation Phases (dependency order; one effort)
1. **RAG/pgvector foundation** — VectorStore interface + PgVectorStore; docs-RAG + mem0 → pgvector;
   remove chromadb; `penguincode` schema + migrations; cross-namespace PG wiring; `penguincode.rag` flag.
2. **Auth/scope foundation** — JWT/tenant middleware, `ScopeContext`, scope columns + scope-aware
   query builder wired into VectorStore.
3. **GraphStore driver + PostgresGraphStore** — interface, tables, scoped traversal, factory.
4. **Three graphs** — code (tree-sitter), knowledge (LLM), memory (LLM) extractors + their flags.
5. **GraphRAG retrieval** — hybrid vector+graph, scope-filtered.
6. **Flags, OTel, tests, deploy** — threaded through each phase; final green gate.

## 16. Risks & Open Questions
- **Postgres graph vs a real graph engine:** recursive CTEs cover GraphRAG retrieval; heavy graph
  algorithms are out of scope. Kùzu/LadybugDB (MIT) is the pluggable escape hatch if needed.
- **penguincode auth is net-new** — it has no JWT/tenant middleware today; this is a real addition,
  not a config change. How penguincode obtains the caller JWT (gRPC metadata? API header?) to be
  nailed in the plan.
- **mem0 scope enforcement** — mem0 manages its own memory table; scope is injected via metadata and
  enforced by a scoping wrapper/view. Confirm mem0 2.0.18 lets us filter reads by metadata reliably;
  if not, wrap memory writes/reads so scope is enforced in our layer, not mem0's.
- **Migrations without Alembic** — idempotent SQL at init is simplest; revisit if penguincode later
  needs versioned migrations.
- **Beta offline** — no live cluster validation until dal2 returns; mitigated by ephemeral-Postgres
  CI + dark flags.

## 17. Decisions Locked (unless changed in review)
- Backend default: **Postgres graph module** (permissive); Kùzu/LadybugDB MIT pluggable.
- Graph scope this build: **all three** (code, knowledge, memory) on top of RAG.
- Isolation: **tenant + org/team + user/visibility**, enforced at the store layer.
- Flags: four, **default OFF**, flip on post-validation.
- penguincode owns a **`penguincode` schema + idempotent SQL migrations** in the shared PG.
