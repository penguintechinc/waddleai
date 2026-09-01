"""Tests for TenantGraphClient -- the tenant-isolation enforcement point.

Every scenario here targets one invariant: no method can issue an
un-scoped store call. Uses InMemoryGraphStore (Task 3's honest oracle,
which filters strictly on `tenant.scope_props()`) plus a fake resolver, so
cross-tenant leakage would show up as a real assertion failure, not a
mocked-away no-op.
"""

from __future__ import annotations

import pytest

from shared.graph.client import TenantGraphClient
from shared.graph.resolver import ResolvedInstance
from shared.graph.types import GraphQuery, GraphUnavailableError, TenantScope
from tests.unit.graph.fakes import InMemoryGraphStore

A = TenantScope(org_id="A", repo_id="1", branch_ref="main")
B = TenantScope(org_id="B", repo_id="1", branch_ref="main")


def _client(store: InMemoryGraphStore, *, ready: bool = True) -> TenantGraphClient:
    """Build a client wired to `store` via an injected resolver (no live Neo4j)."""

    async def resolver(db: object, org_id: str) -> ResolvedInstance:
        if not ready:
            raise GraphUnavailableError("not ready")
        return ResolvedInstance("bolt://x", "neo4j", "pw")

    return TenantGraphClient(db=object(), store_factory=lambda inst: store, resolver=resolver)


@pytest.mark.asyncio
async def test_shared_instance_is_isolated_by_scope_props() -> None:
    """A shared store (dev-mode's one Neo4j) still isolates org A from org B."""
    store = InMemoryGraphStore()
    ca, cb = _client(store), _client(store)
    await ca.upsert_node(A, "Class", "Foo", {"name": "Foo"})
    await cb.upsert_node(B, "Class", "Bar", {"name": "Bar"})
    recs = await ca.query(A, GraphQuery(labels=("Class",)))
    assert {r.properties["name"] for r in recs} == {"Foo"}


@pytest.mark.asyncio
async def test_query_where_is_merged_with_scope_props() -> None:
    """An empty/attacker-controlled `where` never overrides the caller's own scope."""
    store = InMemoryGraphStore()
    ca = _client(store)
    await ca.upsert_node(A, "Class", "Foo", {"name": "Foo"})
    await _client(store).upsert_node(B, "Class", "Foo", {"name": "OtherFoo"})
    recs = await ca.query(A, GraphQuery(labels=("Class",), where={}))
    assert recs
    assert all(r.properties["org_id"] == "A" for r in recs)


@pytest.mark.asyncio
async def test_upsert_stamps_key_and_scope_props() -> None:
    """upsert_node builds the key via scope.node_key() and stamps scope_props onto properties."""
    store = InMemoryGraphStore()
    await _client(store).upsert_node(A, "Class", "pkg.Foo", {"name": "Foo"})
    node = store._nodes["A:1:main:pkg.Foo"]  # noqa: SLF001 -- inspecting the fake's storage directly
    assert node.properties["org_id"] == "A"
    assert node.properties["repo_id"] == "1"
    assert node.properties["branch_ref"] == "main"


@pytest.mark.asyncio
async def test_upsert_edge_stamps_scope_props_and_uses_node_keys() -> None:
    """upsert_edge stamps scope onto edge properties and resolves endpoints via node_key()."""
    store = InMemoryGraphStore()
    c = _client(store)
    await c.upsert_node(A, "Function", "a", {})
    await c.upsert_node(A, "Function", "b", {})
    await c.upsert_edge(A, "CALLS", "a", "b", {})
    (edge,) = store._edges  # noqa: SLF001 -- inspecting the fake's storage directly
    assert edge.src_key == "A:1:main:a"
    assert edge.dst_key == "A:1:main:b"
    assert edge.properties["org_id"] == "A"


@pytest.mark.asyncio
async def test_call_graph_and_class_hierarchy_use_expected_edges() -> None:
    """call_graph walks CALLS; class_hierarchy walks EXTENDS/IMPLEMENTS -- both scoped."""
    store = InMemoryGraphStore()
    c = _client(store)
    await c.upsert_node(A, "Function", "a", {})
    await c.upsert_node(A, "Function", "b", {})
    await c.upsert_edge(A, "CALLS", "a", "b", {})
    paths = await c.call_graph(A, "a", direction="out", depth=3)
    assert any(p.edge_types == ("CALLS",) for p in paths)

    await c.upsert_node(A, "Class", "Base", {})
    await c.upsert_node(A, "Class", "Child", {})
    await c.upsert_edge(A, "EXTENDS", "Child", "Base", {})
    hier = await c.class_hierarchy(A, "Child", direction="out")
    assert any(p.edge_types == ("EXTENDS",) for p in hier)


@pytest.mark.asyncio
async def test_call_graph_does_not_cross_tenant() -> None:
    """A CALLS edge stamped for org B never appears in org A's call_graph traversal."""
    store = InMemoryGraphStore()
    ca, cb = _client(store), _client(store)
    await ca.upsert_node(A, "Function", "a", {})
    await ca.upsert_node(A, "Function", "b", {})
    await cb.upsert_node(B, "Function", "a", {})
    await cb.upsert_node(B, "Function", "b", {})
    await cb.upsert_edge(B, "CALLS", "a", "b", {})  # only org B's call edge exists
    paths = await ca.call_graph(A, "a", direction="out", depth=3)
    assert paths == []


@pytest.mark.asyncio
async def test_delete_scope_deletes_only_the_scoped_subset() -> None:
    """delete_scope removes only the caller's tenant subset, leaving other tenants intact."""
    store = InMemoryGraphStore()
    ca, cb = _client(store), _client(store)
    await ca.upsert_node(A, "Class", "Foo", {"name": "Foo"})
    await cb.upsert_node(B, "Class", "Bar", {"name": "Bar"})
    deleted = await ca.delete_scope(A)
    assert deleted == 1
    remaining = await cb.query(B, GraphQuery(labels=("Class",)))
    assert {r.properties["name"] for r in remaining} == {"Bar"}


@pytest.mark.asyncio
async def test_unavailable_instance_raises_not_hangs() -> None:
    """A resolver raising GraphUnavailableError propagates immediately from every method."""
    with pytest.raises(GraphUnavailableError):
        await _client(InMemoryGraphStore(), ready=False).query(A, GraphQuery())


@pytest.mark.asyncio
async def test_unavailable_instance_raises_from_upsert() -> None:
    """The unavailable-propagates guarantee holds for writes too, not just reads."""
    with pytest.raises(GraphUnavailableError):
        await _client(InMemoryGraphStore(), ready=False).upsert_node(A, "Class", "Foo", {})


@pytest.mark.asyncio
async def test_aclose_closes_cached_stores() -> None:
    """aclose() closes every store this client resolved and clears the cache."""

    class _TrackingStore(InMemoryGraphStore):
        closed: bool = False

        async def close(self) -> None:
            self.closed = True

    store = _TrackingStore()
    c = _client(store)
    await c.upsert_node(A, "Class", "Foo", {})
    await c.aclose()
    assert store.closed is True
