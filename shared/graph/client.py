"""The tenant-guard over GraphStore -- no un-scoped Cypher/store call can be issued.

(spec Section 3)

This is the one seam every consumer (worker, REST, MCP -- Tasks 11/12/13)
goes through. It resolves the caller's `TenantScope.org_id` to a physical
instance per call (never accepts a pre-resolved connection from the
caller), builds every node key via `TenantScope.node_key()`, and stamps
`TenantScope.scope_props()` onto every write's properties and merges it
into every read/traverse/delete predicate. Both the in-memory fake and the
real Neo4j driver already enforce the tenant predicate internally (Tasks 3
and 4); the stamping/merging here is defense-in-depth at the boundary
where an org id first enters the graph platform, not a replacement for
that lower-layer enforcement -- and it is what makes a *write* carry its
own scope so a later read/delete can find it at all.

`org_id` always comes from the `scope` a caller passes in -- this class
never accepts a separate org argument that could diverge from `scope`, so
there is no parameter combination that lets a validated tenant scope be
overridden by an untrusted value. Building that `TenantScope` from the
validated JWT `tenant` claim (never request body/params) is the caller's
responsibility (Tasks 11/12/13), not this client's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Literal

from shared.graph.drivers.neo4j_driver import create_neo4j_store
from shared.graph.resolver import ResolvedInstance, resolve_or_dev
from shared.graph.store import GraphStore
from shared.graph.types import GraphPath, GraphQuery, GraphRecord, TenantScope

_Direction = Literal["out", "in", "both"]

# Loosely typed (Any, not the resolver's real `_SqlDB`/`int` params) so an
# injected test fake can use whatever db/org_id representation it wants --
# TenantScope.org_id is str, while the real resolver's org_id is int, and a
# test's org_id may be neither (e.g. a bare "A"/"B" fixture); the client
# passes `scope.org_id` straight through without coercion so it never
# crashes on a non-numeric id and never silently reinterprets the caller's
# tenant identity.
_ResolverFn = Callable[[Any, Any], Awaitable[ResolvedInstance]]
_StoreFactoryFn = Callable[[ResolvedInstance], GraphStore]


class TenantGraphClient:
    """Scope-guarded facade over a `GraphStore`; one physical instance per org.

    `store_factory`/`resolver` are injectable so unit tests can substitute
    `InMemoryGraphStore` and a fake resolver -- no live Neo4j required
    outside Tasks 14-17's integration tests.
    """

    def __init__(
        self,
        db: object,
        *,
        store_factory: _StoreFactoryFn | None = None,
        resolver: _ResolverFn = resolve_or_dev,
    ) -> None:
        """Bind to a penguin-dal-style db handle passed through to `resolver` unchanged."""
        self._db = db
        self._resolver = resolver
        self._store_factory: _StoreFactoryFn = store_factory or (
            lambda inst: create_neo4j_store(inst.bolt_url, inst.user, inst.password)
        )
        self._stores: dict[str, GraphStore] = {}

    async def _store(self, scope: TenantScope) -> GraphStore:
        """Resolve `scope.org_id` to its physical instance and return its cached `GraphStore`.

        Resolution runs fresh on every call using only `scope.org_id` -- no
        org id is ever accepted from anywhere else. A `GraphUnavailableError`
        raised by the resolver propagates immediately (no retry, no except
        clause here to swallow it, no hang waiting on a dead instance).
        """
        instance = await self._resolver(self._db, scope.org_id)
        store = self._stores.get(instance.bolt_url)
        if store is None:
            store = self._store_factory(instance)
            self._stores[instance.bolt_url] = store
        return store

    async def upsert_node(
        self, scope: TenantScope, label: str, qualified_name: str, props: dict[str, Any]
    ) -> None:
        """Create/update one node, keyed via `scope.node_key()` and stamped with the tenant scope.

        `scope.scope_props()` is merged in last, so a caller-supplied
        `props` dict can never override the org/repo/branch stamp.
        """
        store = await self._store(scope)
        stamped = {**props, "qualified_name": qualified_name, **scope.scope_props()}
        await store.upsert_node(scope, label, scope.node_key(qualified_name), stamped)

    async def upsert_edge(
        self,
        scope: TenantScope,
        edge_type: str,
        src_qn: str,
        dst_qn: str,
        props: dict[str, Any],
    ) -> None:
        """Create/update a directed edge between two qualified names, stamped with the tenant scope.

        Both endpoints are resolved through `scope.node_key()`, so an edge
        can never be written between a node key in one tenant's namespace
        and a bare/foreign key in another's.
        """
        store = await self._store(scope)
        stamped = {**props, **scope.scope_props()}
        await store.upsert_edge(
            scope, edge_type, scope.node_key(src_qn), scope.node_key(dst_qn), stamped
        )

    async def query(self, scope: TenantScope, query: GraphQuery) -> list[GraphRecord]:
        """Return nodes matching `query.labels`/`query.where`, with `scope` always winning.

        `scope.scope_props()` is merged into `query.where` last, so even an
        empty or attacker-influenced `where` can never widen the result
        past the caller's own tenant scope.
        """
        store = await self._store(scope)
        scoped_query = replace(query, where={**query.where, **scope.scope_props()})
        return await store.query(scope, scoped_query)

    async def traverse(
        self,
        scope: TenantScope,
        start_qn: str,
        edge_types: list[str],
        max_depth: int,
        direction: _Direction,
    ) -> list[GraphPath]:
        """Walk the graph from `start_qn`'s node key, scoped to the tenant throughout.

        The start node is addressed via `scope.node_key()`; the store
        implementation (Tasks 3/5) further restricts every hop to edges/
        nodes matching `scope.scope_props()`, so a path can never cross
        into another tenant's subgraph.
        """
        store = await self._store(scope)
        return await store.traverse(
            scope, scope.node_key(start_qn), edge_types, max_depth, direction
        )

    async def call_graph(
        self,
        scope: TenantScope,
        symbol: str,
        *,
        direction: _Direction = "out",
        depth: int = 3,
    ) -> list[GraphPath]:
        """Callers of / callees from `symbol` (spec Section 4a): a scoped traverse over CALLS."""
        return await self.traverse(scope, symbol, ["CALLS"], depth, direction)

    async def class_hierarchy(
        self,
        scope: TenantScope,
        symbol: str,
        *,
        direction: _Direction = "out",
        depth: int = 5,
    ) -> list[GraphPath]:
        """Inheritance chain for `symbol` (spec Section 4a).

        A scoped traverse over EXTENDS (single-inheritance) and IMPLEMENTS (interfaces).
        """
        return await self.traverse(scope, symbol, ["EXTENDS", "IMPLEMENTS"], depth, direction)

    async def delete_scope(self, scope: TenantScope, path: str | None = None) -> int:
        """Delete every node/edge in `scope`, optionally narrowed to one file's `path`.

        Delegates entirely to the store's own scoped delete (Tasks 3/5) --
        `scope` is required, so there is no call shape that deletes without
        a tenant predicate.
        """
        store = await self._store(scope)
        return await store.delete_scope(scope, path=path)

    async def aclose(self) -> None:
        """Close every `GraphStore` this client has resolved and drop the cache."""
        for store in self._stores.values():
            await store.close()
        self._stores.clear()
