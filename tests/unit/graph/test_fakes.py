"""Contract tests for `InMemoryGraphStore`, the unit-test GraphStore oracle.

These prove the fake is an *honest* oracle: it filters every read by the
exact property predicates it is given (including org_id/repo_id/branch_ref),
never a pass-through that returns all rows regardless of scope. Tasks 8/11
build cross-tenant isolation tests on top of this fake, so a fake that
silently ignored scope would make every one of those tests pass for the
wrong reason (spec Section 3, graph platform Phase 1 plan).
"""

from __future__ import annotations

from shared.graph.types import GraphQuery, TenantScope
from tests.unit.graph.fakes import InMemoryGraphStore


async def test_query_filters_by_scope_props() -> None:
    """query() scoped to org A's props must never surface org B's node."""
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props(), "name": "Foo"})
    await store.upsert_node(b, "Class", b.node_key("Bar"), {**b.scope_props(), "name": "Bar"})

    recs = await store.query(a, GraphQuery(labels=("Class",), where=a.scope_props()))

    names = {r.properties["name"] for r in recs}
    assert names == {"Foo"}


async def test_query_tenant_arg_cannot_be_overridden_by_where() -> None:
    """The `tenant` argument is authoritative.

    A `where` claiming another org's scope must not leak that org's data
    back to the caller.
    """
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props(), "name": "Foo"})
    await store.upsert_node(b, "Class", b.node_key("Bar"), {**b.scope_props(), "name": "Bar"})

    # Caller authenticated as tenant `a` but supplies `where` claiming org B's
    # scope -- the store must still resolve to tenant `a`'s data only.
    recs = await store.query(a, GraphQuery(labels=("Class",), where=b.scope_props()))

    names = {r.properties["name"] for r in recs}
    assert names == {"Foo"}


async def test_query_returns_nothing_for_unrelated_org() -> None:
    """Querying with org B's own scope must return zero rows for org A's data."""
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props(), "name": "Foo"})

    recs = await store.query(b, GraphQuery(labels=("Class",), where=b.scope_props()))

    assert recs == []


async def test_traverse_excludes_edges_outside_tenant_scope() -> None:
    """traverse() only walks edges whose own properties match the tenant's scope.

    Matching key namespace alone is not sufficient, so an edge smuggled in
    under another tenant's scope stamp never appears in the walk even when
    its node keys look like they belong to the caller's org.
    """
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    start = a.node_key("Foo")
    in_scope = a.node_key("Bar")
    leaked = a.node_key("Leaked")
    await store.upsert_node(a, "Class", start, {**a.scope_props(), "name": "Foo"})
    await store.upsert_node(a, "Class", in_scope, {**a.scope_props(), "name": "Bar"})
    await store.upsert_node(a, "Class", leaked, {**a.scope_props(), "name": "Leaked"})
    # Legitimate edge, scoped to A.
    await store.upsert_edge(a, "CALLS", start, in_scope, {**a.scope_props()})
    # Adversarial edge: node keys sit under A's key namespace, but the edge's
    # own properties are stamped for tenant B.
    await store.upsert_edge(b, "CALLS", start, leaked, {**b.scope_props()})

    paths = await store.traverse(a, start, ["CALLS"], max_depth=1, direction="out")

    reached = {key for p in paths for key in p.node_keys}
    assert in_scope in reached
    assert leaked not in reached


async def test_delete_scope_is_property_scoped() -> None:
    """delete_scope() removes only the scoped org's nodes, leaving others intact."""
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    b = TenantScope(org_id="B", repo_id="1", branch_ref="main")
    await store.upsert_node(a, "Class", a.node_key("Foo"), {**a.scope_props()})
    await store.upsert_node(b, "Class", b.node_key("Bar"), {**b.scope_props()})

    removed = await store.delete_scope(a)

    assert removed == 1
    remaining = await store.query(b, GraphQuery(labels=("Class",), where=b.scope_props()))
    assert len(remaining) == 1


async def test_delete_scope_honors_optional_path_filter() -> None:
    """delete_scope() with a `path` only removes nodes under that path, in-scope."""
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    await store.upsert_node(
        a, "Module", a.node_key("pkg.mod_a"), {**a.scope_props(), "path": "pkg/mod_a.py"}
    )
    await store.upsert_node(
        a, "Module", a.node_key("pkg.mod_b"), {**a.scope_props(), "path": "pkg/mod_b.py"}
    )

    removed = await store.delete_scope(a, path="pkg/mod_a.py")

    assert removed == 1
    remaining = await store.query(a, GraphQuery(labels=("Module",), where=a.scope_props()))
    assert [r.properties["path"] for r in remaining] == ["pkg/mod_b.py"]


async def test_upsert_node_is_idempotent_on_key() -> None:
    """A second upsert_node() with the same key replaces, not duplicates."""
    store = InMemoryGraphStore()
    a = TenantScope(org_id="A", repo_id="1", branch_ref="main")
    key = a.node_key("Foo")
    await store.upsert_node(a, "Class", key, {**a.scope_props(), "name": "Foo", "v": 1})
    await store.upsert_node(a, "Class", key, {**a.scope_props(), "name": "Foo", "v": 2})

    recs = await store.query(a, GraphQuery(labels=("Class",), where=a.scope_props()))

    assert len(recs) == 1
    assert recs[0].properties["v"] == 2


async def test_close_is_a_noop() -> None:
    """close() exists to satisfy the Protocol and completes without error."""
    store = InMemoryGraphStore()
    await store.close()
