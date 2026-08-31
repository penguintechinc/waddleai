# Shared Graph Platform — Neo4j Backing CodeRAG, LightRAG, and mem0 Graph-Memory

**Date:** 2026-08-31
**Branch:** release/v0.3.X
**Author:** Justin Bowen
**Status:** Draft — for review

**Decisions (2026-08-31, owner):** shared datastore = **Neo4j**, explicitly not Apache AGE (flagged immature) and not a Postgres-relational graph (superseded by this decision); pgvector stays embedding-only on `code_chunks`. Tenancy = **one Neo4j instance per org + property-scoping within the instance**. Neo4j Community suffices (single-DB, no Enterprise licence). **Neo4j is an interim backend** — owner intent: "we'll eventually want to move off neo4j to something more license friendly (or our own) but this works for now," so the access layer must be vendor-abstracted from day one (§3).

This is a **greenfield** design: `grep -rniE "neo4j|lightrag|knowledge.?graph|graph.?store|graphdb"` across `*.py/*.md/*.yaml/*.yml/*.toml` repo-wide returns **zero hits**.

---

## 1. Problem, and why a shared graph

WaddleAI has three consumers that each want a property graph, and none of them has one today:

- **CodeRAG structural graph** — `shared/knowledge/code_chunker.py`'s `chunk_code()` (L291) and `_walk_definitions()` (L130) already classify class/method/function/module definitions via tree-sitter, but produce flat `CodeChunkDraft`s for `code_chunks` (pgvector) only — **no CALLS/EXTENDS/edge extraction exists**. Scoping today is `shared/knowledge/scoping.py`'s `ScopeKey`/`is_visible`/`filter_visible` (L64, 135, 171) — a **post-fetch filter**, not a query-layer scope; the code's own comment (L155-159) calls branch isolation "a security property, not just a filter convenience." The graph tenant-guard in §3 exists specifically so the graph doesn't repeat this weakness in Cypher.
- **LightRAG** (`HKUDS/LightRAG`) — net new, entity/relationship extraction into a knowledge graph with dual-level retrieval.
- **mem0 graph-memory** — net new. WaddleAI's `Mem0MemoryStore` (`shared/utils/memory_integration.py:117`) already uses the **hosted** `mem0.MemoryClient` (L22), not self-hosted `mem0.Memory`. More importantly: **mem0's self-hosted OSS SDK has removed native `graph_store` support entirely** — confirmed against current docs.mem0.ai: *"Graph memory is removed from the open-source SDK... it is a Mem0 Platform feature,"* listing "All external graph store drivers (Neo4j, Memgraph, Kuzu, Apache AGE, Neptune)" as deleted (~4000 lines). There is no config flag to flip; §4c designs around this.

Each consumer standing up its own graph store triples the operational surface (K8s lifecycle, tenant isolation, backup, licence review) for the same primitive: a property graph, scoped per org, queried by traversal. One shared datastore amortizes all four.

**Why Neo4j over AGE/relational:** owner rejects AGE on maturity grounds (settled, not re-litigated here). A Postgres-relational graph (adjacency tables + recursive CTEs) is what pgvector/Postgres already does for embeddings, but multi-hop CALLS chains, cycle detection (Tarjan's SCC), and CK-metric traversal are native to a property-graph engine and get combinatorially awkward in recursive CTEs at this depth. Neo4j has the widest adoption, an Apache-2.0 Python driver (confirmed: `neo4j/neo4j-python-driver` ships `LICENSE.APACHE2.txt`), an official Helm chart (Artifact Hub `neo4j`, defaults to Community), and both LightRAG (`Neo4JStorage`) and historically mem0 support it natively — maximizing consumer fit even though mem0's own integration has since been removed (§4c).

---

## 2. Per-tenant provisioning and lifecycle

**Model ("2 for free"):** hard isolation via a separate Neo4j **instance per org** (physical — cross-org leakage impossible without a network path existing at all), *plus* property-scoping (`repo_id`/`branch_ref`) enforced within that org's own instance by the driver layer (§3) — two independent layers, not either/or.

**Why per-instance, not per-database:** Neo4j Community Edition supports exactly one active user database per DBMS process — multi-database and Fabric are Enterprise-only (confirmed). "One org, one database" therefore has to mean "one org, one Neo4j server process." This keeps the design on Community and off the proprietary Enterprise licence entirely.

**Deployment shape — StatefulSet per tenant:** this chart has no StatefulSet today (postgres/valkey/ollama are Deployments, `k8s/helm/waddleai/templates/*-deployment.yaml`); Neo4j is the first workload needing a stable network identity plus an exclusive PVC per instance — what a StatefulSet is for. Recommended path: the management service (already drives K8s-facing reconciliation via `CiliumPolicyReconciler`, `services/management/app/services/cilium_policy.py`, invoked from `create_organization()`, `organizations.py:117-152`) renders and applies a per-org manifest set (StatefulSet + headless Service + PVC + Secret + CiliumNetworkPolicy) directly via the K8s API — not a full custom operator (too heavy for an Enterprise-only, few-large-tenants feature) and not a per-tenant Helm sub-release (adds state-management this repo doesn't otherwise carry). Open question (§9) — riskiest new infra pattern here.

**Provisioning trigger:** `create_organization()` today fire-and-forgets `CiliumPolicyReconciler` (L16-30, `asyncio.create_task(asyncio.to_thread(...))`, never raises). Neo4j pod bring-up (image pull, PVC bind, ~10-30s startup) is too slow for that pattern. Add a `graph_instances` table (`org_id`, `status` ∈ `pending|provisioning|ready|failed|deprovisioning|deprovisioned`, `bolt_url`, `created_at`) plus a durable provisioning job; the org→instance resolver (§3) treats anything but `ready` as feature-unavailable (clean 503), never a hang.

**Rootless:** `runAsNonRoot: true`, `runAsUser/Group: 7474` (Neo4j's own default UID, confirmed), `fsGroup: 7474`, `allowPrivilegeEscalation: false`, `capabilities: drop: [ALL]` — matches `critical-rules.md` Rootless Containers. Caveat found in research: Neo4j's entrypoint writes to `/data /logs /tmp /plugins /conf` at startup, so `readOnlyRootFilesystem: true` needs those mounted writable (PVC for `/data`; `emptyDir` for `/logs /tmp /conf`); bake APOC into the image at build time rather than downloading plugins at runtime (dependency-pinning rule).

**Resources:** starting default per tenant — `requests: 500m/1Gi`, `limits: 1/2Gi` (heap/pagecache tuned via `NEO4J_server_memory_*`). Scale honesty: N tenants = N pods, so this is realistically **Enterprise-tier only** — which already matches `critical-rules.md`'s tier table (Enterprise = 200+ employees or 10+ years, i.e. few, larger tenants). At 20 Enterprise orgs: ~20-40Gi memory, one dedicated node pool, fine. At hundreds of small orgs it does not work — see §9 fallback.

**PVC / at-rest encryption:** CSI-driver-default encryption (platform-managed keys, `security.md` baseline) — same posture as the chart's other PVCs.

**CiliumNetworkPolicy:** default-deny + explicit allow: only `management` (schema/admin bolt access) and pods carrying the calling org's identity reach that org's `neo4j-{org_id}` Service on 7687 — no cross-org pod can reach another org's Service at all. Model on `k8s/helm/waddleai/templates/cilium-network-policy.yaml`; generate a per-tenant policy alongside the StatefulSet. This is the network-layer half of tenant isolation; §3/§8 cover the driver-layer half.

**Backup:** Community Edition has **no online backup** (`neo4j-admin database backup` is Enterprise-only, confirmed) — only offline dump, which requires stopping the instance. Since graph content here is largely re-derivable from source of truth (git repos for coderag, chat history for mem0-graph), treat the graph as a **rebuildable index**, not a primary store: periodic full reindex is the DR strategy (consistent with coderag's existing content-hash incremental design), supplemented by nightly CSI volume-snapshot for faster restore than a full rebuild. Accepted trade-off, not a silently dropped requirement — confirm in §9.

**Deletion:** `delete_organization()` today is **soft-only** (`organizations.py:212-238` — `enabled=False`, blocks if `user_count>0`, no hard-delete path exists). Match that: on soft-delete, immediately revoke the CiliumNetworkPolicy allow (data inert/unreachable) but don't destroy the StatefulSet/PVC yet; actual teardown rides either the 30-day no-contact staleness pattern already used for node/seat licence counters (`critical-rules.md`) or a future hard-delete/purge action, whichever lands first — this repo has no hard-delete path today, so full teardown automation is new work (§7).

---

## 3. Shared graph-access layer (`shared/graph/`)

New package, sibling to `shared/knowledge/`, `shared/mcp/`, `shared/utils/`. Protocol-based, consistent with existing house style (`CodeSearchBackend` Protocol, `code_search.py:48`; `KnowledgeService`/`MemoryService` Protocols, `shared/mcp/tools.py:98-141`).

```python
class GraphStore(Protocol):
    """Vendor-neutral property-graph interface. Consumers depend on this
    Protocol only -- never a Cypher string or neo4j-driver type -- so
    swapping the backing store is a driver implementation, not a rewrite."""
    async def upsert_node(self, tenant: TenantScope, label: str, key: str, properties: dict) -> None: ...
    async def upsert_edge(self, tenant: TenantScope, edge_type: str, src_key: str, dst_key: str, properties: dict) -> None: ...
    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]: ...
    async def traverse(self, tenant: TenantScope, start_key: str, edge_types: list[str], max_depth: int, direction: Literal["out", "in", "both"]) -> list[GraphPath]: ...
    async def delete_scope(self, tenant: TenantScope, repo_id: str, branch_ref: str | None = None) -> int: ...
    async def close(self) -> None: ...

@dataclass(slots=True)
class TenantScope:
    org_id: str          # resolves org -> physical instance; never client-supplied
    repo_id: str
    branch_ref: str | None = None
    scope_type: str | None = None   # reuses scoping.py's ScopeKey vocabulary
    scope_ref: str | None = None
```

**Tenant guard mechanics:** `TenantGraphClient` resolves `org_id` (read from the validated JWT `tenant` claim, never request body/params, per `security.md`) to a physical instance connection via the `graph_instances` table, at the start of every call — no method accepts a pre-resolved connection from the caller. Every query is built by an internal `GraphQuery` AST (node/edge pattern + predicates), never a raw Cypher string from a consumer; the query-builder **always** injects `repo_id`/`branch_ref` into the compiled predicate — this is the "no un-scoped query can be issued" rule, applied to Cypher instead of a post-fetch filter.

| Layer | Enforced by |
|---|---|
| Org isolation | Separate physical Neo4j instance (§2) + CiliumNetworkPolicy |
| Auth to that instance | Per-org credentials, resolved server-side from the JWT `tenant` claim |
| Repo/branch scoping in-instance | `TenantGraphClient`'s query-builder — mandatory predicate, driver-layer, not post-fetch |

### Backend abstraction and exit strategy (Neo4j is interim)

Owner intent: eventually move off Neo4j to something more licence-friendly or in-house. `Neo4jGraphStore` (`shared/graph/drivers/neo4j_driver.py`) is the **only** place the `neo4j` driver import or a compiled Cypher string exists. Portability risks to watch, called out honestly rather than assumed away:

- **APOC / Neo4j Graph Data Science** procedures are Neo4j-only (GDS is Enterprise-licensed anyway, unavailable here) — avoid. CK metrics and Tarjan's SCC (§4a) are implemented as WaddleAI's own Python traversal over `traverse()` results, not Cypher/APOC one-liners: slower per call, but driver-portable and unit-testable against an in-memory fake `GraphStore` with no live Neo4j.
- **Cypher itself** is openCypher-specific; the `GraphQuery` AST is the seam — only `Neo4jGraphStore` compiles it to Cypher, a future driver compiles the same AST differently.
- Variable-length multi-hop traversal (`[:CALLS*1..5]`) is common across most property-graph engines — low portability risk. Full-text/vector search *inside* Neo4j is a higher-risk feature to lean on and is explicitly out of scope here: pgvector remains the only embedding store (owner decision, §0); the graph driver never owns embedding storage.

**Migration exercise when the owner decides to swap:** (1) implement a new `GraphStore` driver, (2) a per-tenant data-migration job (`traverse()`/`query()` old → `upsert_node`/`upsert_edge` new), (3) a config flip (`GRAPH_STORE_DRIVER=neo4j|<new>`), (4) decommission old instances via §2's teardown path. If the abstraction holds, consumers need zero code changes.

---

## 4. Consumer integration

### 4a. CodeRAG structural graph

- **Nodes:** `Module`/`Class`/`Method`/`Function`/`Field` — the same def-kinds `_walk_definitions()` (`code_chunker.py:130`) already classifies at chunk granularity, promoted to graph nodes. Key: `{org_id}:{repo_id}:{branch_ref}:{qualified_name}`, stable across the incremental reindex `CodeRagWorker.index()` already keys on (`coderag_worker.py:88`, `(repo_id, branch_ref)`).
- **Edges:** `CALLS`, `EXTENDS`, `IMPLEMENTS`, `CONTAINS` (module→class→method nesting), `REFERENCES`.
- **Incremental emission:** extend the existing per-file content-hash diff loop (`_insert_chunk`, L234) — after producing `CodeChunkDraft`s via tree-sitter for a changed file, walk the same AST for edges and emit via `upsert_node`/`upsert_edge`; a file deletion calls `delete_scope` filtered to that file, keeping graph and chunks consistent together.
- **CK metrics** (WMC, DIT, NOC, CBO, RFC, LCOM) and **circular-dependency detection (Tarjan's SCC)** run as WaddleAI's own Python traversal over `traverse()` output (per the portability note above, not Neo4j GDS), cached per repo/branch rather than recomputed per request. God-class/dead-code/high-coupling are threshold rules over the same metrics plus in-degree-zero non-entrypoint detection.
- **MCP surface:** extend `WaddleAITools` (`shared/mcp/tools.py:285-350`, today `search_code`/`get_symbol`/`search_docs`/`memory_add`/`memory_search`) with `get_call_graph`/`get_class_hierarchy`/`get_architecture_metrics`, org-scoped the same way `resources.py`'s `docs_page`/`repo_chunk` templates already are (L28-57).

### 4b. LightRAG

- **Shape: library, in-process** (within `services/management` or a coderag-adjacent worker), not a standalone sidecar. `lightrag-hku[api]`'s bundled FastAPI server is for LightRAG's own demo Web UI — unneeded, since WaddleAI already has a management API and MCP surface; `coderag_worker.py` is already an in-process worker of the same shape.
- **Graph backend:** a thin shim subclassing LightRAG's `BaseGraphStorage` (`lightrag/base.py` — `has_node`/`upsert_node`/`upsert_edge`/`get_node_edges`/`get_knowledge_graph`, confirmed via LightRAG docs) that maps every call onto `TenantGraphClient`. **Not** LightRAG's native `Neo4JStorage` directly — that would talk to Neo4j straight, bypassing the §3 tenant guard and the abstraction boundary.
- **Embeddings:** `embedding_func=EmbeddingFunc(...)` (LightRAG's documented extension point) wraps the same `embed_cached()` (`shared/knowledge/embed.py`) already used elsewhere — nomic-embed-text via Ollama/penguin-dal, one embedding pipeline, not two.
- **Extraction LLM:** must be ≥2B (house rule, never 1B); LightRAG's own docs recommend ~30B+ with a 32k+ context window for reliable extraction and explicitly advise against reasoning/"thinking" models for the `EXTRACT` role (parallelized calls, reasoning is wasted cost). Route via the existing model-access-policy layer (`docs/superpowers/specs/2026-08-28-model-access-policy-design.md`) with a house-approved non-reasoning ≥2B model pinned to the `EXTRACT` role.
- **Trigger:** piggyback the same content-hash-diff signal `CodeRagWorker` uses, scoped to `docs_cache`-flagged prose content (docs/comments/READMEs) — coderag's AST graph already covers code structure, so LightRAG shouldn't duplicate it.

### 4c. mem0-style graph-memory

mem0's self-hosted OSS `graph_store` integration is gone (§1) — there is no config to wire up. Build it as WaddleAI's own pipeline:

- New `GraphMemoryStore` (parallel to `PgvectorMemoryStore`/`Mem0MemoryStore` in `memory_integration.py`, selected via the existing `create_memory_manager(backend=...)` switch, L761, e.g. `backend="graph"`).
- Reuses mem0's well-documented fact/entity-extraction **prompt pattern** (MIT-licensed prompt text, not SDK code) driven by a house-approved ≥2B LLM, writing extracted entities/relationships via `TenantGraphClient.upsert_node`/`upsert_edge` — the same `GraphStore` interface as §4a/§4b.
- Embeddings via the same Ollama/nomic-embed-text path, used for entity resolution/dedup (is "the proxy service" the same node as an existing "WaddleAI proxy" entry) — mirrors mem0's own historical architecture, just executed against our store.
- **Augments, not replaces** pgvector memory (mirrors mem0's own documented design: vector for semantic recall, graph for multi-hop relationship queries, run side by side) — `PgvectorMemoryStore` stays primary; `GraphMemoryStore` is additive, both queryable from `KnowledgeRetriever.memory_search()` (`retriever.py:114`), fused the way `search_code` already fuses pgvector+FTS (`code_search.py:72`). Confirm with owner before Phase 3 (§9).

---

## 5. Licensing and supply chain

- **Neo4j Community server — GPLv3** (confirmed; not AGPL — Neo4j moved off AGPLv3+Commons-Clause to a GPLv3-Community/proprietary-Enterprise open-core split in 2018). GPLv3 has no network-copyleft clause (unlike AGPLv3 §13): consuming it purely over Bolt/TCP from a separately-deployed, separately-licensed application does not "convey" GPLv3 obligations onto WaddleAI's AGPL-3.0-with-commercial-exception codebase (`LICENSE.md`), **provided** WaddleAI (a) never statically/dynamically links Neo4j source into its own process — it doesn't; it talks over the wire via the Apache-2.0 `neo4j` driver — and (b) never bundles the Neo4j server binary inside a WaddleAI image/installer — it doesn't; Neo4j deploys from its own upstream image as a peer service. **Verdict: acceptable as an interim, isolated dependency** — the §3 driver boundary is doubly load-bearing here, both as the exit path and as the one place that would ever plausibly touch Neo4j's GPLv3 surface.
- **Neo4j Enterprise:** out of scope — Community suffices under the per-instance tenancy model (§2).
- **`neo4j` Python driver:** Apache-2.0 (confirmed: `neo4j/neo4j-python-driver` repo root carries `LICENSE.APACHE2.txt`).
- **LightRAG (`HKUDS/LightRAG`):** MIT-licensed (confirmed). Maintainer: HKUDS lab, University of Hong Kong — a PRC Special Administrative Region, legally a distinct customs/legal territory, but the house rule ("PRC-based or sanctioned entity dependencies: forbidden," `general.md`) targets PRC state-affiliation risk broadly and its plain text doesn't cleanly resolve HKU's status. **Flag for explicit owner sign-off — do not default in or out.** Pure Python, no compiled artifacts, no documented telemetry, which lowers but doesn't eliminate the risk either way.
- **mem0 (`mem0ai`):** already pinned (`requirements.in:48`, resolved `mem0ai==2.0.18`, `requirements.txt:1625`), Apache-2.0, existing house dependency — used here only as design inspiration for the extraction prompt pattern (§4c), not its removed graph_store code path, so no new supply-chain review needed.

---

## 6. Feature flags and tier gating

- `waddleai.graph` — master flag; gates provisioning itself (§2's org-creation hook checks this **before** creating a `graph_instances` row), not just query access — disabled means zero Neo4j pods exist, not just zero queries served.
- Per-consumer, independently toggleable once the master flag is on: `waddleai.coderag_graph` (distinct from the existing chunk-level `waddleai.coderag` flag, `coderag_worker.py:35-44` — the chunk pipeline runs without the graph extension), `waddleai.lightrag`, `waddleai.memory_graph`.
- License entitlement: `license_client.has_feature("waddleai_graph")`, resolved via `get_tier()` — **Enterprise only** (WaddleAI itself is already Enterprise-gated per `critical-rules.md`'s tier table; this gates a level deeper, within WaddleAI). Domain-bypass rule unchanged (`*.penguincloud.io`/`*.penguintech.cloud`/product `.app`).
- Graceful degradation: flag/licence server unreachable → last-known cached value, never crash; never provision or deprovision an instance on a transient flag-check failure — "unknown" means "no change," not "off."

---

## 7. Phasing and dependencies

| Phase | Scope | Depends on |
|---|---|---|
| 1 | `shared/graph/` access layer (`GraphStore` + `TenantGraphClient` + `Neo4jGraphStore`) + per-tenant provisioning (§2) + CodeRAG structural graph (§4a: schema, incremental AST-edge emission, CK metrics/Tarjan's, MCP tools) | **CodeRAG CORE completion** — a sibling plan; `docs/coderag-completion-plan` branch exists but no doc has landed yet (confirmed via worktree check). This phase assumes that plan's chunk-level tree-sitter walk is stable, since edge-emission piggybacks on the same walk. |
| 2 | LightRAG integration (`BaseGraphStorage` shim, ≥2B extraction model wiring, docs-content trigger) | Phase 1 |
| 3 | mem0-style graph-memory (`GraphMemoryStore`, extraction pipeline, entity-resolution) | Phase 1; owner confirmation on §4c/§9 augment-vs-replace |

Each phase is independently flag-gated (§6) so Phase 2/3 slipping never blocks Phase 1 shipping.

---

## 8. Test strategy

- **Unit:** `GraphStore` exercised against an in-memory fake driver — CK-metric computation, Tarjan's SCC, and the query-builder's mandatory-predicate injection are all testable with no live Neo4j.
- **Property-scoping unit tests:** every `TenantGraphClient` method must reject/raise on a `TenantScope` missing `repo_id`/`branch_ref` — no silent unscoped fallback path exists to test against.
- **Tenant isolation, proven not asserted:** an e2e test provisions two tenant instances (testcontainers, or an ephemeral kind/MicroK8s namespace mirroring `local-alpha`), writes distinguishable nodes into org A and org B, then asserts both: (a) network layer — org A's resolved connection can never reach org B's Bolt port (CiliumNetworkPolicy; a direct-connect attempt against the wrong org's Service ClusterIP must refuse/timeout), and (b) client-construction layer — a `TenantScope` for org A can never pair with a connection resolved for org B, since the resolver keys purely off the validated JWT `tenant` claim, never client input (`security.md`). Both layers get their own assertion; passing one isn't evidence for the other.
- **CK-metric regression tests:** small fixture repos with hand-computed WMC/DIT/NOC/CBO/RFC/LCOM, asserted against computed output — catches algorithm drift.
- **LightRAG shim conformance:** exercise the `BaseGraphStorage` shim's `has_node`/`upsert_node`/`get_knowledge_graph`/etc. against the in-memory fake `GraphStore`.
- **Coverage:** 90%+ (`critical-rules.md`), `shared/graph/` added to `.coveragerc`'s source list.

---

## 9. Open questions for the owner

1. **Many-small-orgs fallback:** accept N-pods-per-org as permanently Enterprise-only, or invest later in a shared-pool/bin-packed fallback for lower tiers — the latter explicitly trades away hard physical isolation for those tenants and needs sign-off before it's ever built, not a default.
2. **LightRAG library vs. service:** this spec recommends in-process library use (§4b); confirm, since `lightrag-hku[api]`'s bundled server/Web UI is an alternative if graph visualization tooling is wanted as a bonus.
3. **mem0-graph augment vs. replace:** this spec recommends `GraphMemoryStore` as additive to `PgvectorMemoryStore` (§4c); confirm before Phase 3, since it changes `create_memory_manager`'s backend semantics.
4. **Backup/DR trade-off:** confirm "rebuild-from-source + PVC snapshot" (§2) is acceptable given Neo4j Community has no hot backup — or fund per-tenant Enterprise licences for tenants where that matters, a materially different cost model than this spec assumes.
5. **LightRAG supply-chain sign-off:** HKUDS/University of Hong Kong origin against the PRC/sanctioned-entity rule (§5) — explicit go/no-go needed, the rule's text doesn't resolve Hong Kong SAR status on its own.
6. **Provisioning mechanism:** management-service-driven direct K8s API manifest rendering (recommended, §2) vs. a dedicated operator vs. per-tenant Helm sub-releases — needed before Phase 1 starts; it's the first StatefulSet and the riskiest new infra pattern in this chart.
