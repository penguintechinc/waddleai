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
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphQuery, TenantScope

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.asyncio(loop_scope="session"),
]


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
    """A CALLS chain built entirely within org A is invisible to an org-B-scoped traverse.

    Also proves the positive case: org A's own traverse walks its own chain.
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
    finally:
        await graph_client.delete_scope(org_a)
        await graph_client.delete_scope(org_b)


async def test_call_graph_and_class_hierarchy_are_tenant_scoped(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """The higher-level `call_graph`/`class_hierarchy` helpers inherit `traverse`'s scoping.

    Builds identical-shaped CALLS and EXTENDS graphs under two different
    orgs sharing the same qualified names, then proves org A's helpers
    return only org A's paths (by node key), never org B's.
    """
    org_a = unique_scope("main")
    org_b = unique_scope("main")
    try:
        for scope in (org_a, org_b):
            await graph_client.upsert_node(scope, "Function", "outer", {})
            await graph_client.upsert_node(scope, "Function", "inner", {})
            await graph_client.upsert_edge(scope, "CALLS", "outer", "inner", {})
            await graph_client.upsert_node(scope, "Class", "Base", {})
            await graph_client.upsert_node(scope, "Class", "Derived", {})
            await graph_client.upsert_edge(scope, "EXTENDS", "Derived", "Base", {})

        a_calls = await graph_client.call_graph(org_a, "outer", direction="out", depth=3)
        a_hierarchy = await graph_client.class_hierarchy(org_a, "Derived", direction="out", depth=3)

        assert len(a_calls) == 1
        assert a_calls[0].node_keys == (org_a.node_key("outer"), org_a.node_key("inner"))
        for path in a_calls:
            for key in path.node_keys:
                assert key.startswith(f"{org_a.org_id}:"), f"call_graph leaked a foreign key: {key}"

        assert len(a_hierarchy) == 1
        assert a_hierarchy[0].node_keys == (org_a.node_key("Derived"), org_a.node_key("Base"))
        for path in a_hierarchy:
            for key in path.node_keys:
                assert key.startswith(f"{org_a.org_id}:"), (
                    f"class_hierarchy leaked a foreign key: {key}"
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
