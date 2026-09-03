# Shared Graph Platform — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task is self-contained: read only your task plus the **Global Constraints** and **File Structure** sections — you should not need the full design spec.

**Goal:** Build the vendor-abstracted `shared/graph/` access layer (Neo4j-backed, interim) and wire CodeRAG's structural graph as its first consumer, tenant-isolated at the Cypher query layer, flag-gated `waddleai.graph` (fail-safe OFF), proven against a live Neo4j container.

**Architecture:** Consumers depend on a `GraphStore` Protocol and a `TenantGraphClient` guard only; the single `Neo4jGraphStore` driver is the one place `neo4j` and Cypher exist. `TenantGraphClient` resolves the caller's `org_id` (from the validated JWT `tenant` claim) to a physical instance via a `graph_instances` table, and injects `org_id`/`repo_id`/`branch_ref` property predicates into **every** query — so no un-scoped Cypher can be issued (the coderag SQL-scoping invariant, applied to Cypher). The coderag indexer emits AST nodes/edges alongside its existing chunk indexing; call-graph and inheritance queries are exposed over the management REST API + MCP.

**Tech Stack:** Python 3.13, async/await; `neo4j` official async driver (`neo4j.AsyncGraphDatabase`); `tree-sitter` + `tree-sitter-language-pack` (reused from coderag); Quart + penguin-dal (management); Alembic (schema); PostHog feature flags + `penguin_licensing` (Enterprise gate); pytest (unit + `pytest.mark.integration`).

**Spec:** `docs/superpowers/specs/2026-08-31-shared-graph-platform-design.md` (owner-approved). This plan implements §3 (access layer), the dev-mode slice of §2 (resolver only; provisioning deferred), and §4a (coderag structural graph: nodes/edges + call-graph/inheritance queries). It does **not** redesign the spec.

## Global Constraints

Every task's requirements implicitly include this section. Exact values, copied from the spec and house rules:

- **Neo4j confinement:** No `import neo4j` and no Cypher string anywhere outside `shared/graph/drivers/`. Consumers touch `GraphStore`/`TenantGraphClient`/the `shared/graph/types` dataclasses only — never a driver type or a Cypher string. (Interim backend; documented exit path.)
- **Tenant isolation (security-critical):** `org_id` comes from the validated JWT `tenant` claim / `g.user["organization_id"]` only — never request body/params. `TenantGraphClient` injects `org_id` + `repo_id` (+ `branch_ref` when set) as mandatory property predicates into every read, write, traverse, and delete. There is no un-scoped code path. Because Phase 1 dev-mode maps all orgs to **one** shared Neo4j (physical per-instance isolation is deferred with provisioning), `org_id` is a **mandatory Cypher property predicate**, not just physical separation — this is what makes the shared instance safe. Any task touching this needs isolation tests.
- **Async only:** Use the neo4j **native async driver** (`neo4j.AsyncGraphDatabase.driver`, `AsyncSession`) — chosen over `asyncio.to_thread`-wrapping the sync driver because it is first-class in the driver, avoids per-query thread-pool churn on a hot path, and matches the "async only" rule. Blocking penguin-dal reads (the `graph_instances` resolver) still use `asyncio.to_thread`, matching `PgCodeSearchBackend`.
- **Parameterized Cypher:** All values passed as `$`-parameters — never string-interpolated. Labels and relationship types (not parameterizable in Cypher) come from a fixed allowlist (`_NODE_LABELS`, `_EDGE_TYPES`); anything else raises — this is the injection guard for identifiers.
- **`@dataclass(slots=True)`** on every data structure; `from __future__ import annotations` at the top of every module; PEP 257 docstrings (first-line summary + 1–2 lines context) on every class/function.
- **Dependency pinning:** `neo4j` pinned to an exact version with hashes in `requirements.txt` (via `uv pip compile --generate-hashes`); the Neo4j test image pinned by SHA256 digest.
- **Flag + tier gate:** `waddleai.graph` PostHog flag, default OFF, fail-safe (any evaluation error → OFF). Enterprise-only: `penguin_licensing` `check_feature("waddleai_graph")`, fail-closed. Domain-bypass unchanged. Graceful degradation: flag/license unreachable → last-known/OFF, never crash.
- **Coverage:** 90% (branch coverage on) — builds fail below. `shared/graph/` is already covered by `.coveragerc`'s `shared` source entry (no `.coveragerc` change needed). The one genuine live-only line (the `AsyncGraphDatabase.driver(...)` construction in the store factory) carries `# pragma: no cover`; everything else is unit-tested, the async I/O class via an injected `AsyncMock` driver.
- **No LLM in Phase 1:** structural extraction is deterministic tree-sitter — no model call. (The house "any LLM ≥2B, never <2B" rule therefore does not bind Phase 1; it will bind the deferred graph-RAG consumer.)

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `shared/graph/__init__.py` | Create | Package exports (`GraphStore`, `TenantScope`, `TenantGraphClient`, error types) |
| `shared/graph/types.py` | Create | `TenantScope`, `GraphQuery`, `GraphNode`, `GraphEdge`, `GraphPath`, `GraphRecord`; `_NODE_LABELS`/`_EDGE_TYPES` allowlists; `GraphUnavailableError`, `GraphScopeError` |
| `shared/graph/store.py` | Create | `GraphStore` Protocol (vendor-neutral seam) |
| `shared/graph/drivers/__init__.py` | Create | Empty package marker |
| `shared/graph/drivers/neo4j_driver.py` | Create | The ONLY `neo4j` import + Cypher: pure `_compile_*` functions + `Neo4jGraphStore` (async) + `create_neo4j_store()` factory |
| `shared/graph/resolver.py` | Create | `graph_instances` org→instance resolver + dev-mode (`WADDLEAI_GRAPH_BOLT_URL`) |
| `shared/graph/client.py` | Create | `TenantGraphClient` tenant-guard (scope-predicate injection, per-call resolution, `call_graph`/`class_hierarchy`) |
| `shared/knowledge/code_graph.py` | Create | `extract_graph()` + `GraphFragment`/`GraphNodeDraft`/`GraphEdgeDraft` (tree-sitter, deterministic) |
| `services/management/alembic/versions/020_graph_instances.py` | Create | `graph_instances` table migration |
| `services/management/app/models_sqlalchemy.py` | Modify | Add `GraphInstance` model (schema authority + idempotent create) |
| `services/management/app/services/coderag_worker.py` | Modify | Incremental graph emission in `index()`; `_graph_enabled` gate |
| `services/management/app/api/v1/graph.py` | Create | `GET /api/v1/graph/call-graph`, `/class-hierarchy` (flag + Enterprise gated, org from JWT) |
| `services/management/app/api/v1/__init__.py` | Modify | Append `graph,` to the route-module import tuple |
| `shared/mcp/tools.py` | Modify | Extend `KnowledgeService` Protocol + `WaddleAITools` with `get_call_graph`/`get_class_hierarchy` |
| `shared/mcp/graph_adapter.py` | Create | Adapts `TenantGraphClient` to the `KnowledgeService` graph methods |
| `scripts/graph-neo4j.sh` | Create | Start/stop the pinned Neo4j container for integration tests |
| `Makefile` | Modify | `graph-neo4j-up` / `graph-neo4j-down` / `test-graph-integration` targets |
| `tests/unit/graph/fakes.py` | Create | `InMemoryGraphStore` (unit-test oracle, scope-honoring) |
| `tests/unit/graph/test_*.py` | Create | Unit tests for types, fake, cypher, store, resolver, client |
| `tests/unit/knowledge/test_code_graph.py` | Create | Extractor unit tests |
| `tests/unit/management/test_coderag_worker_graph.py`, `test_graph_routes.py`, `test_graph_instances_migration.py` | Create | Worker emission, routes, migration structure |
| `tests/unit/mcp/test_tools_graph.py` | Create | MCP graph tool tests |
| `tests/integration/graph/conftest.py`, `test_graph_isolation.py`, `test_graph_coderag_roundtrip.py`, `test_graph_unavailable.py` | Create | Live-Neo4j integration tests |
| `requirements.in`, `requirements.txt` | Modify | Pin `neo4j` (exact + hashes) |

**Key interfaces (defined once, referenced by later tasks):**

```python
# shared/graph/types.py
@dataclass(slots=True, frozen=True)
class TenantScope:
    org_id: str
    repo_id: str
    branch_ref: str | None = None
    scope_type: str | None = None
    scope_ref: str | None = None
    def node_key(self, qualified_name: str) -> str: ...   # "{org}:{repo}:{branch}:{qn}"
    def scope_props(self) -> dict[str, str]: ...           # {"org_id","repo_id","branch_ref"?}

# shared/graph/store.py  (Protocol — driver-agnostic)
class GraphStore(Protocol):
    async def upsert_node(self, tenant: TenantScope, label: str, key: str, properties: dict) -> None: ...
    async def upsert_edge(self, tenant: TenantScope, edge_type: str, src_key: str, dst_key: str, properties: dict) -> None: ...
    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]: ...
    async def traverse(self, tenant: TenantScope, start_key: str, edge_types: list[str], max_depth: int, direction: Literal["out","in","both"]) -> list[GraphPath]: ...
    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int: ...
    async def close(self) -> None: ...
```

> **Documented deviation from spec §3 signatures:** `delete_scope`'s `repo_id`/`branch_ref` params are collapsed into `TenantScope` (single source of truth — a caller cannot pass a `repo_id` that disagrees with the scope), and an optional `path` is added for the file-granular delete §4a requires. This strengthens the invariant; it does not weaken it.

---

### Task 1: Pin the `neo4j` async driver

**Files:**
- Modify: `requirements.in`, `requirements.txt`
- Test: `tests/unit/graph/test_dependency_pin.py`

**Interfaces:**
- Produces: an importable, exact-pinned `neo4j` package (async driver `neo4j.AsyncGraphDatabase`) for Tasks 4, 5, 14+.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_dependency_pin.py
from __future__ import annotations
import pathlib, re

def test_neo4j_pinned_exact_in_requirements_in() -> None:
    text = pathlib.Path("requirements.in").read_text()
    assert re.search(r"^neo4j==5\.\d+\.\d+\b", text, re.M), "neo4j must be exact-pinned (==), not >=/~="

def test_neo4j_importable_v5() -> None:
    import neo4j
    assert neo4j.__version__.startswith("5."), neo4j.__version__
    assert hasattr(neo4j, "AsyncGraphDatabase")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_dependency_pin.py -v`
Expected: FAIL — `ModuleNotFoundError: neo4j` and/or requirements assertion fails.

- [ ] **Step 3: Add the pin and regenerate hashes**

Add to `requirements.in` under a new `# Graph platform (interim: Neo4j)` heading:
```
# Graph platform (interim backend — confined behind shared/graph/drivers/)
neo4j==5.28.1
```
Then regenerate the hashed lock (never plain pip-compile):
```bash
uv pip compile requirements.in --generate-hashes -o requirements.txt
.venv/bin/pip install -r requirements.txt
```
(If `5.28.1` is unavailable at build time, pick the latest `5.x` release — it must be a 5.x driver matching the Neo4j 5 Community server; keep it exact-pinned.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_dependency_pin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements.in requirements.txt tests/unit/graph/test_dependency_pin.py
git commit -m "build: pin neo4j async driver for the graph platform"
```

---

### Task 2: Graph types + `GraphStore` Protocol

**Files:**
- Create: `shared/graph/types.py`, `shared/graph/store.py`, `shared/graph/__init__.py`
- Test: `tests/unit/graph/test_types.py`

**Interfaces:**
- Produces: `TenantScope` (+ `.node_key()`, `.scope_props()`), `GraphQuery`, `GraphRecord`, `GraphNode`, `GraphEdge`, `GraphPath`, `_NODE_LABELS`, `_EDGE_TYPES`, `GraphUnavailableError`, `GraphScopeError`, and the `GraphStore` Protocol. Consumed by every later task.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_types.py
from __future__ import annotations
import pytest
from shared.graph.types import (
    TenantScope, GraphQuery, GraphRecord, _NODE_LABELS, _EDGE_TYPES,
    GraphUnavailableError, GraphScopeError,
)

def test_scope_key_and_props():
    s = TenantScope(org_id="7", repo_id="42", branch_ref="main")
    assert s.node_key("pkg.Cls.method") == "7:42:main:pkg.Cls.method"
    assert s.scope_props() == {"org_id": "7", "repo_id": "42", "branch_ref": "main"}

def test_scope_props_omits_none_branch():
    s = TenantScope(org_id="7", repo_id="42")
    assert s.scope_props() == {"org_id": "7", "repo_id": "42"}

def test_scope_rejects_empty_org_or_repo():
    with pytest.raises(GraphScopeError):
        TenantScope(org_id="", repo_id="42")
    with pytest.raises(GraphScopeError):
        TenantScope(org_id="7", repo_id="")

def test_label_and_edge_allowlists_frozen():
    assert _NODE_LABELS == frozenset({"Module", "Class", "Method", "Function", "Field"})
    assert _EDGE_TYPES == frozenset({"CALLS", "EXTENDS", "IMPLEMENTS", "CONTAINS", "REFERENCES"})

def test_slots_enforced():
    s = TenantScope(org_id="7", repo_id="42")
    with pytest.raises(AttributeError):
        s.__dict__  # noqa: B018  -- slots dataclass has no __dict__

def test_errors_are_distinct_exceptions():
    assert issubclass(GraphUnavailableError, RuntimeError)
    assert issubclass(GraphScopeError, ValueError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: shared.graph.types`.

- [ ] **Step 3: Write minimal implementation**

`shared/graph/types.py`:
```python
"""Vendor-neutral graph value types and the node/edge allowlists.

Pure data + validation. No neo4j import, no I/O, no Cypher — consumers
depend on these types, never on a driver type, so swapping the backing
store is a driver change, not a rewrite (spec §3).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

_NODE_LABELS = frozenset({"Module", "Class", "Method", "Function", "Field"})
_EDGE_TYPES = frozenset({"CALLS", "EXTENDS", "IMPLEMENTS", "CONTAINS", "REFERENCES"})


class GraphUnavailableError(RuntimeError):
    """The org's graph instance is not ready/reachable — map to a clean 503, never hang."""


class GraphScopeError(ValueError):
    """A TenantScope or query was missing a mandatory tenant field (org_id/repo_id)."""


@dataclass(slots=True, frozen=True)
class TenantScope:
    """The caller's tenant context for one graph operation; org from JWT only."""
    org_id: str
    repo_id: str
    branch_ref: str | None = None
    scope_type: str | None = None
    scope_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.org_id:
            raise GraphScopeError("org_id is required")
        if not self.repo_id:
            raise GraphScopeError("repo_id is required")

    def node_key(self, qualified_name: str) -> str:
        """Composite node identity: '{org}:{repo}:{branch}:{qualified_name}' (spec §4a)."""
        return f"{self.org_id}:{self.repo_id}:{self.branch_ref}:{qualified_name}"

    def scope_props(self) -> dict[str, str]:
        """The mandatory property predicates injected into every query/write."""
        props = {"org_id": self.org_id, "repo_id": self.repo_id}
        if self.branch_ref is not None:
            props["branch_ref"] = self.branch_ref
        return props


@dataclass(slots=True, frozen=True)
class GraphQuery:
    """A node-match AST for GraphStore.query(): label + property predicates + return."""
    labels: tuple[str, ...] = ()
    where: dict[str, Any] = field(default_factory=dict)
    return_keys: bool = True
    limit: int | None = None


@dataclass(slots=True, frozen=True)
class GraphNode:
    key: str
    label: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphEdge:
    edge_type: str
    src_key: str
    dst_key: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphRecord:
    """One row returned by query() — a matched node plus its properties."""
    key: str
    label: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphPath:
    """One traversal path: ordered node keys and the edge types between them."""
    node_keys: tuple[str, ...]
    edge_types: tuple[str, ...]
```

`shared/graph/store.py`:
```python
"""The vendor-neutral GraphStore Protocol — the seam every consumer depends on."""
from __future__ import annotations
from typing import Literal, Protocol, runtime_checkable
from shared.graph.types import GraphPath, GraphQuery, GraphRecord, TenantScope


@runtime_checkable
class GraphStore(Protocol):
    """Property-graph interface. Consumers depend on this, never on Cypher/neo4j (spec §3)."""
    async def upsert_node(self, tenant: TenantScope, label: str, key: str, properties: dict) -> None: ...
    async def upsert_edge(self, tenant: TenantScope, edge_type: str, src_key: str, dst_key: str, properties: dict) -> None: ...
    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]: ...
    async def traverse(self, tenant: TenantScope, start_key: str, edge_types: list[str], max_depth: int, direction: Literal["out", "in", "both"]) -> list[GraphPath]: ...
    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int: ...
    async def close(self) -> None: ...
```

`shared/graph/__init__.py`:
```python
"""Shared vendor-abstracted graph-access layer (spec §3). Neo4j is interim."""
from __future__ import annotations
from shared.graph.store import GraphStore
from shared.graph.types import (
    GraphEdge, GraphNode, GraphPath, GraphQuery, GraphRecord,
    GraphScopeError, GraphUnavailableError, TenantScope,
)

__all__ = [
    "GraphStore", "TenantScope", "GraphQuery", "GraphRecord", "GraphNode",
    "GraphEdge", "GraphPath", "GraphUnavailableError", "GraphScopeError",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/graph/ tests/unit/graph/test_types.py
git commit -m "feat(graph): vendor-neutral graph types and GraphStore Protocol"
```

---

### Task 3: In-memory fake `GraphStore` (unit-test oracle)

**Files:**
- Create: `tests/unit/graph/fakes.py`
- Test: `tests/unit/graph/test_fakes.py`

**Interfaces:**
- Consumes: `GraphStore`, `TenantScope`, `GraphQuery`, `GraphRecord`, `GraphPath` (Task 2).
- Produces: `InMemoryGraphStore` — a scope-honoring fake used by Tasks 8, 11 as the isolation oracle. It stores every node's `properties` (which include `org_id`/`repo_id`/`branch_ref` stamped by the client) and filters reads by the exact property predicates it is given, so a test proving cross-tenant reads return nothing is meaningful.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_fakes.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphQuery, TenantScope
from tests.unit.graph.fakes import InMemoryGraphStore

@pytest.mark.asyncio
async def test_query_filters_by_scope_props():
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props(), "name": "Foo"})
    await store.upsert_node(b, "Class", b.node_key("Bar"), {**b.scope_props(), "name": "Bar"})
    # A queries with A's scope props -> sees only Foo.
    recs = await store.query(a, GraphQuery(labels=("Class",), where=a.scope_props()))
    names = {r.properties["name"] for r in recs}
    assert names == {"Foo"}

@pytest.mark.asyncio
async def test_delete_scope_is_property_scoped():
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props()})
    await store.upsert_node(b, "Class", b.node_key("Bar"), {**b.scope_props()})
    removed = await store.delete_scope(a)
    assert removed == 1
    remaining = await store.query(b, GraphQuery(labels=("Class",), where=b.scope_props()))
    assert len(remaining) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_fakes.py -v`
Expected: FAIL — `ModuleNotFoundError: tests.unit.graph.fakes`.

- [ ] **Step 3: Write minimal implementation**

`tests/unit/graph/fakes.py`:
```python
"""In-memory GraphStore for unit tests — filters reads by the property predicates given."""
from __future__ import annotations
from typing import Literal
from shared.graph.types import GraphEdge, GraphNode, GraphPath, GraphQuery, GraphRecord, TenantScope


def _matches(props: dict, where: dict) -> bool:
    return all(props.get(k) == v for k, v in where.items())


class InMemoryGraphStore:
    """Dict-backed GraphStore; honors scope predicates so isolation tests are real."""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    async def upsert_node(self, tenant, label, key, properties) -> None:
        self.nodes[key] = GraphNode(key=key, label=label, properties=dict(properties))

    async def upsert_edge(self, tenant, edge_type, src_key, dst_key, properties) -> None:
        self.edges.append(GraphEdge(edge_type, src_key, dst_key, dict(properties)))

    async def query(self, tenant, query: GraphQuery) -> list[GraphRecord]:
        out = []
        for n in self.nodes.values():
            if query.labels and n.label not in query.labels:
                continue
            if not _matches(n.properties, query.where):
                continue
            out.append(GraphRecord(key=n.key, label=n.label, properties=n.properties))
        return out[: query.limit] if query.limit else out

    async def traverse(self, tenant, start_key, edge_types, max_depth, direction) -> list[GraphPath]:
        allowed = set(edge_types)
        paths: list[GraphPath] = []
        def walk(node_key, nodes_acc, edges_acc, depth):
            if depth >= max_depth:
                return
            for e in self.edges:
                out_hop = direction in ("out", "both") and e.src_key == node_key
                in_hop = direction in ("in", "both") and e.dst_key == node_key
                if e.edge_type not in allowed or not (out_hop or in_hop):
                    continue
                nxt = e.dst_key if out_hop else e.src_key
                paths.append(GraphPath(tuple(nodes_acc + [nxt]), tuple(edges_acc + [e.edge_type])))
                walk(nxt, nodes_acc + [nxt], edges_acc + [e.edge_type], depth + 1)
        walk(start_key, [start_key], [], 0)
        return paths

    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int:
        where = tenant.scope_props()
        def keep(props: dict) -> bool:
            if not _matches(props, where):
                return True
            return path is not None and props.get("path") != path
        before = len(self.nodes)
        self.nodes = {k: n for k, n in self.nodes.items() if keep(n.properties)}
        self.edges = [e for e in self.edges if keep(e.properties)]
        return before - len(self.nodes)

    async def close(self) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_fakes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/graph/fakes.py tests/unit/graph/test_fakes.py
git commit -m "test(graph): scope-honoring in-memory GraphStore fake"
```

---

### Task 4: Neo4j Cypher compilation (pure functions)

**Files:**
- Create: `shared/graph/drivers/__init__.py`, `shared/graph/drivers/neo4j_driver.py` (compile functions only in this task)
- Test: `tests/unit/graph/test_neo4j_cypher.py`

**Interfaces:**
- Consumes: `TenantScope`, `GraphQuery`, `_NODE_LABELS`, `_EDGE_TYPES` (Task 2).
- Produces: module-level pure functions `compile_upsert_node`, `compile_upsert_edge`, `compile_query`, `compile_traverse`, `compile_delete_scope`, each returning `(cypher: str, params: dict)`. These are the ONLY place Cypher strings are built. Consumed by Task 5's `Neo4jGraphStore`.

**Rule reminders:** every value is a `$`-param; labels/edge-types validated against the allowlist (raise `GraphScopeError` on anything else — the identifier-injection guard); the scope props (`org_id`/`repo_id`/`branch_ref`) are always in the WHERE/MERGE.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_neo4j_cypher.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphQuery, GraphScopeError, TenantScope
from shared.graph.drivers import neo4j_driver as nd

SCOPE = TenantScope(org_id="7", repo_id="42", branch_ref="main")

def test_upsert_node_merges_on_key_and_stamps_scope():
    cypher, params = nd.compile_upsert_node(SCOPE, "Class", SCOPE.node_key("Foo"), {"name": "Foo"})
    assert "MERGE" in cypher and ":Class" in cypher
    assert "$key" in cypher and "$props" in cypher      # no value interpolation
    assert params["key"] == "7:42:main:Foo"
    assert params["props"]["org_id"] == "7" and params["props"]["repo_id"] == "42"
    assert params["props"]["branch_ref"] == "main" and params["props"]["name"] == "Foo"

def test_unknown_label_rejected():
    with pytest.raises(GraphScopeError):
        nd.compile_upsert_node(SCOPE, "Secret", SCOPE.node_key("x"), {})

def test_unknown_edge_type_rejected():
    with pytest.raises(GraphScopeError):
        nd.compile_upsert_edge(SCOPE, "OWNS", "a", "b", {})

def test_query_where_includes_scope_predicates():
    cypher, params = nd.compile_query(SCOPE, GraphQuery(labels=("Class",), where=SCOPE.scope_props()))
    assert "n.org_id = $org_id" in cypher
    assert "n.repo_id = $repo_id" in cypher
    assert "n.branch_ref = $branch_ref" in cypher
    assert params["org_id"] == "7"

def test_traverse_scopes_both_endpoints_and_uses_allowlisted_rels():
    cypher, params = nd.compile_traverse(SCOPE, "7:42:main:Foo", ["CALLS"], 3, "out")
    assert ":CALLS*1..3" in cypher            # variable-length, bounded
    assert "start.org_id = $org_id" in cypher
    assert params["start_key"] == "7:42:main:Foo"

def test_delete_scope_optional_path_predicate():
    c1, p1 = nd.compile_delete_scope(SCOPE, None)
    assert "n.org_id = $org_id" in c1 and "n.path" not in c1
    c2, p2 = nd.compile_delete_scope(SCOPE, "a/b.py")
    assert "n.path = $path" in c2 and p2["path"] == "a/b.py"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_neo4j_cypher.py -v`
Expected: FAIL — module/functions not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/graph/drivers/__init__.py`: empty (`"""Graph driver implementations. The only place neo4j/Cypher lives."""`).

`shared/graph/drivers/neo4j_driver.py` (compile functions; the `import neo4j` and the store class come in Task 5):
```python
"""The Neo4j GraphStore driver — the ONLY module importing neo4j or emitting Cypher.

Cypher-string construction lives in the module-level ``compile_*`` functions
(pure, unit-tested without a live server); ``Neo4jGraphStore`` (Task 5) is a
thin async I/O shell over them. Labels/relationship types are validated
against a fixed allowlist because they cannot be Cypher parameters —
everything else is a $-param (never interpolated).
"""
from __future__ import annotations
from typing import Any, Literal
import neo4j  # the one allowed import site
from neo4j import AsyncGraphDatabase
from shared.graph.types import (
    _EDGE_TYPES, _NODE_LABELS, GraphPath, GraphQuery, GraphRecord, GraphScopeError, TenantScope,
)

_DIR_ARROWS: dict[str, tuple[str, str]] = {"out": ("-", "->"), "in": ("<-", "-"), "both": ("-", "-")}


def _check_label(label: str) -> str:
    if label not in _NODE_LABELS:
        raise GraphScopeError(f"label not in allowlist: {label!r}")
    return label

def _check_edge(edge_type: str) -> str:
    if edge_type not in _EDGE_TYPES:
        raise GraphScopeError(f"edge_type not in allowlist: {edge_type!r}")
    return edge_type

def _scope_where(alias: str, scope: TenantScope) -> tuple[str, dict[str, Any]]:
    clauses = [f"{alias}.org_id = $org_id", f"{alias}.repo_id = $repo_id"]
    params: dict[str, Any] = {"org_id": scope.org_id, "repo_id": scope.repo_id}
    if scope.branch_ref is not None:
        clauses.append(f"{alias}.branch_ref = $branch_ref")
        params["branch_ref"] = scope.branch_ref
    return " AND ".join(clauses), params


def compile_upsert_node(scope: TenantScope, label: str, key: str, properties: dict) -> tuple[str, dict]:
    """MERGE a node on its composite key, stamping the scope props onto it."""
    _check_label(label)
    props = {**properties, **scope.scope_props()}
    cypher = f"MERGE (n:{label} {{key: $key}}) SET n += $props"
    return cypher, {"key": key, "props": props}

def compile_upsert_edge(scope: TenantScope, edge_type: str, src_key: str, dst_key: str, properties: dict) -> tuple[str, dict]:
    """MERGE an allowlisted edge between two scope-matched nodes."""
    _check_edge(edge_type)
    where_s, sp = _scope_where("s", scope)
    props = {**properties, **scope.scope_props()}
    cypher = (
        "MATCH (s {key: $src_key}), (d {key: $dst_key}) "
        f"WHERE {where_s} "
        f"MERGE (s)-[r:{edge_type}]->(d) SET r += $props"
    )
    return cypher, {"src_key": src_key, "dst_key": dst_key, "props": props, **sp}

def compile_query(scope: TenantScope, query: GraphQuery) -> tuple[str, dict]:
    """A scoped node-match. Labels come from the allowlist; scope predicates are mandatory."""
    label_frag = "".join(f":{_check_label(l)}" for l in query.labels)
    where_s, params = _scope_where("n", scope)
    extra = {k: v for k, v in query.where.items() if k not in ("org_id", "repo_id", "branch_ref")}
    for i, (k, v) in enumerate(extra.items()):
        where_s += f" AND n.{k} = $w{i}"
        params[f"w{i}"] = v
    limit = f" LIMIT {int(query.limit)}" if query.limit else ""
    cypher = f"MATCH (n{label_frag}) WHERE {where_s} RETURN n.key AS key, labels(n) AS labels, properties(n) AS props{limit}"
    return cypher, params

def compile_traverse(scope: TenantScope, start_key: str, edge_types: list[str], max_depth: int, direction: Literal["out", "in", "both"]) -> tuple[str, dict]:
    """Bounded variable-length traversal over allowlisted rels, both endpoints scoped."""
    rels = "|".join(_check_edge(e) for e in edge_types)
    left, right = _DIR_ARROWS[direction]
    where_start, params = _scope_where("start", scope)
    params["start_key"] = start_key
    cypher = (
        f"MATCH (start {{key: $start_key}}) WHERE {where_start} "
        f"MATCH p = (start){left}[:{rels}*1..{int(max_depth)}]{right}(end) "
        "WHERE end.org_id = $org_id AND end.repo_id = $repo_id "
        "RETURN [n IN nodes(p) | n.key] AS node_keys, [r IN relationships(p) | type(r)] AS edge_types"
    )
    return cypher, params

def compile_delete_scope(scope: TenantScope, path: str | None) -> tuple[str, dict]:
    """DETACH DELETE every node in this scope (optionally one file's nodes)."""
    where_s, params = _scope_where("n", scope)
    if path is not None:
        where_s += " AND n.path = $path"
        params["path"] = path
    cypher = f"MATCH (n) WHERE {where_s} DETACH DELETE n RETURN count(n) AS deleted"
    return cypher, params
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_neo4j_cypher.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/graph/drivers/ tests/unit/graph/test_neo4j_cypher.py
git commit -m "feat(graph): parameterized Cypher compilers with label/edge allowlist"
```

---

### Task 5: `Neo4jGraphStore` async class + factory

**Files:**
- Modify: `shared/graph/drivers/neo4j_driver.py` (append the class + factory)
- Test: `tests/unit/graph/test_neo4j_store.py`

**Interfaces:**
- Consumes: the `compile_*` functions (Task 4), `GraphStore` Protocol (Task 2).
- Produces: `Neo4jGraphStore(driver)` implementing `GraphStore` over an injected async driver; `create_neo4j_store(bolt_url, user, password) -> Neo4jGraphStore` factory. Consumed by Tasks 8, 14+.

- [ ] **Step 1: Write the failing test** (async driver injected as an `AsyncMock`, so no live server)

```python
# tests/unit/graph/test_neo4j_store.py
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest
from shared.graph.types import GraphQuery, TenantScope
from shared.graph.drivers.neo4j_driver import Neo4jGraphStore

SCOPE = TenantScope(org_id="7", repo_id="42", branch_ref="main")

def _driver_returning(rows: list[dict]):
    result = AsyncMock()
    result.data = AsyncMock(return_value=rows)
    session = AsyncMock()
    session.run = AsyncMock(return_value=result)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session_cm)
    driver.close = AsyncMock()
    return driver, session

@pytest.mark.asyncio
async def test_upsert_node_runs_compiled_cypher():
    driver, session = _driver_returning([])
    store = Neo4jGraphStore(driver)
    await store.upsert_node(SCOPE, "Class", SCOPE.node_key("Foo"), {"name": "Foo"})
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args[1]
    assert "MERGE" in cypher and ":Class" in cypher
    assert kwargs["key"] == "7:42:main:Foo"

@pytest.mark.asyncio
async def test_query_maps_rows_to_records():
    rows = [{"key": "7:42:main:Foo", "labels": ["Class"], "props": {"name": "Foo", "org_id": "7"}}]
    driver, _ = _driver_returning(rows)
    recs = await Neo4jGraphStore(driver).query(SCOPE, GraphQuery(labels=("Class",), where=SCOPE.scope_props()))
    assert recs[0].key == "7:42:main:Foo" and recs[0].label == "Class"

@pytest.mark.asyncio
async def test_traverse_maps_paths():
    rows = [{"node_keys": ["7:42:main:A", "7:42:main:B"], "edge_types": ["CALLS"]}]
    driver, _ = _driver_returning(rows)
    paths = await Neo4jGraphStore(driver).traverse(SCOPE, "7:42:main:A", ["CALLS"], 3, "out")
    assert paths[0].node_keys == ("7:42:main:A", "7:42:main:B") and paths[0].edge_types == ("CALLS",)

@pytest.mark.asyncio
async def test_delete_scope_returns_count_and_close_delegates():
    driver, _ = _driver_returning([{"deleted": 4}])
    store = Neo4jGraphStore(driver)
    assert await store.delete_scope(SCOPE, path="a/b.py") == 4
    await store.close()
    driver.close.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_neo4j_store.py -v`
Expected: FAIL — `Neo4jGraphStore` not defined.

- [ ] **Step 3: Write minimal implementation** (append to `neo4j_driver.py`)

```python
class Neo4jGraphStore:
    """GraphStore over the neo4j async driver. Thin: all Cypher comes from compile_* above."""

    def __init__(self, driver: "neo4j.AsyncDriver") -> None:
        """Bind to an already-constructed async driver (injectable for tests)."""
        self._driver = driver

    async def _run(self, cypher: str, params: dict) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(cypher, **params)
            return await result.data()

    async def upsert_node(self, tenant, label, key, properties) -> None:
        await self._run(*compile_upsert_node(tenant, label, key, properties))

    async def upsert_edge(self, tenant, edge_type, src_key, dst_key, properties) -> None:
        await self._run(*compile_upsert_edge(tenant, edge_type, src_key, dst_key, properties))

    async def query(self, tenant, query: GraphQuery) -> list[GraphRecord]:
        rows = await self._run(*compile_query(tenant, query))
        labels = lambda r: (r["labels"][0] if r.get("labels") else "")
        return [GraphRecord(key=r["key"], label=labels(r), properties=r["props"]) for r in rows]

    async def traverse(self, tenant, start_key, edge_types, max_depth, direction) -> list[GraphPath]:
        rows = await self._run(*compile_traverse(tenant, start_key, edge_types, max_depth, direction))
        return [GraphPath(tuple(r["node_keys"]), tuple(r["edge_types"])) for r in rows]

    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int:
        rows = await self._run(*compile_delete_scope(tenant, path))
        return int(rows[0]["deleted"]) if rows else 0

    async def close(self) -> None:
        await self._driver.close()


def create_neo4j_store(bolt_url: str, user: str, password: str) -> Neo4jGraphStore:
    """Construct a Neo4jGraphStore with a bounded-timeout async driver (fail fast, never hang)."""
    driver = AsyncGraphDatabase.driver(  # pragma: no cover - live I/O, exercised by integration tests
        bolt_url,
        auth=(user, password),
        connection_timeout=5.0,
        connection_acquisition_timeout=10.0,
        max_connection_lifetime=300,
    )
    return Neo4jGraphStore(driver)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_neo4j_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/graph/drivers/neo4j_driver.py tests/unit/graph/test_neo4j_store.py
git commit -m "feat(graph): Neo4jGraphStore async driver + bounded-timeout factory"
```

---

### Task 6: `graph_instances` migration + `GraphInstance` model

**Files:**
- Create: `services/management/alembic/versions/020_graph_instances.py`
- Modify: `services/management/app/models_sqlalchemy.py` (add `GraphInstance`)
- Test: `tests/unit/management/test_graph_instances_migration.py`

**Interfaces:**
- Produces: table `graph_instances` (`id`, `org_id` unique, `status`, `bolt_url`, `created_at`, `updated_at`). Consumed by the resolver (Task 7). Status ∈ `pending|provisioning|ready|failed|deprovisioning|deprovisioned`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_graph_instances_migration.py
from __future__ import annotations
import importlib
from app.models_sqlalchemy import GraphInstance

def test_migration_chain_and_shape():
    mod = importlib.import_module(
        "alembic.versions.020_graph_instances"
    ) if False else __import__(
        "services.management.alembic.versions.020_graph_instances",
        fromlist=["revision"],
    )
    assert mod.revision == "020_graph_instances"
    assert mod.down_revision == "019_code_repos_webhook_secret"
    assert callable(mod.upgrade) and callable(mod.downgrade)

def test_model_columns():
    cols = {c.name for c in GraphInstance.__table__.columns}
    assert {"id", "org_id", "status", "bolt_url", "created_at", "updated_at"} <= cols
    assert GraphInstance.__tablename__ == "graph_instances"

def test_status_values_documented():
    from app.models_sqlalchemy import GRAPH_INSTANCE_STATUSES
    assert GRAPH_INSTANCE_STATUSES == (
        "pending", "provisioning", "ready", "failed", "deprovisioning", "deprovisioned",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_graph_instances_migration.py -v`
Expected: FAIL — `GraphInstance` / migration module not found.

- [ ] **Step 3: Write minimal implementation**

`services/management/alembic/versions/020_graph_instances.py` (mirror 018/019 exactly):
```python
"""graph_instances: per-org graph-store instance registry (spec §2, Phase 1 dev-mode).

One row per org. Phase 1 dev-mode resolves every org to ONE shared Neo4j
(env WADDLEAI_GRAPH_BOLT_URL); the StatefulSet-per-tenant provisioning that
populates status/bolt_url per org is deferred to the next slice. The
org->instance resolver treats anything but status='ready' as
feature-unavailable (clean 503), never a hang.

Revision ID: 020_graph_instances
Revises: 019_code_repos_webhook_secret
Create Date: 2026-09-01
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "020_graph_instances"
down_revision: str | None = "019_code_repos_webhook_secret"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``graph_instances`` (one per org; status-gated resolution)."""
    op.create_table(
        "graph_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        # pending|provisioning|ready|failed|deprovisioning|deprovisioned
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("bolt_url", sa.String(512), nullable=True),  # NULL until ready
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", name="uq_graph_instances_org"),
    )


def downgrade() -> None:
    """Drop ``graph_instances``."""
    op.drop_table("graph_instances")
```

Add to `services/management/app/models_sqlalchemy.py` (near `CodeRepo`), plus a module-level tuple:
```python
GRAPH_INSTANCE_STATUSES = (
    "pending", "provisioning", "ready", "failed", "deprovisioning", "deprovisioned",
)


class GraphInstance(Base):
    """Per-org graph-store instance registry (spec §2). Phase 1: dev-mode shared Neo4j."""

    __tablename__ = "graph_instances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="pending", server_default="pending")
    bolt_url = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("org_id", name="uq_graph_instances_org"),)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_graph_instances_migration.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/alembic/versions/020_graph_instances.py services/management/app/models_sqlalchemy.py tests/unit/management/test_graph_instances_migration.py
git commit -m "feat(graph): graph_instances table + GraphInstance model"
```

---

### Task 7: Instance resolver + dev mode

**Files:**
- Create: `shared/graph/resolver.py`
- Test: `tests/unit/graph/test_resolver.py`

**Interfaces:**
- Consumes: `GraphUnavailableError` (Task 2); `graph_instances` table (Task 6). Reads via `db.executesql` (raw SQL, matching `PgCodeSearchBackend`) so no PyDAL table definition is required.
- Produces: `@dataclass(slots=True) ResolvedInstance(bolt_url, user, password)`; `async resolve_instance(db, org_id) -> ResolvedInstance` (raises `GraphUnavailableError` unless status='ready' AND bolt_url set); `async ensure_dev_instance(db, org_id) -> None` (upserts a ready row from `WADDLEAI_GRAPH_BOLT_URL` when set); `async resolve_or_dev(db, org_id) -> ResolvedInstance`. Consumed by Task 8 (client) and Tasks 11/12 (worker/routes) via a resolver callable.

**Dev mode:** when `WADDLEAI_GRAPH_BOLT_URL` is set, all orgs resolve to that one Neo4j (per-instance provisioning deferred). Credentials from `WADDLEAI_GRAPH_USER` (default `neo4j`) / `WADDLEAI_GRAPH_PASSWORD`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_resolver.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphUnavailableError
from shared.graph.resolver import resolve_instance, resolve_or_dev

class FakeDB:
    def __init__(self, row): self._row = row; self.written = []
    def executesql(self, sql, params=None):
        if sql.strip().upper().startswith("SELECT"):
            return [self._row] if self._row else []
        self.written.append((sql, params)); return []

@pytest.mark.asyncio
async def test_ready_resolves(monkeypatch):
    monkeypatch.setenv("WADDLEAI_GRAPH_USER", "neo4j")
    monkeypatch.setenv("WADDLEAI_GRAPH_PASSWORD", "secret")
    db = FakeDB(("ready", "bolt://neo4j:7687"))
    inst = await resolve_instance(db, org_id=7)
    assert inst.bolt_url == "bolt://neo4j:7687" and inst.user == "neo4j" and inst.password == "secret"

@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, ("pending", None), ("failed", "bolt://x"), ("ready", None)])
async def test_non_ready_is_unavailable(row):
    with pytest.raises(GraphUnavailableError):
        await resolve_instance(FakeDB(row), org_id=7)

@pytest.mark.asyncio
async def test_dev_mode_autocreates_ready(monkeypatch):
    monkeypatch.setenv("WADDLEAI_GRAPH_BOLT_URL", "bolt://localhost:7687")
    monkeypatch.setenv("WADDLEAI_GRAPH_PASSWORD", "secret")
    db = FakeDB(None)          # no row yet
    inst = await resolve_or_dev(db, org_id=7)
    assert inst.bolt_url == "bolt://localhost:7687"
    assert any("graph_instances" in sql for sql, _ in db.written)   # upserted a ready row

@pytest.mark.asyncio
async def test_no_dev_and_no_row_is_unavailable(monkeypatch):
    monkeypatch.delenv("WADDLEAI_GRAPH_BOLT_URL", raising=False)
    with pytest.raises(GraphUnavailableError):
        await resolve_or_dev(FakeDB(None), org_id=7)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_resolver.py -v`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/graph/resolver.py`:
```python
"""org_id -> physical graph instance resolution, with a Phase-1 dev-mode short-circuit.

Reads graph_instances via raw executesql (no PyDAL table def needed). Anything
but status='ready' with a bolt_url is a clean GraphUnavailableError -> 503,
never a hang. Dev-mode (WADDLEAI_GRAPH_BOLT_URL set) resolves every org to one
shared Neo4j; per-instance StatefulSet provisioning is deferred (spec §2).
"""
from __future__ import annotations
import asyncio
import os
from dataclasses import dataclass
from shared.graph.types import GraphUnavailableError


@dataclass(slots=True, frozen=True)
class ResolvedInstance:
    """A ready graph instance connection triple; credentials resolved server-side."""
    bolt_url: str
    user: str
    password: str


def _creds() -> tuple[str, str]:
    return os.getenv("WADDLEAI_GRAPH_USER", "neo4j"), os.getenv("WADDLEAI_GRAPH_PASSWORD", "")


async def resolve_instance(db: object, org_id: int) -> ResolvedInstance:
    """Resolve a ready instance for org_id, or raise GraphUnavailableError."""
    def _read() -> tuple | None:
        rows = db.executesql(  # nosec B608 - fixed literal, org_id bound
            "SELECT status, bolt_url FROM graph_instances WHERE org_id = %s LIMIT 1",
            [org_id],
        )
        return rows[0] if rows else None
    row = await asyncio.to_thread(_read)
    if not row or row[0] != "ready" or not row[1]:
        raise GraphUnavailableError(f"graph instance for org {org_id} is not ready")
    user, password = _creds()
    return ResolvedInstance(bolt_url=row[1], user=user, password=password)


async def ensure_dev_instance(db: object, org_id: int) -> None:
    """Dev-mode: upsert a ready graph_instances row pointing at WADDLEAI_GRAPH_BOLT_URL."""
    bolt = os.getenv("WADDLEAI_GRAPH_BOLT_URL")
    if not bolt:
        return
    def _upsert() -> None:
        db.executesql(  # nosec B608 - fixed literal, values bound
            "INSERT INTO graph_instances (org_id, status, bolt_url) VALUES (%s, 'ready', %s) "
            "ON CONFLICT (org_id) DO UPDATE SET status='ready', bolt_url=EXCLUDED.bolt_url, "
            "updated_at=now()",
            [org_id, bolt],
        )
        db.commit()
    await asyncio.to_thread(_upsert)


async def resolve_or_dev(db: object, org_id: int) -> ResolvedInstance:
    """resolve_instance, first materializing the dev-mode shared instance when configured."""
    await ensure_dev_instance(db, org_id)
    return await resolve_instance(db, org_id)
```

> Note: `FakeDB` in the test has no `.commit()`; give the fake a `commit(self): pass` method, or have `ensure_dev_instance` guard `getattr(db, "commit", None)`. Prefer adding `def commit(self): pass` to the test fake.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/graph/resolver.py tests/unit/graph/test_resolver.py
git commit -m "feat(graph): org->instance resolver with dev-mode shared Neo4j"
```

---

### Task 8: `TenantGraphClient` tenant-guard (security-critical)

**Files:**
- Create: `shared/graph/client.py`
- Test: `tests/unit/graph/test_client.py`

**Interfaces:**
- Consumes: `resolve_or_dev`/`ResolvedInstance` (Task 7), `GraphStore`/`InMemoryGraphStore` (Tasks 2/3), `TenantScope`/`GraphQuery`/`GraphPath`/`GraphUnavailableError` (Task 2).
- Produces: `TenantGraphClient(db, store_factory=create_neo4j_store, resolver=resolve_or_dev)` with methods `upsert_node(scope,label,qualified_name,props)`, `upsert_edge(scope,edge_type,src_qn,dst_qn,props)`, `query(scope,query)`, `traverse(scope,start_qn,edge_types,max_depth,direction)`, `call_graph(scope,symbol,direction,depth)`, `class_hierarchy(scope,symbol,direction)`, `delete_scope(scope,path=None)`, `aclose()`. Consumed by Tasks 11 (worker), 12 (routes), 13 (MCP).

**Invariant this task enforces (test it):** no method ever issues a store call without `org_id`+`repo_id`(+`branch_ref`) predicates. The client stamps scope props on every write and injects them into every read/traverse/delete — a `GraphQuery.where` is always merged with `scope.scope_props()` before it reaches the store. Keys are always built via `scope.node_key(qualified_name)` so a consumer passes a bare qualified name, never a cross-tenant key.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_client.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphQuery, GraphUnavailableError, TenantScope
from shared.graph.resolver import ResolvedInstance
from shared.graph.client import TenantGraphClient
from tests.unit.graph.fakes import InMemoryGraphStore

A = TenantScope(org_id="A", repo_id="1", branch_ref="main")
B = TenantScope(org_id="B", repo_id="1", branch_ref="main")

def _client(store, *, ready=True):
    async def resolver(db, org_id):
        if not ready:
            raise GraphUnavailableError("not ready")
        return ResolvedInstance("bolt://x", "neo4j", "pw")
    return TenantGraphClient(db=object(), store_factory=lambda inst: store, resolver=resolver)

@pytest.mark.asyncio
async def test_shared_instance_is_isolated_by_scope_props():
    store = InMemoryGraphStore()                 # ONE shared store, like dev-mode's one Neo4j
    ca, cb = _client(store), _client(store)
    await ca.upsert_node(A, "Class", "Foo", {"name": "Foo"})
    await cb.upsert_node(B, "Class", "Bar", {"name": "Bar"})
    recs = await ca.query(A, GraphQuery(labels=("Class",)))
    assert {r.properties["name"] for r in recs} == {"Foo"}     # A cannot see B in the shared store

@pytest.mark.asyncio
async def test_query_where_is_merged_with_scope_props():
    store = InMemoryGraphStore()
    ca = _client(store)
    await ca.upsert_node(A, "Class", "Foo", {"name": "Foo"})
    # Even if a caller passes an empty where, scope props are injected -> B's data is invisible.
    await _client(store).upsert_node(B, "Class", "Foo", {"name": "OtherFoo"})
    recs = await ca.query(A, GraphQuery(labels=("Class",), where={}))
    assert all(r.properties["org_id"] == "A" for r in recs)

@pytest.mark.asyncio
async def test_upsert_stamps_key_and_scope_props():
    store = InMemoryGraphStore()
    await _client(store).upsert_node(A, "Class", "pkg.Foo", {"name": "Foo"})
    node = store.nodes["A:1:main:pkg.Foo"]
    assert node.properties["org_id"] == "A" and node.properties["repo_id"] == "1"

@pytest.mark.asyncio
async def test_call_graph_and_class_hierarchy_use_expected_edges():
    store = InMemoryGraphStore()
    c = _client(store)
    await c.upsert_node(A, "Function", "a", {}); await c.upsert_node(A, "Function", "b", {})
    await c.upsert_edge(A, "CALLS", "a", "b", {})
    paths = await c.call_graph(A, "a", direction="out", depth=3)
    assert any(p.edge_types == ("CALLS",) for p in paths)

@pytest.mark.asyncio
async def test_unavailable_instance_raises_not_hangs():
    with pytest.raises(GraphUnavailableError):
        await _client(InMemoryGraphStore(), ready=False).query(A, GraphQuery())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/graph/test_client.py -v`
Expected: FAIL — `TenantGraphClient` not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/graph/client.py`:
```python
"""The tenant-guard over GraphStore — no un-scoped query can be issued (spec §3).

Resolves the caller's org_id to a physical instance per call (never accepts a
pre-resolved connection), builds every node key from TenantScope.node_key(),
and merges TenantScope.scope_props() into every read/traverse/delete predicate
and every write's properties. This is the coderag SQL-scoping invariant applied
to Cypher; the property-scoping (incl. org_id) is what makes the Phase-1 shared
Neo4j safe.
"""
from __future__ import annotations
from dataclasses import replace
from typing import Callable, Literal
from shared.graph.resolver import ResolvedInstance, resolve_or_dev
from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.store import GraphStore
from shared.graph.types import GraphPath, GraphQuery, GraphRecord, TenantScope


class TenantGraphClient:
    """Scope-guarded facade over a GraphStore; one physical instance per org."""

    def __init__(
        self,
        db: object,
        *,
        store_factory: Callable[[ResolvedInstance], GraphStore] = None,
        resolver: Callable = resolve_or_dev,
    ) -> None:
        """Bind to a penguin-dal handle; store_factory/resolver are injectable for tests."""
        self._db = db
        self._resolver = resolver
        self._store_factory = store_factory or (
            lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password)
        )
        self._stores: dict[str, GraphStore] = {}

    async def _store(self, scope: TenantScope) -> GraphStore:
        inst = await self._resolver(self._db, int(scope.org_id))
        if inst.bolt_url not in self._stores:
            self._stores[inst.bolt_url] = self._store_factory(inst)
        return self._stores[inst.bolt_url]

    async def upsert_node(self, scope, label, qualified_name, props) -> None:
        store = await self._store(scope)
        await store.upsert_node(scope, label, scope.node_key(qualified_name), {**props, "qualified_name": qualified_name})

    async def upsert_edge(self, scope, edge_type, src_qn, dst_qn, props) -> None:
        store = await self._store(scope)
        await store.upsert_edge(scope, edge_type, scope.node_key(src_qn), scope.node_key(dst_qn), props)

    async def query(self, scope: TenantScope, query: GraphQuery) -> list[GraphRecord]:
        store = await self._store(scope)
        scoped = replace(query, where={**query.where, **scope.scope_props()})
        return await store.query(scope, scoped)

    async def traverse(self, scope, start_qn, edge_types, max_depth, direction) -> list[GraphPath]:
        store = await self._store(scope)
        return await store.traverse(scope, scope.node_key(start_qn), edge_types, max_depth, direction)

    async def call_graph(self, scope, symbol, *, direction: Literal["out", "in", "both"] = "out", depth: int = 3) -> list[GraphPath]:
        """Callers of / callees from a symbol (spec §4a): traverse CALLS."""
        return await self.traverse(scope, symbol, ["CALLS"], depth, direction)

    async def class_hierarchy(self, scope, symbol, *, direction: Literal["out", "in", "both"] = "out", depth: int = 5) -> list[GraphPath]:
        """Inheritance chain (spec §4a): traverse EXTENDS/IMPLEMENTS."""
        return await self.traverse(scope, symbol, ["EXTENDS", "IMPLEMENTS"], depth, direction)

    async def delete_scope(self, scope: TenantScope, path: str | None = None) -> int:
        store = await self._store(scope)
        return await store.delete_scope(scope, path=path)

    async def aclose(self) -> None:
        for store in self._stores.values():
            await store.close()
        self._stores.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/graph/test_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/graph/client.py tests/unit/graph/test_client.py
git commit -m "feat(graph): TenantGraphClient scope-guard (no un-scoped Cypher)"
```

---

### Task 9: `code_graph` structural extractor — nodes + CONTAINS

**Files:**
- Create: `shared/knowledge/code_graph.py`
- Test: `tests/unit/knowledge/test_code_graph.py`

**Interfaces:**
- Consumes: `tree_sitter_language_pack.get_parser`, and `_GRAMMARS`/`_resolve_language`/`_get_name`/`_looks_binary` from `shared/knowledge/code_chunker.py` (reuses the same grammar node-type sets + parser as the chunker — same AST, graph-shaped output).
- Produces: `@dataclass(slots=True, frozen=True) GraphNodeDraft(label, qualified_name, name, path)`, `GraphEdgeDraft(edge_type, src_qn, dst_qn, path)`, `GraphFragment(nodes, edges)`, and `extract_graph(path, source, language=None) -> GraphFragment`. This task emits `Module`/`Class`/`Method`/`Function` nodes and `CONTAINS` edges (module→top-level def, class→method). Consumed by Task 10 (adds edges/Field) and Task 11 (worker emission).

Node-label mapping (reuses the chunker's kind classification): `class`→`Class`, top-level `function`→`Function`, `method` (function inside a class)→`Method`. The file itself → one `Module` node. Qualified names nest by definition name: top-level `foo`→`foo`, class `C`→`C`, method `m` in `C`→`C.m`. The `Module` node's `qualified_name` is the repo-relative `path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/knowledge/test_code_graph.py
from __future__ import annotations
from shared.knowledge.code_graph import extract_graph

PY = '''
def top():
    pass

class C:
    def m(self):
        pass
'''

def test_nodes_and_contains():
    frag = extract_graph("pkg/mod.py", PY)
    labels = {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Module", "pkg/mod.py") in labels
    assert ("Function", "top") in labels
    assert ("Class", "C") in labels
    assert ("Method", "C.m") in labels
    contains = {(e.src_qn, e.dst_qn) for e in frag.edges if e.edge_type == "CONTAINS"}
    assert ("pkg/mod.py", "top") in contains        # module contains top-level fn
    assert ("pkg/mod.py", "C") in contains          # module contains class
    assert ("C", "C.m") in contains                 # class contains method

def test_non_parseable_returns_module_only_no_crash():
    frag = extract_graph("data.bin", "\x00\x01binary")
    assert [n.label for n in frag.nodes] == ["Module"]
    assert frag.edges == []

def test_every_node_carries_path():
    frag = extract_graph("pkg/mod.py", PY)
    assert all(n.path == "pkg/mod.py" for n in frag.nodes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/knowledge/test_code_graph.py -v`
Expected: FAIL — module not defined.

- [ ] **Step 3: Write minimal implementation**

`shared/knowledge/code_graph.py`:
```python
"""Structural graph extraction from the same tree-sitter AST coderag chunks (spec §4a).

Deterministic — tree-sitter only, no LLM. Emits Module/Class/Method/Function
nodes and CONTAINS nesting edges here; Task 10 adds Field nodes and
EXTENDS/IMPLEMENTS/CALLS/REFERENCES edges (Python reference language). Reuses
the chunker's grammar node-type sets and parser (shared/knowledge/code_chunker).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from shared.knowledge.code_chunker import _GRAMMARS, _get_name, _looks_binary, _resolve_language


@dataclass(slots=True, frozen=True)
class GraphNodeDraft:
    """One structural node destined for the graph, keyed later by TenantScope.node_key(qn)."""
    label: str
    qualified_name: str
    name: str | None
    path: str


@dataclass(slots=True, frozen=True)
class GraphEdgeDraft:
    """One structural edge between two qualified names within this file's scope."""
    edge_type: str
    src_qn: str
    dst_qn: str
    path: str


@dataclass(slots=True)
class GraphFragment:
    """All nodes + edges extracted from one file, for incremental emission."""
    nodes: list[GraphNodeDraft] = field(default_factory=list)
    edges: list[GraphEdgeDraft] = field(default_factory=list)


def _parse(source: str, language: str):
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(language).parse(source.encode("utf-8"))
    except Exception:
        return None


def extract_graph(path: str, source: str, language: str | None = None) -> GraphFragment:
    """Extract Module/Class/Method/Function nodes + CONTAINS edges from ``source``."""
    frag = GraphFragment()
    frag.nodes.append(GraphNodeDraft("Module", path, path, path))

    lang = _resolve_language(path, language)
    grammar = _GRAMMARS.get(lang) if lang else None
    if grammar is None or _looks_binary(source):
        return frag                      # module-only for binary/unmapped/unparseable
    tree = _parse(source, lang)
    if tree is None:
        return frag

    def walk(node, parent_qn: str, class_qn: str | None) -> None:
        for child in node.children:
            nt = child.type
            if nt in grammar.class_types:
                name = _get_name(child) or "anonymous"
                qn = name if class_qn is None else f"{class_qn}.{name}"
                frag.nodes.append(GraphNodeDraft("Class", qn, name, path))
                frag.edges.append(GraphEdgeDraft("CONTAINS", parent_qn, qn, path))
                walk(child, qn, qn)      # recurse into class body for methods
            elif nt in grammar.function_types or nt in grammar.method_types:
                name = _get_name(child) or "anonymous"
                label = "Method" if class_qn is not None else "Function"
                qn = f"{class_qn}.{name}" if class_qn is not None else name
                frag.nodes.append(GraphNodeDraft(label, qn, name, path))
                frag.edges.append(GraphEdgeDraft("CONTAINS", parent_qn, qn, path))
                # do not recurse into function bodies (no nested-closure nodes)
            else:
                walk(child, parent_qn, class_qn)

    walk(tree.root_node, path, None)
    return frag
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/knowledge/test_code_graph.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/knowledge/code_graph.py tests/unit/knowledge/test_code_graph.py
git commit -m "feat(graph): tree-sitter node + CONTAINS extraction"
```

---

### Task 10: `code_graph` edges — Field, EXTENDS, CALLS, REFERENCES (Python)

**Files:**
- Modify: `shared/knowledge/code_graph.py`
- Test: `tests/unit/knowledge/test_code_graph_edges.py`

**Interfaces:**
- Produces (extends `GraphFragment`): `Field` nodes (class-body assignment targets) + `CONTAINS` (class→field); `EXTENDS` edges (Python `class_definition.superclasses`); `CALLS` edges (intra-file call resolution — an unqualified call name matched to a known top-level function or same-class method); `REFERENCES` edges (a known class/function name used inside another definition's body).

> **Decomposition decision (documented):** structural **edge** extraction targets **Python** as the reference language in Phase 1 (nodes + CONTAINS from Task 9 are already generic across the chunker's grammars). This bounds Phase 1 to a runnable, live-Neo4j-provable slice; adding a language is a grammar-by-grammar follow-on that never touches the graph layer or the client. Cross-file CALLS resolution is also out (intra-file only); a call to an unresolved (external/imported) name is skipped, not guessed. `IMPLEMENTS` (interfaces) is a non-Python concept and is emitted only once a grammar that distinguishes interfaces is added.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/knowledge/test_code_graph_edges.py
from __future__ import annotations
from shared.knowledge.code_graph import extract_graph

PY = '''
class Base:
    pass

class Derived(Base):
    kind = "d"
    def run(self):
        return helper()

def helper():
    return 1
'''

def _edges(frag, t):
    return {(e.src_qn, e.dst_qn) for e in frag.edges if e.edge_type == t}

def test_extends_edge():
    frag = extract_graph("m.py", PY)
    assert ("Derived", "Base") in _edges(frag, "EXTENDS")

def test_field_node_and_contains():
    frag = extract_graph("m.py", PY)
    assert ("Field", "Derived.kind") in {(n.label, n.qualified_name) for n in frag.nodes}
    assert ("Derived", "Derived.kind") in _edges(frag, "CONTAINS")

def test_calls_edge_intrafile():
    frag = extract_graph("m.py", PY)
    assert ("Derived.run", "helper") in _edges(frag, "CALLS")

def test_unresolved_external_call_skipped():
    frag = extract_graph("m.py", "def f():\n    external_thing()\n")
    assert _edges(frag, "CALLS") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/knowledge/test_code_graph_edges.py -v`
Expected: FAIL — edges/Field not yet emitted.

- [ ] **Step 3: Write minimal implementation**

Extend `extract_graph` (Python-only edge pass). After building nodes/CONTAINS, when `lang == "python"`, run a second pass over the same `tree`:

```python
# --- append inside code_graph.py; called from extract_graph when lang == "python" ---
def _python_edges(tree, path: str, frag: GraphFragment) -> None:
    """EXTENDS/Field/CALLS/REFERENCES for Python (reference language, spec §4a)."""
    known_functions = {n.qualified_name for n in frag.nodes if n.label in ("Function", "Method")}
    known_classes = {n.qualified_name for n in frag.nodes if n.label == "Class"}

    def name_text(n) -> str | None:
        nm = n.child_by_field_name("name")
        return nm.text.decode("utf-8", "replace") if nm is not None else None

    def walk(node, class_qn: str | None, def_qn: str | None) -> None:
        for child in node.children:
            t = child.type
            if t == "class_definition":
                cname = name_text(child) or "anonymous"
                cqn = cname if class_qn is None else f"{class_qn}.{cname}"
                supers = child.child_by_field_name("superclasses")
                if supers is not None:
                    for s in supers.children:
                        if s.type == "identifier":
                            frag.edges.append(GraphEdgeDraft("EXTENDS", cqn, s.text.decode("utf-8", "replace"), path))
                # Field: class-body top-level assignments (identifier = ...)
                body = child.child_by_field_name("body")
                for stmt in (body.children if body is not None else []):
                    if stmt.type == "expression_statement" and stmt.children and stmt.children[0].type == "assignment":
                        target = stmt.children[0].child_by_field_name("left")
                        if target is not None and target.type == "identifier":
                            fname = target.text.decode("utf-8", "replace")
                            fqn = f"{cqn}.{fname}"
                            frag.nodes.append(GraphNodeDraft("Field", fqn, fname, path))
                            frag.edges.append(GraphEdgeDraft("CONTAINS", cqn, fqn, path))
                walk(child, cqn, def_qn)
            elif t == "function_definition":
                fname = name_text(child) or "anonymous"
                fqn = f"{class_qn}.{fname}" if class_qn is not None else fname
                walk(child, class_qn, fqn)   # descend into body with def context
            elif t == "call":
                if def_qn is not None:
                    fn = child.child_by_field_name("function")
                    callee = None
                    if fn is not None and fn.type == "identifier":
                        callee = fn.text.decode("utf-8", "replace")
                    elif fn is not None and fn.type == "attribute" and class_qn is not None:
                        attr = fn.child_by_field_name("attribute")
                        if attr is not None:
                            callee = f"{class_qn}.{attr.text.decode('utf-8', 'replace')}"
                    if callee in known_functions:
                        frag.edges.append(GraphEdgeDraft("CALLS", def_qn, callee, path))
                    elif callee in known_classes and def_qn is not None:
                        frag.edges.append(GraphEdgeDraft("REFERENCES", def_qn, callee, path))
                walk(child, class_qn, def_qn)
            else:
                walk(child, class_qn, def_qn)

    walk(tree.root_node, None, None)
```

Then in `extract_graph`, after the node/CONTAINS walk:
```python
    if lang == "python":
        _python_edges(tree, path, frag)
    return frag
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/knowledge/test_code_graph_edges.py tests/unit/knowledge/test_code_graph.py -v`
Expected: PASS (both files — the node/CONTAINS suite still passes).

- [ ] **Step 5: Commit**

```bash
git add shared/knowledge/code_graph.py tests/unit/knowledge/test_code_graph_edges.py
git commit -m "feat(graph): Python EXTENDS/CALLS/REFERENCES/Field edge extraction"
```

---

### Task 11: Worker incremental graph emission

**Files:**
- Modify: `services/management/app/services/coderag_worker.py`
- Test: `tests/unit/management/test_coderag_worker_graph.py`

**Interfaces:**
- Consumes: `extract_graph` (Tasks 9/10), `TenantScope`/`TenantGraphClient` (Tasks 2/8), `is_feature_enabled` (existing).
- Produces (on `CodeRagWorker`): `_graph_enabled(org_id) -> bool` (flag `waddleai.graph`, fail-safe OFF); `async _emit_graph_changes(graph_client, scope, clone_dir, changed, deleted) -> None`; `index()` now emits graph nodes/edges alongside chunks when the flag is on. `CodeRagWorker.__init__` gains `graph_client: TenantGraphClient | None = None` (injectable for tests; built from `self.db` when None and the flag is on). `IndexResult` gains `graph_status: str = "skipped"`.

**Behavior:** the graph runs *alongside* the chunk pipeline — with `waddleai.graph` OFF the chunk index is byte-for-byte unchanged (no graph client constructed, no emission). A graph-store failure is logged and sets `graph_status="error"` but never fails the chunk index (the graph is a rebuildable index, spec §2). Changed file → `delete_scope(scope, path)` then re-emit its nodes/edges. Deleted file → `delete_scope(scope, path)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_coderag_worker_graph.py
from __future__ import annotations
import os
import pathlib
import pytest
from app.services.coderag_worker import CodeRagWorker
from shared.graph.types import TenantScope

class FakeGraphClient:
    def __init__(self): self.upserts = []; self.edges = []; self.deletes = []
    async def upsert_node(self, scope, label, qn, props): self.upserts.append((label, qn))
    async def upsert_edge(self, scope, et, s, d, props): self.edges.append((et, s, d))
    async def delete_scope(self, scope, path=None): self.deletes.append(path)

@pytest.mark.asyncio
async def test_emit_graph_changes_upserts_and_deletes(tmp_path: pathlib.Path):
    (tmp_path / "m.py").write_text("class C:\n    def m(self):\n        pass\n")
    worker = CodeRagWorker(db=object())
    gc = FakeGraphClient()
    scope = TenantScope(org_id="7", repo_id="42", branch_ref="main")
    await worker._emit_graph_changes(gc, scope, str(tmp_path), changed=["m.py"], deleted=["old.py"])
    assert ("Class", "C") in gc.upserts and ("Method", "C.m") in gc.upserts
    assert "old.py" in gc.deletes          # deleted file scrubbed
    assert "m.py" in gc.deletes            # changed file scrubbed before re-emit

def test_graph_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("WADDLEAI_FLAG_GRAPH", raising=False)
    assert CodeRagWorker(db=object())._graph_enabled(7) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_coderag_worker_graph.py -v`
Expected: FAIL — `_emit_graph_changes`/`_graph_enabled` not defined.

- [ ] **Step 3: Write minimal implementation**

Add imports and the flag/emission logic to `coderag_worker.py`:
```python
from shared.knowledge.code_graph import extract_graph
from shared.graph.types import TenantScope

_GRAPH_FLAG_KEY = "waddleai.graph"
```
On `CodeRagWorker`:
```python
    def _graph_enabled(self, org_id: int) -> bool:
        """Fail-safe-OFF check of the ``waddleai.graph`` flag (spec §6)."""
        try:
            from shared.utils.feature_flags import is_feature_enabled
            return is_feature_enabled(_GRAPH_FLAG_KEY, distinct_id=str(org_id), default=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("graph flag evaluation failed, treating as OFF: %s", exc)
            return False

    async def _emit_graph_changes(self, graph_client, scope, clone_dir, changed, deleted) -> None:
        """Mirror the chunk diff into the graph: re-emit changed files, scrub deleted ones."""
        for path in deleted:
            await graph_client.delete_scope(scope, path=path)
        for path in changed:
            await graph_client.delete_scope(scope, path=path)
            try:
                content = (pathlib.Path(clone_dir) / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            frag = extract_graph(path, content)
            for node in frag.nodes:
                await graph_client.upsert_node(scope, node.label, node.qualified_name,
                                               {"name": node.name, "path": node.path})
            for edge in frag.edges:
                await graph_client.upsert_edge(scope, edge.edge_type, edge.src_qn, edge.dst_qn,
                                               {"path": edge.path})
```
(Add `import pathlib` at the top.) Extend `__init__(self, db, workdir=None, graph_client=None)` storing `self.graph_client = graph_client`. Add `graph_status: str = "skipped"` to `IndexResult`. In `index()`, after the changed/deleted DB loop and before `_mark_indexed`:
```python
        graph_status = "skipped"
        if self._graph_enabled(repo_row["org_id"]):
            from shared.graph.client import TenantGraphClient
            client = self.graph_client or TenantGraphClient(self.db)
            scope = TenantScope(org_id=str(repo_row["org_id"]), repo_id=str(repo_id), branch_ref=branch_ref)
            try:
                await self._emit_graph_changes(client, scope, clone_dir, changed, deleted)
                graph_status = "emitted"
            except Exception as exc:  # graph is a rebuildable index — never fail the chunk index
                logger.error("coderag graph emission failed for repo %s: %s", repo_id, exc)
                graph_status = "error"
```
and set `graph_status=graph_status` on the returned `IndexResult`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_coderag_worker_graph.py tests/unit/management/test_coderag_worker.py -v`
Expected: PASS (existing worker tests still green — `graph_status` has a default, flag defaults OFF).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/coderag_worker.py tests/unit/management/test_coderag_worker_graph.py
git commit -m "feat(graph): incremental graph emission in the coderag worker"
```

---

### Task 12: Management REST graph query routes

**Files:**
- Create: `services/management/app/api/v1/graph.py`
- Modify: `services/management/app/api/v1/__init__.py` (append `graph,` to the import tuple)
- Test: `tests/unit/management/test_graph_routes.py`

**Interfaces:**
- Consumes: `TenantGraphClient`/`TenantScope` (Tasks 8/2), `GraphUnavailableError` (Task 2), `is_feature_enabled` + `penguin_licensing` (existing patterns from `code_repos.py`/`model_access_policies.py`).
- Produces: `GET /api/v1/graph/call-graph?repo=&branch=&symbol=&direction=&depth=` and `GET /api/v1/graph/class-hierarchy?repo=&branch=&symbol=&direction=` — `@require_auth` only (read-only, org-scoped, mirroring `list_code_repos`); org from JWT (`g.user["organization_id"]`); flag `waddleai.graph` (404 if off) + Enterprise `check_feature("waddleai_graph")` (403); repo name→repo_id resolved org-scoped (unknown → 404, IDOR-safe); `GraphUnavailableError` → 503. Explicit response schema (never a raw object).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/management/test_graph_routes.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphPath, GraphUnavailableError

# The route module exposes helpers the test drives directly (Quart app wiring is
# covered by integration tests); unit-test the gate + serialization + error mapping.
from app.api.v1 import graph as graph_routes

class FakeClient:
    def __init__(self, paths=None, raise_unavailable=False):
        self._paths = paths or []; self._raise = raise_unavailable
    async def call_graph(self, scope, symbol, *, direction="out", depth=3):
        if self._raise: raise GraphUnavailableError("nope")
        return self._paths

def test_serialize_paths_shape():
    paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
    out = graph_routes._serialize_paths(paths)
    assert out == [{"nodes": ["7:42:main:a", "7:42:main:b"], "edges": ["CALLS"]}]

@pytest.mark.asyncio
async def test_run_call_graph_maps_unavailable_to_503():
    body, status = await graph_routes._run_call_graph(
        FakeClient(raise_unavailable=True),
        org_id=7, repo_id=42, branch="main", symbol="a", direction="out", depth=3,
    )
    assert status == 503

@pytest.mark.asyncio
async def test_run_call_graph_happy_path():
    paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
    body, status = await graph_routes._run_call_graph(
        FakeClient(paths=paths), org_id=7, repo_id=42, branch="main", symbol="a", direction="out", depth=3,
    )
    assert status == 200 and body["data"]["paths"][0]["edges"] == ["CALLS"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/management/test_graph_routes.py -v`
Expected: FAIL — module/helpers not defined.

- [ ] **Step 3: Write minimal implementation**

`services/management/app/api/v1/graph.py` — model the flag/entitlement gate on `model_access_policies.py` and the org-scoped resolution + IDOR pattern on `code_repos.py`:
```python
"""§4a CodeRAG graph queries: ``/api/v1/graph/call-graph`` and ``/class-hierarchy``.

Read-only, org-scoped from the validated JWT (a repo outside the caller's org
resolves to 404, IDOR-safe). Two-layer gate: ``waddleai.graph`` PostHog flag
(404 if off) + Enterprise ``waddleai_graph`` license entitlement (403). A
non-ready/unreachable graph instance maps to a clean 503, never a hang.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Any
from quart import g, jsonify, request
from ...extensions import db
from . import api_v1_bp
from .auth import require_auth
from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphPath, GraphScopeError, GraphUnavailableError, TenantScope
from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)
_FLAG_KEY = "waddleai.graph"
_LICENSE_FEATURE = "waddleai_graph"
_license_client: Any = None


def _get_license_client() -> Any:
    global _license_client
    if _license_client is None:
        from penguin_licensing import LicenseClient
        _license_client = LicenseClient(
            license_key=os.environ.get("LICENSE_KEY", ""),
            product="waddleai",
            base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
        )
    return _license_client


def _flag_enabled(org_id: int | None) -> bool:
    return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id or "server"), default=False)


async def _entitled() -> bool:
    def _check() -> bool:
        try:
            return bool(_get_license_client().check_feature(_LICENSE_FEATURE))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("graph: entitlement check failed: %s", exc)
            return False
    return await asyncio.to_thread(_check)


async def _gate(org_id: int | None) -> tuple | None:
    if not _flag_enabled(org_id):
        return jsonify({"status": "error", "error": "not_found"}), 404
    if not await _entitled():
        return jsonify({"status": "error", "error": "graph requires an Enterprise entitlement (waddleai_graph)"}), 403
    return None


def _serialize_paths(paths: list[GraphPath]) -> list[dict]:
    return [{"nodes": list(p.node_keys), "edges": list(p.edge_types)} for p in paths]


async def _resolve_repo_id(org_id: int, repo_name: str) -> int | None:
    def _fetch() -> Any:
        return db((db.code_repos.org_id == org_id) & (db.code_repos.name == repo_name)).select().first()
    row = await asyncio.to_thread(_fetch)
    return int(row.id) if row else None


async def _run_call_graph(client, *, org_id, repo_id, branch, symbol, direction, depth) -> tuple[dict, int]:
    scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch)
    try:
        paths = await client.call_graph(scope, symbol, direction=direction, depth=depth)
    except GraphUnavailableError:
        return {"status": "error", "error": "graph unavailable"}, 503
    return {"status": "success", "data": {"paths": _serialize_paths(paths)}}, 200


async def _run_class_hierarchy(client, *, org_id, repo_id, branch, symbol, direction) -> tuple[dict, int]:
    scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch)
    try:
        paths = await client.class_hierarchy(scope, symbol, direction=direction)
    except GraphUnavailableError:
        return {"status": "error", "error": "graph unavailable"}, 503
    return {"status": "success", "data": {"paths": _serialize_paths(paths)}}, 200


@api_v1_bp.route("/graph/call-graph", methods=["GET"])
@require_auth
async def call_graph_route() -> tuple[Any, int]:
    """Call-graph traversal from a symbol, org-scoped."""
    org_id = g.user.get("organization_id")
    gate = await _gate(org_id)
    if gate is not None:
        return gate
    repo_name = request.args.get("repo", "")
    symbol = request.args.get("symbol", "")
    branch = request.args.get("branch", "main")
    direction = request.args.get("direction", "out")
    depth = min(int(request.args.get("depth", 3)), 10)
    if not repo_name or not symbol:
        return jsonify({"status": "error", "error": "repo and symbol are required"}), 400
    repo_id = await _resolve_repo_id(org_id, repo_name)
    if repo_id is None:
        return jsonify({"status": "error", "error": "not found"}), 404
    client = TenantGraphClient(db)
    body, status = await _run_call_graph(client, org_id=org_id, repo_id=repo_id, branch=branch,
                                         symbol=symbol, direction=direction, depth=depth)
    return jsonify(body), status


@api_v1_bp.route("/graph/class-hierarchy", methods=["GET"])
@require_auth
async def class_hierarchy_route() -> tuple[Any, int]:
    """Inheritance-chain traversal from a symbol, org-scoped."""
    org_id = g.user.get("organization_id")
    gate = await _gate(org_id)
    if gate is not None:
        return gate
    repo_name = request.args.get("repo", "")
    symbol = request.args.get("symbol", "")
    branch = request.args.get("branch", "main")
    direction = request.args.get("direction", "out")
    if not repo_name or not symbol:
        return jsonify({"status": "error", "error": "repo and symbol are required"}), 400
    repo_id = await _resolve_repo_id(org_id, repo_name)
    if repo_id is None:
        return jsonify({"status": "error", "error": "not found"}), 404
    client = TenantGraphClient(db)
    body, status = await _run_class_hierarchy(client, org_id=org_id, repo_id=repo_id, branch=branch,
                                              symbol=symbol, direction=direction)
    return jsonify(body), status
```
Append `graph,` to the `from . import (...)` tuple in `services/management/app/api/v1/__init__.py` (append at the end, per that file's append-only comment — do not reorder).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/management/test_graph_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/graph.py services/management/app/api/v1/__init__.py tests/unit/management/test_graph_routes.py
git commit -m "feat(graph): call-graph + class-hierarchy REST routes (flag + Enterprise gated)"
```

---

### Task 13: MCP graph tools

**Files:**
- Modify: `shared/mcp/tools.py` (extend `KnowledgeService` Protocol + `WaddleAITools`)
- Create: `shared/mcp/graph_adapter.py` (`GraphKnowledgeService` — adapts `TenantGraphClient` to the graph Protocol methods)
- Test: `tests/unit/mcp/test_tools_graph.py`

**Interfaces:**
- Consumes: `ToolContext`/`WaddleAITools`/`KnowledgeService`/`ToolDisabledError` (existing `shared/mcp/tools.py`); `TenantGraphClient`/`TenantScope`/`GraphUnavailableError` (Tasks 8/2).
- Produces: `KnowledgeService.get_call_graph(*, org_id, repo, branch, symbol, direction, depth)` and `get_class_hierarchy(*, org_id, repo, branch, symbol, direction)`; `WaddleAITools.get_call_graph(repo, symbol, branch=None, direction="out", depth=3)` and `get_class_hierarchy(...)` — subject-free (org from `ctx.org_id`), `_require_enabled()`-gated (`waddleai.mcp_v2`). `GraphKnowledgeService` (repo name→repo_id org-scoped via db; `waddleai.graph` flag check; `GraphUnavailableError`→empty list, never a hang).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp/test_tools_graph.py
from __future__ import annotations
import pytest
from shared.mcp.tools import WaddleAITools, ToolContext, ToolDisabledError

class FakeKnowledge:
    def __init__(self): self.calls = []
    async def get_call_graph(self, **kw): self.calls.append(("cg", kw)); return [{"nodes": [], "edges": []}]
    async def get_class_hierarchy(self, **kw): self.calls.append(("ch", kw)); return []
    # other KnowledgeService methods unused here

def _tools(monkeypatch, enabled=True):
    monkeypatch.setenv("WADDLEAI_FLAG_MCP_V2", "1" if enabled else "0")
    ctx = ToolContext(org_id=7, user_uuid="u", session_id="s", workspace_hint=None, scopes=frozenset())
    kn = FakeKnowledge()
    return WaddleAITools(ctx, knowledge=kn, memory=object(), routing=object(), usage=object()), kn

@pytest.mark.asyncio
async def test_get_call_graph_uses_ctx_org(monkeypatch):
    tools, kn = _tools(monkeypatch)
    await tools.get_call_graph(repo="r", symbol="a")
    kind, kw = kn.calls[0]
    assert kind == "cg" and kw["org_id"] == 7 and kw["repo"] == "r" and kw["symbol"] == "a"

@pytest.mark.asyncio
async def test_graph_tool_disabled_when_flag_off(monkeypatch):
    tools, _ = _tools(monkeypatch, enabled=False)
    with pytest.raises(ToolDisabledError):
        await tools.get_call_graph(repo="r", symbol="a")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/mcp/test_tools_graph.py -v`
Expected: FAIL — `get_call_graph` not defined on `WaddleAITools`.

- [ ] **Step 3: Write minimal implementation**

In `shared/mcp/tools.py`, add to the `KnowledgeService` Protocol:
```python
    async def get_call_graph(self, *, org_id: int, repo: str, branch: str | None, symbol: str, direction: str, depth: int) -> list[dict[str, Any]]:
        """Call-graph paths from a symbol, org-scoped (§4a)."""
        ...

    async def get_class_hierarchy(self, *, org_id: int, repo: str, branch: str | None, symbol: str, direction: str) -> list[dict[str, Any]]:
        """Inheritance paths from a symbol, org-scoped (§4a)."""
        ...
```
Add to `WaddleAITools` (subject-free — org from ctx):
```python
    async def get_call_graph(self, repo: str, symbol: str, branch: str | None = None, direction: str = "out", depth: int = 3) -> list[dict[str, Any]]:
        """Call-graph traversal, scoped to the caller's org (§4a)."""
        self._require_enabled()
        return await self._knowledge.get_call_graph(
            org_id=self._ctx.org_id, repo=repo, branch=branch, symbol=symbol, direction=direction, depth=depth,
        )

    async def get_class_hierarchy(self, repo: str, symbol: str, branch: str | None = None, direction: str = "out") -> list[dict[str, Any]]:
        """Class-hierarchy traversal, scoped to the caller's org (§4a)."""
        self._require_enabled()
        return await self._knowledge.get_class_hierarchy(
            org_id=self._ctx.org_id, repo=repo, branch=branch, symbol=symbol, direction=direction,
        )
```

`shared/mcp/graph_adapter.py`:
```python
"""Adapts TenantGraphClient to the KnowledgeService graph methods for MCP (§4a).

Resolves the repo name to an org-scoped repo_id, builds a TenantScope from the
caller's org (never client input), and serializes GraphPaths. A graph instance
that is not ready returns an empty list rather than hanging — the flag/entitlement
gate proper lives on the REST surface and on provisioning.
"""
from __future__ import annotations
import asyncio
from typing import Any
from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphPath, GraphUnavailableError, TenantScope
from shared.utils.feature_flags import is_feature_enabled


def _serialize(paths: list[GraphPath]) -> list[dict[str, Any]]:
    return [{"nodes": list(p.node_keys), "edges": list(p.edge_types)} for p in paths]


class GraphKnowledgeService:
    """KnowledgeService graph methods backed by TenantGraphClient."""

    def __init__(self, db: object, client: TenantGraphClient | None = None) -> None:
        """Bind to a penguin-dal handle; client injectable for tests."""
        self._db = db
        self._client = client or TenantGraphClient(db)

    async def _repo_id(self, org_id: int, repo: str) -> int | None:
        def _fetch() -> Any:
            rows = self._db.executesql(  # nosec B608 - fixed literal, values bound
                "SELECT id FROM code_repos WHERE org_id = %s AND name = %s LIMIT 1", [org_id, repo])
            return int(rows[0][0]) if rows else None
        return await asyncio.to_thread(_fetch)

    async def get_call_graph(self, *, org_id, repo, branch, symbol, direction, depth) -> list[dict[str, Any]]:
        if not is_feature_enabled("waddleai.graph", distinct_id=str(org_id), default=False):
            return []
        repo_id = await self._repo_id(org_id, repo)
        if repo_id is None:
            return []
        scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch or "main")
        try:
            return _serialize(await self._client.call_graph(scope, symbol, direction=direction, depth=depth))
        except GraphUnavailableError:
            return []

    async def get_class_hierarchy(self, *, org_id, repo, branch, symbol, direction) -> list[dict[str, Any]]:
        if not is_feature_enabled("waddleai.graph", distinct_id=str(org_id), default=False):
            return []
        repo_id = await self._repo_id(org_id, repo)
        if repo_id is None:
            return []
        scope = TenantScope(org_id=str(org_id), repo_id=str(repo_id), branch_ref=branch or "main")
        try:
            return _serialize(await self._client.class_hierarchy(scope, symbol, direction=direction))
        except GraphUnavailableError:
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/mcp/test_tools_graph.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add shared/mcp/tools.py shared/mcp/graph_adapter.py tests/unit/mcp/test_tools_graph.py
git commit -m "feat(graph): MCP get_call_graph/get_class_hierarchy tools + adapter"
```

---

### Task 14: Live-Neo4j integration harness

**Files:**
- Create: `scripts/graph-neo4j.sh`
- Modify: `Makefile` (`graph-neo4j-up` / `graph-neo4j-down` / `test-graph-integration`)
- Create: `tests/integration/graph/__init__.py`, `tests/integration/graph/conftest.py`, `tests/integration/graph/test_connectivity.py`

**Interfaces:**
- Produces: a pinned-by-digest `neo4j:5-community` container started/stopped by `make`, and a session fixture `graph_client` (a real `TenantGraphClient` bound to `WADDLEAI_GRAPH_BOLT_URL`) that `pytest.skip`s when Neo4j is unreachable — mirroring the existing `ollama_available` probe. Consumed by Tasks 15–17.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/graph/test_connectivity.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphQuery, TenantScope
pytestmark = pytest.mark.integration

@pytest.mark.asyncio
async def test_neo4j_round_trips(graph_client, seed_ready_instance):
    scope = TenantScope(org_id="ping", repo_id="1", branch_ref="main")
    await graph_client.upsert_node(scope, "Module", "m.py", {"path": "m.py"})
    recs = await graph_client.query(scope, GraphQuery(labels=("Module",)))
    assert any(r.properties.get("path") == "m.py" for r in recs)
    await graph_client.delete_scope(scope)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make graph-neo4j-up && make test-graph-integration`
Expected: FAIL — harness/fixtures not defined (or, without the container up, SKIP; the fixtures must exist first).

- [ ] **Step 3: Write the harness**

`scripts/graph-neo4j.sh` (`set -euo pipefail`; digest resolved once and pinned):
```bash
#!/usr/bin/env bash
set -euo pipefail
# Pin the image by digest. Resolve once with:
#   docker pull neo4j:5-community && docker inspect --format='{{index .RepoDigests 0}}' neo4j:5-community
# then paste the neo4j:5-community@sha256:... value below (dependency-pinning rule).
NEO4J_IMAGE="${NEO4J_IMAGE:?set NEO4J_IMAGE to the pinned neo4j:5-community@sha256:<digest>}"
NAME=waddleai-graph-neo4j
PASSWORD="${WADDLEAI_GRAPH_PASSWORD:-testpassword}"
case "${1:-up}" in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -p 7687:7687 \
      -e NEO4J_AUTH="neo4j/${PASSWORD}" "$NEO4J_IMAGE" >/dev/null
    echo "waiting for bolt..."
    for _ in $(seq 1 30); do
      if docker exec "$NAME" cypher-shell -u neo4j -p "$PASSWORD" "RETURN 1" >/dev/null 2>&1; then
        echo "neo4j ready on bolt://localhost:7687"; exit 0
      fi; sleep 2
    done
    echo "neo4j did not become ready" >&2; exit 1 ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
```
`chmod +x scripts/graph-neo4j.sh`. Makefile targets:
```make
graph-neo4j-up:
	@NEO4J_IMAGE=$(NEO4J_IMAGE) scripts/graph-neo4j.sh up

graph-neo4j-down:
	@scripts/graph-neo4j.sh down

test-graph-integration:
	@echo "Running live-Neo4j graph integration tests..."
	WADDLEAI_GRAPH_BOLT_URL=bolt://localhost:7687 WADDLEAI_GRAPH_USER=neo4j \
	  WADDLEAI_GRAPH_PASSWORD=$${WADDLEAI_GRAPH_PASSWORD:-testpassword} \
	  $(PY) -m pytest tests/integration/graph -v --no-cov
```
Add `graph-neo4j-up graph-neo4j-down test-graph-integration` to the `.PHONY` line, and define `NEO4J_IMAGE ?= neo4j:5-community@sha256:<pinned-digest>` near the top of the Makefile (the digest resolved via the `pinning-dependency-digests` skill or the `docker inspect` command above).

`tests/integration/graph/conftest.py`:
```python
"""Live-Neo4j fixtures. Skips when bolt is unreachable (mirrors ollama_available)."""
from __future__ import annotations
import os
import pytest
import pytest_asyncio
from shared.graph.client import TenantGraphClient
from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.resolver import ResolvedInstance

BOLT = os.getenv("WADDLEAI_GRAPH_BOLT_URL", "")
USER = os.getenv("WADDLEAI_GRAPH_USER", "neo4j")
PASSWORD = os.getenv("WADDLEAI_GRAPH_PASSWORD", "")


def _bolt_up() -> bool:
    if not BOLT:
        return False
    try:
        import socket
        host_port = BOLT.split("//", 1)[-1]
        host, port = host_port.split(":")
        with socket.create_connection((host, int(port)), timeout=3):
            return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def graph_client():
    """A TenantGraphClient whose resolver returns the one shared test Neo4j; skips if down."""
    if not _bolt_up():
        pytest.skip("Neo4j not reachable — run `make graph-neo4j-up` first")
    async def resolver(db, org_id):
        return ResolvedInstance(BOLT, USER, PASSWORD)
    client = TenantGraphClient(db=object(), store_factory=lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password), resolver=resolver)
    yield client
    await client.aclose()


@pytest.fixture
def seed_ready_instance():
    """No-op marker fixture — the shared test instance is always 'ready' in dev-mode."""
    return True
```
(Add `pytest-asyncio` if not already a dev dep — it is already used by the async unit tests; reuse the existing pin.)

- [ ] **Step 4: Run test to verify it passes**

Run: `make graph-neo4j-up && make test-graph-integration && make graph-neo4j-down`
Expected: PASS (connectivity round-trip); teardown removes the container.

- [ ] **Step 5: Commit**

```bash
git add scripts/graph-neo4j.sh Makefile tests/integration/graph/__init__.py tests/integration/graph/conftest.py tests/integration/graph/test_connectivity.py
git commit -m "test(graph): live-Neo4j integration harness (make target + fixtures)"
```

---

### Task 15: Integration — tenant property-scoping isolation (live Neo4j)

**Files:**
- Create: `tests/integration/graph/test_graph_isolation.py`

**Interfaces:**
- Consumes: `graph_client` fixture (Task 14). Proves spec §8 test (a): in the ONE shared instance, an org's query cannot see another org's nodes (property-scoping is the only isolation in dev-mode — this is the security-critical invariant).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/graph/test_graph_isolation.py
from __future__ import annotations
import pytest
from shared.graph.types import GraphQuery, TenantScope
pytestmark = pytest.mark.integration

A = TenantScope(org_id="orgA", repo_id="1", branch_ref="main")
B = TenantScope(org_id="orgB", repo_id="1", branch_ref="main")

@pytest.mark.asyncio
async def test_org_cannot_read_another_orgs_nodes(graph_client):
    try:
        await graph_client.upsert_node(A, "Class", "Secret", {"name": "A-Secret"})
        await graph_client.upsert_node(B, "Class", "Secret", {"name": "B-Secret"})
        a_recs = await graph_client.query(A, GraphQuery(labels=("Class",)))
        names = {r.properties.get("name") for r in a_recs}
        assert names == {"A-Secret"}                     # never B-Secret, same physical instance
        # traverse is scoped too: an A-scoped traverse from B's key returns nothing
        await graph_client.upsert_node(A, "Function", "a1", {}); await graph_client.upsert_node(A, "Function", "a2", {})
        await graph_client.upsert_edge(A, "CALLS", "a1", "a2", {})
        assert await graph_client.traverse(B, "a1", ["CALLS"], 3, "out") == []
    finally:
        await graph_client.delete_scope(A)
        await graph_client.delete_scope(B)

@pytest.mark.asyncio
async def test_delete_scope_does_not_touch_other_org(graph_client):
    try:
        await graph_client.upsert_node(A, "Class", "Ka", {})
        await graph_client.upsert_node(B, "Class", "Kb", {})
        await graph_client.delete_scope(A)
        b_recs = await graph_client.query(B, GraphQuery(labels=("Class",)))
        assert len(b_recs) == 1                          # B intact
    finally:
        await graph_client.delete_scope(B)
```

- [ ] **Step 2: Run to verify it fails (before Tasks 2–8 land) / passes (after)**

Run: `make graph-neo4j-up && make test-graph-integration`
Expected: with the stack built, PASS; a scoping regression makes `names == {"A-Secret"}` fail loudly.

- [ ] **Step 3: (No new implementation)** — this task is a proof over Tasks 2–8. If it fails, the defect is in `TenantGraphClient`/`compile_*`, not the test.

- [ ] **Step 4: Run to verify it passes**

Run: `make test-graph-integration`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/graph/test_graph_isolation.py
git commit -m "test(graph): live-Neo4j tenant property-scoping isolation proof"
```

---

### Task 16: Integration — coderag extraction + query round-trip (live Neo4j)

**Files:**
- Create: `tests/integration/graph/test_graph_coderag_roundtrip.py`

**Interfaces:**
- Consumes: `graph_client` fixture (Task 14), `extract_graph` (Tasks 9/10). Proves spec §8 test (b): extract a fixture's structural graph, emit it, and round-trip call-graph + inheritance queries.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/graph/test_graph_coderag_roundtrip.py
from __future__ import annotations
import pytest
from shared.knowledge.code_graph import extract_graph
from shared.graph.types import TenantScope
pytestmark = pytest.mark.integration

SRC = '''
class Base:
    pass

class Derived(Base):
    def run(self):
        return helper()

def helper():
    return 1
'''

S = TenantScope(org_id="rt", repo_id="9", branch_ref="main")

@pytest.mark.asyncio
async def test_extraction_and_queries_round_trip(graph_client):
    try:
        frag = extract_graph("m.py", SRC)
        for n in frag.nodes:
            await graph_client.upsert_node(S, n.label, n.qualified_name, {"name": n.name, "path": n.path})
        for e in frag.edges:
            await graph_client.upsert_edge(S, e.edge_type, e.src_qn, e.dst_qn, {"path": e.path})
        # call-graph: Derived.run -> helper
        calls = await graph_client.call_graph(S, "Derived.run", direction="out", depth=3)
        reached = {k for p in calls for k in p.node_keys}
        assert S.node_key("helper") in reached
        # inheritance: Derived -> Base
        hier = await graph_client.class_hierarchy(S, "Derived", direction="out")
        reached_h = {k for p in hier for k in p.node_keys}
        assert S.node_key("Base") in reached_h
    finally:
        await graph_client.delete_scope(S)

@pytest.mark.asyncio
async def test_file_delete_scrubs_only_that_file(graph_client):
    try:
        frag = extract_graph("m.py", SRC)
        for n in frag.nodes:
            await graph_client.upsert_node(S, n.label, n.qualified_name, {"name": n.name, "path": n.path})
        await graph_client.upsert_node(S, "Module", "other.py", {"path": "other.py"})
        await graph_client.delete_scope(S, path="m.py")
        from shared.graph.types import GraphQuery
        remaining = await graph_client.query(S, GraphQuery(labels=("Module",)))
        paths = {r.properties.get("path") for r in remaining}
        assert paths == {"other.py"}                    # m.py scrubbed, other.py kept
    finally:
        await graph_client.delete_scope(S)
```

- [ ] **Step 2–4: Run**

Run: `make graph-neo4j-up && make test-graph-integration`
Expected: PASS (proves extraction + emission + call-graph/inheritance/file-delete round-trip against live Neo4j).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/graph/test_graph_coderag_roundtrip.py
git commit -m "test(graph): live-Neo4j coderag extraction + query round-trip"
```

---

### Task 17: Integration — unavailable/non-ready → clean 503, never a hang (live Neo4j)

**Files:**
- Create: `tests/integration/graph/test_graph_unavailable.py`

**Interfaces:**
- Consumes: `resolve_instance`/`ResolvedInstance` (Task 7), `create_neo4j_store` (Task 5), `TenantGraphClient` (Task 8), `GraphUnavailableError` (Task 2). Proves spec §8 test (c): a non-ready instance resolves to a prompt `GraphUnavailableError`, and an unreachable bolt_url fails fast within the bounded timeout — never hangs.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/graph/test_graph_unavailable.py
from __future__ import annotations
import time
import pytest
from shared.graph.client import TenantGraphClient
from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.resolver import ResolvedInstance, resolve_instance
from shared.graph.types import GraphQuery, GraphUnavailableError, TenantScope
pytestmark = pytest.mark.integration

class FakeDB:
    def __init__(self, row): self._row = row
    def executesql(self, sql, params=None): return [self._row] if self._row else []
    def commit(self): ...

@pytest.mark.asyncio
async def test_non_ready_row_is_unavailable_fast():
    start = time.monotonic()
    with pytest.raises(GraphUnavailableError):
        await resolve_instance(FakeDB(("provisioning", None)), org_id=1)
    assert time.monotonic() - start < 2.0            # a DB lookup, never a hang

@pytest.mark.asyncio
async def test_unreachable_bolt_fails_within_bounded_timeout():
    async def resolver(db, org_id):
        return ResolvedInstance("bolt://127.0.0.1:9", "neo4j", "x")   # closed port
    client = TenantGraphClient(db=object(),
        store_factory=lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password),
        resolver=resolver)
    scope = TenantScope(org_id="1", repo_id="1", branch_ref="main")
    start = time.monotonic()
    with pytest.raises(Exception):                    # neo4j.exceptions.ServiceUnavailable or similar
        await client.query(scope, GraphQuery(labels=("Class",)))
    elapsed = time.monotonic() - start
    assert elapsed < 15.0                             # bounded by connection_acquisition_timeout, no hang
    await client.aclose()
```

- [ ] **Step 2–4: Run**

Run: `make graph-neo4j-up && make test-graph-integration`
Expected: PASS. If the second test hangs, the driver factory's timeouts (Task 5) are not being applied — fix there.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/graph/test_graph_unavailable.py
git commit -m "test(graph): live-Neo4j unavailable/non-ready fails fast (no hang)"
```

---

## Out of Scope for Phase 1 (next slice)

Do **not** create tasks for these — they are the immediate follow-ups, named here so the executor doesn't scope-creep:

- **K8s StatefulSet-per-tenant provisioning automation** (spec §2): the management service rendering/applying per-org Neo4j `StatefulSet` + headless `Service` + `PVC` + `Secret` + `CiliumNetworkPolicy`, driving the `graph_instances` lifecycle (`pending`→`provisioning`→`ready`/`failed`), and teardown on soft-delete. Needs a live cluster + Enterprise-tier ops. **Phase 1 substitutes the dev-mode resolver: all orgs resolve to one shared Neo4j via `WADDLEAI_GRAPH_BOLT_URL`.** This is the immediate next slice — once it lands, physical per-instance isolation joins the property-scoping this plan already enforces.
- **CK metrics + circular-dependency detection** (spec §4a: WMC/DIT/NOC/CBO/RFC/LCOM, Tarjan's SCC, god-class/dead-code/high-coupling threshold rules, `get_architecture_metrics`). Deliberately excluded from this first slice (the controller's scope names only call-graph + inheritance queries). They build on the same `traverse()`/`query()` output this slice establishes, as WaddleAI's own Python (never Neo4j GDS/APOC — portability, spec §3), and are not needed to prove the runnable/live-Neo4j slice. Next slice within Phase 1.
- **Multi-language structural edge extraction** (beyond the Python reference language of Task 10). Nodes + CONTAINS are already generic; EXTENDS/CALLS/REFERENCES are added grammar-by-grammar without touching the graph layer.
- **In-house graph-RAG** (spec §4b, Phase 2): entity/relationship LLM extraction (≥2B model), dual-level local/global retrieval, docs-content trigger. Entirely deferred; independently flag-gated (`waddleai.graph_rag`) so it never blocks this slice.

## Global Constraints Recap (per-task checklist)

Every task's "done" also means: `@dataclass(slots=True)` on new data types; `from __future__ import annotations`; PEP 257 docstrings; no `neo4j`/Cypher outside `shared/graph/drivers/`; scope predicates on every graph op; parameterized Cypher + label/edge allowlist; `waddleai.graph` fail-safe OFF; async throughout; `.venv/bin/pytest ... -q` green; and (before any merge) `make test-unit` still at ≥90% coverage.

## Self-Review (completed by plan author)

**Spec coverage:** §3 access layer → Tasks 2,4,5,8 (Protocol, driver, client); §3 exit-strategy/confinement → Global Constraints + Tasks 4/5 (single driver file); §2 resolver + `graph_instances` + dev-mode + non-ready→503 → Tasks 6,7 (provisioning automation explicitly deferred); §4a nodes/edges + incremental emission + file-delete → Tasks 9,10,11; §4a call-graph/inheritance via REST + MCP → Tasks 12,13; §6 flag + Enterprise gate + graceful degradation → Tasks 11,12,13; §5 licensing (neo4j pin, driver Apache-2.0) → Task 1 + confinement; §8 unit fake + property-scoping + isolation-proven + unavailable → Tasks 3,8,15,16,17; 90% coverage → AsyncMock-tested driver + `# pragma: no cover` on the one live line. CK-metrics/Tarjan (spec §4a/§7 Phase 1) consciously deferred to the next slice and documented above.

**Placeholder scan:** the only unresolved literal is the Neo4j image SHA256 digest (Task 14) — resolved by an exact, deterministic command in-step (`docker inspect ... RepoDigests`), not a logic gap. `neo4j==5.28.1` may float to the latest 5.x at build time (exact-pin preserved).

**Type consistency:** `TenantScope` fields, `node_key()`/`scope_props()`, `GraphStore`/`TenantGraphClient` method names, `GraphNodeDraft`/`GraphEdgeDraft`/`GraphFragment`, and `ResolvedInstance` are used identically across Tasks 2→17. `delete_scope(tenant, path=None)` is consistent everywhere (the documented collapse of spec §3's `repo_id, branch_ref` params into `TenantScope`).

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-09-01-graph-platform-phase1.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task (Tasks 1→17, in order — later tasks consume earlier interfaces), review between tasks, fast iteration. REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**2. Inline Execution** — execute tasks in this session using superpowers:executing-plans, batch execution with checkpoints for review.

Tasks 15–17 (plus the Task 14 harness) require the live-Neo4j container (`make graph-neo4j-up`); Tasks 1–13 are pure unit-tested and need no container.

**Which approach?**
