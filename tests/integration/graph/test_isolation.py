"""Live-Neo4j proof of the property-scoping tenant isolation invariant (spec Section 8a).

In Phase-1 dev-mode every org resolves to the ONE shared Neo4j instance
(``shared/graph/resolver.py``'s dev-mode short-circuit, mirrored by this
harness's ``graph_client`` fixture -- see ``tests/integration/graph/conftest.py``).
Property-scoping -- ``org_id``/``repo_id``/``branch_ref`` stamped onto every
node/edge and merged into every query/traverse/delete predicate
(``shared/graph/client.py``'s ``TenantGraphClient``, ``shared/graph/drivers/
neo4j_driver.py``'s ``compile_*`` functions) -- is therefore the ONLY thing
that keeps one org's code graph invisible to another's. This module proves
that boundary against a REAL Neo4j round trip, not a mock: two distinct
``TenantScope``s are written into the same physical database, then every
read surface (``query``, ``traverse``, ``call_graph``, ``class_hierarchy``)
is asserted to return org A's data for org A and NEVER org B's -- and vice
versa -- plus that ``delete_scope`` for one org leaves the other untouched.

A real cross-tenant leak (e.g. a missing ``scope_props()`` merge, a dropped
``WHERE`` clause in a ``compile_*`` function) would surface here as an
``assert names == {"a-secret"}``-style failure showing the *other* org's
data in the result set -- this is intentionally a hard failure, not a
warning, since it is the platform's core tenant-isolation boundary.

Traverse-family caveat (fix round 1): ``TenantScope.node_key()`` embeds
``org_id`` into every composite node key, so a traverse that merely starts
on the *wrong* org's qualified name fails the start-node MATCH before
``compile_traverse``'s own tenant predicate is ever exercised -- two orgs
each building their own same-named, same-shaped chain therefore proves
nothing about that predicate on its own (a reviewer confirmed this
empirically by gutting the predicate and watching the naive version of
these tests stay green). The ``traverse``/``call_graph``/``class_hierarchy``
tests below additionally plant a raw cross-org edge directly through the
Neo4j driver -- something ``TenantGraphClient.upsert_edge`` itself refuses
to do -- from org B's own legitimately-scoped node into org A's node, so
the "does not leak" assertion depends on ``compile_traverse``'s end-node
predicate, not on key-namespacing. See the mutation proof in
``task-15-report.md``.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from neo4j import AsyncGraphDatabase

from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphQuery, TenantScope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.asyncio(loop_scope="session"),
]

_BOLT = os.getenv("WADDLEAI_GRAPH_BOLT_URL", "")
_USER = os.getenv("WADDLEAI_GRAPH_USER", "neo4j")
_PASSWORD = os.getenv("WADDLEAI_GRAPH_PASSWORD", "")


async def _plant_raw_cross_org_edge(src_key: str, dst_key: str, edge_type: str) -> None:
    """Write a directed edge straight through the Neo4j driver, bypassing every guard.

    ``TenantGraphClient.upsert_edge``/``compile_upsert_edge`` refuse to
    create an edge whose destination node fails the caller's own tenant
    predicate (``shared/graph/drivers/neo4j_driver.py``), so a genuine
    cross-org edge can never be produced through the client's public write
    path -- this helper corrupts the graph directly, the way a driver bug
    or a direct-Cypher operator mistake might, so a test using it depends
    on ``compile_traverse``'s own WHERE clause to stay isolated, not on
    the write-path guard or on key-namespacing.
    """
    driver = AsyncGraphDatabase.driver(_BOLT, auth=(_USER, _PASSWORD), connection_timeout=5.0)
    try:
        async with driver.session() as session:
            await session.run(
                f"MATCH (s {{key: $src}}), (d {{key: $dst}}) MERGE (s)-[r:{edge_type}]->(d)",
                src=src_key,
                dst=dst_key,
            )
    finally:
        await driver.close()


async def test_query_returns_only_the_caller_orgs_nodes(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """Two orgs write same-labeled nodes into the shared instance; each `query` sees only its own.

    Positive (A sees A) and negative (A never sees B, B never sees A) in
    one round trip -- the core spec Section 8a assertion.
    """
    org_a = unique_scope("main")
    org_b = unique_scope("main")
    try:
        await graph_client.upsert_node(org_a, "Class", "Secret", {"name": "a-secret"})
        await graph_client.upsert_node(org_b, "Class", "Secret", {"name": "b-secret"})

        a_recs = await graph_client.query(org_a, GraphQuery(labels=("Class",)))
        b_recs = await graph_client.query(org_b, GraphQuery(labels=("Class",)))

        a_names = {r.properties.get("name") for r in a_recs}
        b_names = {r.properties.get("name") for r in b_recs}

        assert a_names == {"a-secret"}, f"org A leaked another org's data: {a_names}"
        assert b_names == {"b-secret"}, f"org B leaked another org's data: {b_names}"
    finally:
        await graph_client.delete_scope(org_a)
        await graph_client.delete_scope(org_b)


async def test_traverse_never_crosses_tenant_boundary(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """A CALLS chain in org A stays invisible to org B, including via a planted cross-org edge.

    Key-namespacing alone (``TenantScope.node_key()`` embeds ``org_id``)
    already guarantees a traverse can't even *start* on another org's
    qualified name -- necessary, but it proves nothing about
    ``compile_traverse``'s own end-node tenant predicate. The final
    assertion plants a raw edge directly from org B's own (legitimately
    scoped) node into org A's node -- something
    ``TenantGraphClient.upsert_edge`` itself refuses to do -- so it depends
    on that predicate, not on key-namespacing, to pass.
    """
    org_a = unique_scope("main")
    org_b = unique_scope("main")
    try:
        await graph_client.upsert_node(org_a, "Function", "caller", {})
        await graph_client.upsert_node(org_a, "Function", "callee", {})
        await graph_client.upsert_edge(org_a, "CALLS", "caller", "callee", {})

        a_paths = await graph_client.traverse(org_a, "caller", ["CALLS"], 3, "out")
        assert len(a_paths) == 1
        assert a_paths[0].node_keys == (
            org_a.node_key("caller"),
            org_a.node_key("callee"),
        )

        # org B has no node at this qualified name at all, so the start-node
        # MATCH itself must miss -- no path, not an error.
        b_paths = await graph_client.traverse(org_b, "caller", ["CALLS"], 3, "out")
        assert b_paths == []

        # Harder case: org B's OWN node, with a raw cross-org edge planted
        # straight into org A's node -- bypassing the client's write-path
        # guard entirely. Without compile_traverse's end-node predicate this
        # WOULD surface org A's node in a B-scoped traversal.
        await graph_client.upsert_node(org_b, "Function", "b-caller", {})
        await _plant_raw_cross_org_edge(
            org_b.node_key("b-caller"), org_a.node_key("callee"), "CALLS"
        )
        leaked_paths = await graph_client.traverse(org_b, "b-caller", ["CALLS"], 3, "out")
        leaked_keys = {key for path in leaked_paths for key in path.node_keys}
        assert org_a.node_key("callee") not in leaked_keys, (
            f"traverse leaked org A's node into a B-scoped result: {leaked_keys}"
        )
    finally:
        await graph_client.delete_scope(org_a)
        await graph_client.delete_scope(org_b)


async def test_call_graph_and_class_hierarchy_are_tenant_scoped(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """`call_graph`/`class_hierarchy` inherit `traverse`'s scoping -- proven via a planted edge.

    Both helpers are thin wrappers around ``TenantGraphClient.traverse``
    (``shared/graph/client.py``), so the same key-namespacing caveat from
    ``test_traverse_never_crosses_tenant_boundary`` applies: two orgs each
    building their own same-named CALLS/EXTENDS chain proves nothing about
    the tenant predicate by itself. Each block below plants a raw
    cross-org edge from org B's own node into org A's node before calling
    `call_graph`/`class_hierarchy` from org B, so "does not leak" depends
    on `compile_traverse`'s WHERE clause.
    """
    org_a = unique_scope("main")
    org_b = unique_scope("main")
    try:
        await graph_client.upsert_node(org_a, "Function", "outer", {})
        await graph_client.upsert_node(org_a, "Function", "inner", {})
        await graph_client.upsert_edge(org_a, "CALLS", "outer", "inner", {})
        await graph_client.upsert_node(org_a, "Class", "Base", {})
        await graph_client.upsert_node(org_a, "Class", "Derived", {})
        await graph_client.upsert_edge(org_a, "EXTENDS", "Derived", "Base", {})

        a_calls = await graph_client.call_graph(org_a, "outer", direction="out", depth=3)
        assert len(a_calls) == 1
        assert a_calls[0].node_keys == (org_a.node_key("outer"), org_a.node_key("inner"))

        a_hierarchy = await graph_client.class_hierarchy(org_a, "Derived", direction="out", depth=3)
        assert len(a_hierarchy) == 1
        assert a_hierarchy[0].node_keys == (org_a.node_key("Derived"), org_a.node_key("Base"))

        # Planted cross-org edges: org B's own nodes CALL/EXTEND straight
        # into org A's "inner"/"Base" nodes, bypassing the client's
        # write-path guard.
        await graph_client.upsert_node(org_b, "Function", "b-outer", {})
        await _plant_raw_cross_org_edge(org_b.node_key("b-outer"), org_a.node_key("inner"), "CALLS")
        await graph_client.upsert_node(org_b, "Class", "BDerived", {})
        await _plant_raw_cross_org_edge(
            org_b.node_key("BDerived"), org_a.node_key("Base"), "EXTENDS"
        )

        b_calls = await graph_client.call_graph(org_b, "b-outer", direction="out", depth=3)
        b_call_keys = {key for path in b_calls for key in path.node_keys}
        assert org_a.node_key("inner") not in b_call_keys, (
            f"call_graph leaked org A's node into a B-scoped result: {b_call_keys}"
        )

        b_hierarchy = await graph_client.class_hierarchy(
            org_b, "BDerived", direction="out", depth=3
        )
        b_hierarchy_keys = {key for path in b_hierarchy for key in path.node_keys}
        assert org_a.node_key("Base") not in b_hierarchy_keys, (
            f"class_hierarchy leaked org A's node into a B-scoped result: {b_hierarchy_keys}"
        )
    finally:
        await graph_client.delete_scope(org_a)
        await graph_client.delete_scope(org_b)


async def test_delete_scope_only_removes_the_targeted_org(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """`delete_scope` for org A must not remove a single node/edge belonging to org B."""
    org_a = unique_scope("main")
    org_b = unique_scope("main")
    try:
        await graph_client.upsert_node(org_a, "Class", "Ka", {})
        await graph_client.upsert_node(org_b, "Class", "Kb", {})

        deleted = await graph_client.delete_scope(org_a)
        assert deleted >= 1

        a_recs_after = await graph_client.query(org_a, GraphQuery(labels=("Class",)))
        assert a_recs_after == []

        b_recs_after = await graph_client.query(org_b, GraphQuery(labels=("Class",)))
        b_names_after = {r.properties.get("name") for r in b_recs_after}
        assert len(b_recs_after) == 1, f"org A's delete_scope touched org B: {b_names_after}"
    finally:
        await graph_client.delete_scope(org_a)
        await graph_client.delete_scope(org_b)
