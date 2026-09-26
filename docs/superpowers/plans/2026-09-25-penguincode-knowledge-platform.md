# PenguinCode Knowledge Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is executed by a PenguinTech specialist agent (default `penguin-python-dev`) that applies TDD (write failing test → run → implement → run → commit) within the task; this plan fixes the **file ownership, interfaces, and acceptance criteria** so parallel tasks don't drift.

**Goal:** Remove chromadb; move penguincode RAG to pgvector on the shared WaddleAI Postgres; add code/knowledge/memory graphs on top of RAG behind one swappable permissively-licensed GraphStore driver; isolate everything by tenant + org/team + user; gate every subsystem behind PostHog flags.

**Architecture:** A scope-aware `VectorStore` (PgVector) and a swappable `GraphStore` driver (default `PostgresGraphStore`, all in the shared Postgres `penguincode` schema) sit behind a single per-request `ScopeContext`. Three extractors populate three logical graphs (`graph_kind` = code|knowledge|memory); GraphRAG retrieval = scoped vector hits + scoped graph expansion. Standalone penguincode gains JWT/tenant middleware, a PostHog flag client, and idempotent SQL migrations.

**Tech Stack:** Python 3.13, psycopg + pgvector, tree-sitter (present), Ollama `nomic-embed-text` (768-dim), mem0ai 2.0.18 (vector only), PostHog CE, OpenTelemetry + penguin logging.

**Spec:** `docs/superpowers/specs/2026-09-25-penguincode-knowledge-platform-design.md` (read it alongside this plan).

## Global Constraints

- Python 3.13; ruff clean; `scripts/mypy-gate.sh` 0 new; bandit clean; ≥90% coverage on new code.
- Dependencies exact-pinned + hash-locked (`uv pip compile --generate-hashes`); **chromadb must be fully removed**.
- Embeddings: Ollama `nomic-embed-text`, **768-dim, cosine** everywhere — do not change.
- No PRC-origin deps. Graph/vector backends: **BSD/MIT/Apache/PostgreSQL license only** (no Neo4j GPL, no Memgraph BSL, no AGE, no NebulaGraph).
- Every feature behind a PostHog flag, **default OFF**, graceful degradation (server unreachable → last-known cached → OFF; never crash).
- Tenant is a **hard boundary**; every store read/write is scope-filtered at the store layer — never a raw unscoped query.
- Secrets/DB creds from env/Secret only, never CLI args; no PII/secrets in logs, spans, or metric labels.
- OTel logs+metrics+traces on every vector/graph/extraction op; telemetry validated in smoke tests (≥1 log, ≥1 metric, ≥1 span).
- Commit trailer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` / `Claude-Session: https://claude.ai/code/session_01P1z5JFY9nGxYYzzhQSYc4b`
- Tests marked `# regression: penguincode-knowledge-platform` where they guard the CVE removal or scope isolation.

---

## File Structure (ownership map)

| Path | Responsibility | Owning task |
|---|---|---|
| `services/penguincode/db/migrations/*.sql` + `db/migrate.py` | idempotent schema/extension/tables/indexes + runner | T1 |
| `services/penguincode/auth/scope.py` | `ScopeContext` + JWT/tenant extraction | T2 |
| `services/penguincode/auth/middleware.py` | gRPC/API JWT+tenant middleware | T2 |
| `services/penguincode/config/settings.py` | pgvector default, `graph_backend`, remove `ChromaStoreConfig` | T3 |
| `services/penguincode/flags/client.py` | PostHog flag client + graceful degradation | T4 |
| `services/penguincode/observability/otel.py` | OTel init + span/metric helpers | T5 |
| `services/penguincode/stores/vector.py` | `VectorStore` interface + `PgVectorStore` | T6 |
| `services/penguincode/docs_rag/indexer.py` | route to `VectorStore` (remove chromadb) | T7 |
| `services/penguincode/tools/memory.py` | mem0 pgvector default + scope wrapper | T8 |
| `services/penguincode/pyproject.toml`, `docker-entrypoint.sh`, `config.yaml` | remove chromadb + chroma config | T9 |
| `services/penguincode/stores/graph.py` | `GraphStore` interface + `PostgresGraphStore` | T10 |
| `services/penguincode/graphs/code.py` | tree-sitter code-graph extractor | T11 |
| `services/penguincode/graphs/knowledge.py` | LLM knowledge-graph extractor | T12 |
| `services/penguincode/graphs/memory.py` | LLM memory-graph extractor | T13 |
| `services/penguincode/retrieval/graphrag.py` | hybrid scoped retrieval | T14 |
| `k8s/helm/penguincode/**` | NetworkPolicy, PG Secret/env, migration job, flag config, drop chroma PVC | T15 |
| `services/penguincode/tests/integration/**` | ephemeral-Postgres integration + telemetry validation | T16 |

---

## Shared Contracts (all tasks MUST use these exact names/signatures)

**ScopeContext** (`auth/scope.py`) — derived once per request from the validated JWT:
```python
@dataclass(slots=True, frozen=True)
class ScopeContext:
    tenant_id: str            # required; hard boundary
    org_id: str | None
    team_ids: tuple[str, ...] # caller's teams (JWT `teams`)
    user_id: str              # JWT `sub`
    scopes: tuple[str, ...]   # JWT `scope`
```

**Scope columns** (every vector + graph table): `tenant_id uuid NOT NULL`, `org_id uuid`, `team_id uuid`, `owner_user_id uuid`, `visibility text NOT NULL CHECK (visibility IN ('user','team','tenant'))`.

**Read filter** (applied by the store layer, never by callers): row visible iff
`row.tenant_id == ctx.tenant_id` AND (`row.visibility=='tenant'` OR (`'team'` AND `row.team_id IN ctx.team_ids`) OR (`'user'` AND `row.owner_user_id==ctx.user_id`)).

**VectorStore** (`stores/vector.py`):
```python
class VectorStore(Protocol):
    def upsert(self, ctx: ScopeContext, items: list[VectorItem], *, visibility: str, team_id: str | None) -> None: ...
    def query(self, ctx: ScopeContext, embedding: list[float], *, n: int, where: dict | None = None) -> list[VectorHit]: ...
    def delete(self, ctx: ScopeContext, ids: list[str]) -> None: ...
# VectorItem(id:str, embedding:list[float], document:str, metadata:dict)
# VectorHit(id:str, document:str, metadata:dict, score:float)  # score = 1 - cosine_distance
```

**GraphStore** (`stores/graph.py`):
```python
class GraphStore(Protocol):
    def upsert_nodes(self, ctx: ScopeContext, kind: str, nodes: list[GraphNode], *, visibility: str, team_id: str | None) -> None: ...
    def upsert_edges(self, ctx: ScopeContext, kind: str, edges: list[GraphEdge], *, visibility: str, team_id: str | None) -> None: ...
    def neighbors(self, ctx: ScopeContext, kind: str, node_key: str, *, depth: int, rel_types: list[str] | None = None) -> Subgraph: ...
    def subgraph(self, ctx: ScopeContext, kind: str, seed_keys: list[str], *, depth: int) -> Subgraph: ...
    def delete_by_scope(self, ctx: ScopeContext, kind: str, *, node_keys: list[str] | None = None) -> None: ...
# GraphNode(node_type:str, key:str, props:dict)
# GraphEdge(src_type:str, src_key:str, dst_type:str, dst_key:str, rel_type:str, props:dict)
#   endpoints resolve to graph_nodes.id via (tenant_id, graph_kind, node_type, key); edges MUST carry
#   src_type/dst_type because (node_type, key) — not key alone — is unique per (tenant, graph_kind)
#   (T1 schema: UNIQUE(tenant_id, graph_kind, node_type, key)). Upserting an edge auto-creates missing
#   endpoint nodes (type+key) if absent, so producers may emit edges without a prior node upsert.
# kind in {"code","knowledge","memory"}; Subgraph(nodes:list[GraphNode], edges:list[GraphEdge])
```

**Flag keys:** `penguincode.rag`, `penguincode.code-graph`, `penguincode.knowledge-graph`, `penguincode.memory-graph`. Client: `flags.client.is_enabled(key, ctx) -> bool`.

**Config additions** (`config/settings.py`): `vector_store` default `"pgvector"`; `graph_backend` default `"postgres"` (also `"kuzu"`); `PGVECTOR_URL` env → shared PG; remove `ChromaStoreConfig`.

---

## Waves & Tasks

Dispatch waves in order; parallelize tasks **within** a wave (disjoint files). ≤10 concurrent agents.

### Wave 1 — Foundation (parallel: T1–T5)
- **T1 — Schema & migrations.** Create `db/migrations` (idempotent SQL: `CREATE SCHEMA penguincode`, `CREATE EXTENSION IF NOT EXISTS vector`, tables `docs_vectors`, `memory_vectors`, `graph_nodes`, `graph_edges` with scope columns from Shared Contracts, cosine vector index, composite scope-leading indexes, uniqueness `(tenant_id,graph_kind,node_type,key)`), plus `db/migrate.py` runner. **Produces:** table DDL other tasks read. **Tests:** migrate against ephemeral Postgres is idempotent (run twice), tables+indexes exist. No app code depends on this at import (runner invoked by init).
- **T2 — ScopeContext + auth middleware.** `auth/scope.py` (`ScopeContext` exactly as Shared Contracts) + `auth/middleware.py` (validate WaddleAI JWT from gRPC metadata / API header, reject missing `tenant`, build `ScopeContext`; SPIFFE-ready hook). **Produces:** `ScopeContext`. **Tests:** valid JWT → ctx; missing tenant → 401/reject; teams/scope parsed.
- **T3 — Config.** `config/settings.py`: default `vector_store="pgvector"`, add `graph_backend`, `PGVectorStoreConfig`/`PostgresGraphStoreConfig`, `PGVECTOR_URL`; **remove `ChromaStoreConfig`** and the `chroma` branch. **Tests:** settings parse pgvector+graph defaults from env; chroma config gone.
- **T4 — Flag client.** `flags/client.py`: PostHog CE client, `is_enabled(key, ctx)`, last-known cache, unreachable→cached→OFF, never raise. **Tests:** on→True, off→False, outage→last-known then OFF; never throws.
- **T5 — OTel.** `observability/otel.py`: init from OTLP env (no hardcoded endpoint), span + histogram helpers for store/extraction ops, penguin logging. **Tests:** against in-memory OTLP exporter — ≥1 span + ≥1 metric emitted; no-op without endpoint.

### Wave 2 — Vector layer (parallel: T6, then T7/T8/T9; T7–T9 consume T6)
- **T6 — VectorStore + PgVectorStore.** `stores/vector.py` implementing the Shared-Contracts interface; scope stamped on write, read filter applied in SQL (`WHERE tenant_id=... AND (visibility...)`), cosine `ORDER BY embedding <=> :q`. **Consumes:** T1 tables, T2 `ScopeContext`, T3 config. **Tests:** upsert+query round-trip; cross-tenant/cross-team denied; visibility honored; score=1-distance.
- **T7 — docs-RAG indexer → VectorStore.** Rewrite `docs_rag/indexer.py` `_get_collection`/`add`/`query`/`get`/`delete` to use `VectorStore` (remove chromadb import). Preserve embedding path + metadata filters. **Consumes:** T6. **Tests:** index→retrieve round-trip via pgvector; metadata filter; scope isolation. (docs-RAG chroma path is untested today — this adds the first real coverage.)
- **T8 — mem0 memory → pgvector + scope.** `tools/memory.py`: default vector_store pgvector; wrap mem0 add/search so scope is enforced in our layer (mem0 metadata filter is not trusted — see spec §16). **Consumes:** T2, T3, T6-patterns. **Tests:** memory write/read scoped; cross-tenant memory not returned; disabled-manager path intact.
- **T9 — Remove chromadb.** `pyproject.toml` (drop `chromadb`, re-lock hashes), `docker-entrypoint.sh` + `config.yaml` (drop chroma env/path). **Tests/verify:** `grep -ri chromadb services/penguincode` = 0 (outside changelog); penguincode suite installs+collects without chromadb. `# regression: penguincode-knowledge-platform`.

### Wave 3 — Graph driver (T10)
- **T10 — GraphStore + PostgresGraphStore.** `stores/graph.py` implementing the interface; recursive-CTE traversal with **scope filter re-applied at every hop**; `graph_kind` discriminator; factory (`postgres`|`kuzu`, kuzu stub raising NotImplemented for now). **Consumes:** T1, T2. **Tests:** upsert nodes/edges; neighbors/subgraph at depth; traversal never crosses tenant/team; delete_by_scope.

### Wave 4 — Three graphs (parallel: T11, T12, T13)
- **T11 — Code graph.** `graphs/code.py`: tree-sitter parse of ingested repo → nodes(file/symbol/function/class)+edges(imports/calls/defines/references), written via `GraphStore` kind=`code` under scope; hooked into code indexing; flag `penguincode.code-graph`. **Consumes:** T10. **Tests:** parse a fixture repo → expected nodes/edges; flag-off → no writes.
- **T12 — Knowledge graph.** `graphs/knowledge.py`: LLM (Ollama orchestration model) entity/relation extraction over indexed docs → kind=`knowledge`; flag `penguincode.knowledge-graph`. **Consumes:** T10. **Tests:** mocked-LLM extraction → nodes/edges; flag-off → skip; no PII in logs.
- **T13 — Memory graph.** `graphs/memory.py`: LLM extraction over memory writes → kind=`memory`; flag `penguincode.memory-graph`; wired to T8's memory write path. **Consumes:** T8, T10. **Tests:** mocked-LLM triples upserted scoped; flag-off → vector-only.

### Wave 5 — Retrieval, deploy, integration (parallel: T14, T15; then T16)
- **T14 — GraphRAG retrieval.** `retrieval/graphrag.py`: scoped vector top-k → seed nodes → scoped `subgraph` expansion (only for graphs whose flag is on) → merged, ranked, augmented context. **Consumes:** T6, T10. **Tests:** hybrid retrieve returns vector + graph-expanded context within scope; each graph flag independently gates its contribution.
- **T15 — Deploy.** `k8s/helm/penguincode`: remove chroma PVC; add PG connection Secret/env; **CiliumNetworkPolicy** penguincode→waddleai Postgres:5432; migration/init Job (runs T1); flag-client config. (Dispatch to `k8s-manifest-builder`.) **Verify:** `helm template` + kubeconform clean; probes/securityContext intact.
- **T16 — Integration + telemetry.** `tests/integration`: end-to-end index→retrieve (pgvector), memory→extract→hybrid retrieve, scope isolation e2e, against an ephemeral CI Postgres (service container). Telemetry validation (≥1 log/metric/span). Wire `test-penguincode` CI to stand up Postgres. **Consumes:** all. **Tests:** the above pass in CI.

---

## Self-Review

**Spec coverage:** §5 arch → T2/T6/T10/T14; §6.1 RAG → T6/T7/T8/T9; §6.2 driver → T10; §6.3 three graphs → T11/T12/T13; §6.4 GraphRAG → T14; §7 schema → T1; §8 scope → T2 + enforced in T6/T10; §9 cross-namespace → T15; §10 flags → T4 + used in T7/T11/T12/T13/T14; §11 config/deploy → T3/T9/T15; §12 OTel → T5 + T16; §13 testing → every task + T16; §14 rollout → flags default OFF (T4). No gaps.

**Placeholder scan:** none (kuzu backend is an explicit NotImplemented stub, by design).

**Type consistency:** `ScopeContext`, `VectorStore`, `GraphStore`, `VectorItem/VectorHit`, `GraphNode/GraphEdge/Subgraph`, flag keys, config names are defined once in Shared Contracts and referenced by tasks verbatim.

## Execution

Subagent-driven, dependency-ordered waves, ≤10 concurrent specialist agents, each owning disjoint files per the ownership map; review between waves; per-task branches under this feature branch or a shared feature branch with disjoint files (decided at dispatch).
