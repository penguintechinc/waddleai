"""GraphStore: penguincode's scope-aware graph driver (code/knowledge/memory graphs).

Three logical graphs (`graph_kind` = ``code`` | ``knowledge`` | ``memory``,
populated by the T11-T13 extractors) share one physical `graph_nodes` /
`graph_edges` schema in the shared WaddleAI Postgres (see
``db/migrations/0004_graph_nodes.sql`` / ``0005_graph_edges.sql``, T1). This
module is the only code path that reads or writes those tables -- every
other task (T11-T14) goes through `GraphStore`, never raw SQL, so the scope
filter below is enforced at one chokepoint instead of per callsite.

**Scope filter, re-applied at every traversal hop** (never cached from the
seed row): a row is visible under `ctx` iff
``row.tenant_id == ctx.tenant_id`` (hard boundary) AND (``visibility ==
'tenant'`` OR (``'team'`` AND ``row.team_id in ctx.team_ids``) OR (``'user'``
AND ``row.owner_user_id == ctx.user_id``)). `graph_edges.src_id`/`dst_id`
are plain FKs into `graph_nodes.id` with no CHECK/trigger tying an edge's own
`tenant_id`/`graph_kind` to its endpoints' -- so a corrupt or malicious edge
row *can* point across tenants at the SQL level. The recursive traversal
below re-applies the full scope predicate to the destination node at every
hop specifically to close that gap; it does not rely on the edge row's own
scope being trustworthy. See ``tests/test_stores_graph.py::TestScopeIsolation``
for the regression coverage of exactly this case.

**`neighbors`' `node_key` disambiguation:** `graph_nodes` is unique on
``(tenant_id, graph_kind, node_type, key)`` -- not `key` alone, so two
different node types (e.g. a `file` and a `symbol`) may share the same key.
`neighbors` accepts an optional keyword-only `node_type` to disambiguate the
seed; when omitted, the seed matches any node type sharing that key (a
producer that always mints keys unique within their own `node_type`, the
common case, never needs to pass it). This was chosen over renaming
`node_key` to a compound value because every other call site (`subgraph`'s
`seed_keys`, the Shared Contracts signature itself) uses a bare key string,
and threading a tuple through would ripple into every consumer for a case
that is rare in practice.

**Traversal direction:** `neighbors`/`subgraph` treat edges as undirected for
adjacency purposes (a directed ``a --imports--> b`` row makes each reachable
as a neighbor of the other) -- GraphRAG expansion (T14) wants both "what does
this reference" and "what references this", and `GraphEdge.rel_type` +
`props` already preserve the original direction/semantics for any consumer
that cares. Cycle safety is handled by tracking visited node ids per path in
the recursive CTE (a Postgres-recursive-CTE standard technique), not by
relying on the caller's `depth` bound alone.

`upsert_edges` auto-creates any endpoint node that doesn't already exist
(type+key, scope-stamped with the edge's own `visibility`/`team_id`) so a
producer can emit `[GraphEdge(...)]` without a prior `upsert_nodes` call --
see `PostgresGraphStore._resolve_or_create_node`. An edge upsert never
overwrites an existing endpoint's `props`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import GraphConfig
from penguincode_cli.observability.otel import timed_store_operation

VALID_GRAPH_KINDS: frozenset[str] = frozenset({"code", "knowledge", "memory"})
VALID_VISIBILITIES: frozenset[str] = frozenset({"user", "team", "tenant"})


@dataclass(slots=True, frozen=True)
class GraphNode:
    """One node in a logical graph, identified within its scope by (node_type, key)."""

    node_type: str
    key: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class GraphEdge:
    """One directed edge between two (node_type, key)-identified endpoints.

    `src_type`/`dst_type` are required (not optional) because `(node_type,
    key)` -- not `key` alone -- is the unique tuple per (tenant, graph_kind);
    see T1's `uq_graph_nodes_tenant_kind_type_key` constraint.
    """

    src_type: str
    src_key: str
    dst_type: str
    dst_key: str
    rel_type: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Subgraph:
    """Result of a `neighbors`/`subgraph` traversal: the reachable nodes plus
    every scope-visible edge connecting two of them (the induced subgraph)."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


@runtime_checkable
class GraphStore(Protocol):
    """Scope-aware graph driver interface -- see module docstring for the
    scope-filter and traversal-direction contract every implementation MUST honor."""

    def upsert_nodes(
        self,
        ctx: ScopeContext,
        kind: str,
        nodes: list[GraphNode],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None: ...

    def upsert_edges(
        self,
        ctx: ScopeContext,
        kind: str,
        edges: list[GraphEdge],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None: ...

    def neighbors(
        self,
        ctx: ScopeContext,
        kind: str,
        node_key: str,
        *,
        node_type: str | None = None,
        depth: int,
        rel_types: list[str] | None = None,
    ) -> Subgraph: ...

    def subgraph(
        self, ctx: ScopeContext, kind: str, seed_keys: list[str], *, depth: int
    ) -> Subgraph: ...

    def delete_by_scope(
        self, ctx: ScopeContext, kind: str, *, node_keys: list[str] | None = None
    ) -> None: ...


def _validate_kind(kind: str) -> None:
    if kind not in VALID_GRAPH_KINDS:
        raise ValueError(f"graph_kind must be one of {sorted(VALID_GRAPH_KINDS)}, got {kind!r}")


def _scope_columns(
    ctx: ScopeContext, visibility: str, team_id: str | None
) -> tuple[str | None, str | None, str | None]:
    """Resolve (org_id, team_id, owner_user_id) column values for a write.

    Enforces: `visibility` is one of the three recognized values; `'team'`
    requires a `team_id` that the caller actually belongs to (per `ctx.team_ids`)
    -- a caller cannot stamp data into a team it is not a member of.
    """
    if visibility not in VALID_VISIBILITIES:
        raise ValueError(
            f"visibility must be one of {sorted(VALID_VISIBILITIES)}, got {visibility!r}"
        )
    if visibility == "tenant":
        return ctx.org_id, None, None
    if visibility == "team":
        if not team_id:
            raise ValueError("visibility='team' requires a non-empty team_id")
        if team_id not in ctx.team_ids:
            raise ValueError(f"team_id {team_id!r} is not one of the caller's team_ids")
        return ctx.org_id, team_id, None
    # visibility == "user"
    return ctx.org_id, None, ctx.user_id


def _scope_predicate(alias: str) -> sql.Composed:
    """The read-filter SQL fragment for one table alias: tenant already
    filtered by the caller separately (it's an equality, not part of this
    OR-chain) -- this covers only the visibility/team/user branch.

    `alias` is always a hardcoded literal supplied by this module (a query
    alias like ``"n"``/``"e"`` or a bare table name), never external input;
    it is still composed via `sql.Identifier` (not f-string/`%`-interpolated
    into the query text) so the whole query stays built exclusively from
    `psycopg.sql` composition plus `%(name)s` bind parameters for every
    actual value.
    """
    return sql.SQL(
        "( {alias}.visibility = 'tenant' "
        "OR ({alias}.visibility = 'team' AND {alias}.team_id = ANY(%(team_ids)s)) "
        "OR ({alias}.visibility = 'user' AND {alias}.owner_user_id = %(user_id)s) )"
    ).format(alias=sql.Identifier(alias))


class PostgresGraphStore:
    """Default `GraphStore` backend: `graph_nodes`/`graph_edges` in the shared
    WaddleAI Postgres, traversed via scope-filtered recursive CTEs.

    Opens a fresh connection per call (no pooling) -- correct and simple for
    the extraction-batch and GraphRAG-expansion call patterns this backs
    today; a pool can be added later without changing this public interface.
    """

    def __init__(self, dsn: str, schema: str = "penguincode") -> None:
        self._dsn = dsn
        self._schema = schema

    def _table(self, name: str) -> sql.Identifier:
        return sql.Identifier(self._schema, name)

    def upsert_nodes(
        self,
        ctx: ScopeContext,
        kind: str,
        nodes: list[GraphNode],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None:
        """Scope-stamped upsert, keyed on (tenant_id, graph_kind, node_type, key).

        An existing node's `props` are replaced by the new value (an explicit
        node upsert is a producer stating "this is the current state"), unlike
        the no-op-preserving auto-create path in `upsert_edges`.
        """
        _validate_kind(kind)
        org_id, team_id_col, owner_user_id = _scope_columns(ctx, visibility, team_id)
        if not nodes:
            return

        query = sql.SQL(
            "INSERT INTO {nodes} "
            "(graph_kind, node_type, key, props, tenant_id, org_id, team_id, owner_user_id, visibility) "
            "VALUES (%(kind)s, %(node_type)s, %(key)s, %(props)s, %(tenant_id)s, %(org_id)s, "
            "%(team_id)s, %(owner_user_id)s, %(visibility)s) "
            "ON CONFLICT (tenant_id, graph_kind, node_type, key) DO UPDATE SET props = EXCLUDED.props"
        ).format(nodes=self._table("graph_nodes"))

        with timed_store_operation(
            "graph_query",
            "graph.upsert_nodes",
            backend="postgres",
            graph_kind=kind,
            count=len(nodes),
        ):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    for node in nodes:
                        cur.execute(
                            query,
                            {
                                "kind": kind,
                                "node_type": node.node_type,
                                "key": node.key,
                                "props": Jsonb(dict(node.props)),
                                "tenant_id": ctx.tenant_id,
                                "org_id": org_id,
                                "team_id": team_id_col,
                                "owner_user_id": owner_user_id,
                                "visibility": visibility,
                            },
                        )
                conn.commit()

    def _resolve_or_create_node(
        self,
        cur: psycopg.Cursor[Any],
        *,
        tenant_id: str,
        kind: str,
        node_type: str,
        key: str,
        org_id: str | None,
        team_id: str | None,
        owner_user_id: str | None,
        visibility: str,
    ) -> UUID:
        """Resolve (tenant_id, graph_kind, node_type, key) to its `graph_nodes.id`,
        creating an empty-props node scope-stamped from the edge's own scope if absent.

        A no-op `DO UPDATE` (rather than `DO NOTHING`) is required to make
        `RETURNING id` fire on both the create and already-exists paths in a
        single round trip; it deliberately never touches `props` so an
        existing node's real data is never clobbered by an edge upsert.
        """
        query = sql.SQL(
            "INSERT INTO {nodes} AS gn "
            "(graph_kind, node_type, key, props, tenant_id, org_id, team_id, owner_user_id, visibility) "
            "VALUES (%(kind)s, %(node_type)s, %(key)s, %(props)s, %(tenant_id)s, %(org_id)s, "
            "%(team_id)s, %(owner_user_id)s, %(visibility)s) "
            "ON CONFLICT (tenant_id, graph_kind, node_type, key) DO UPDATE SET props = gn.props "
            "RETURNING id"
        ).format(nodes=self._table("graph_nodes"))
        cur.execute(
            query,
            {
                "kind": kind,
                "node_type": node_type,
                "key": key,
                "props": Jsonb({}),
                "tenant_id": tenant_id,
                "org_id": org_id,
                "team_id": team_id,
                "owner_user_id": owner_user_id,
                "visibility": visibility,
            },
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields exactly one row here
        return row[0]  # type: ignore[no-any-return]

    def upsert_edges(
        self,
        ctx: ScopeContext,
        kind: str,
        edges: list[GraphEdge],
        *,
        visibility: str,
        team_id: str | None,
    ) -> None:
        """Resolve each endpoint (auto-creating if missing) then upsert the edge.

        Endpoints are resolved/created under the *edge's* scope
        (`visibility`/`team_id` args), not any pre-existing scope of their
        own -- if the node already exists, its own stored scope is left
        untouched (only `props` participates in the no-op update); only a
        newly-created node is stamped with the edge's scope.
        """
        _validate_kind(kind)
        org_id, team_id_col, owner_user_id = _scope_columns(ctx, visibility, team_id)
        if not edges:
            return

        edge_query = sql.SQL(
            "INSERT INTO {edges} "
            "(graph_kind, src_id, dst_id, rel_type, props, tenant_id, org_id, team_id, owner_user_id, visibility) "
            "VALUES (%(kind)s, %(src_id)s, %(dst_id)s, %(rel_type)s, %(props)s, %(tenant_id)s, "
            "%(org_id)s, %(team_id)s, %(owner_user_id)s, %(visibility)s) "
            "ON CONFLICT (tenant_id, graph_kind, src_id, dst_id, rel_type) "
            "DO UPDATE SET props = EXCLUDED.props"
        ).format(edges=self._table("graph_edges"))

        with timed_store_operation(
            "graph_query",
            "graph.upsert_edges",
            backend="postgres",
            graph_kind=kind,
            count=len(edges),
        ):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    for edge in edges:
                        src_id = self._resolve_or_create_node(
                            cur,
                            tenant_id=ctx.tenant_id,
                            kind=kind,
                            node_type=edge.src_type,
                            key=edge.src_key,
                            org_id=org_id,
                            team_id=team_id_col,
                            owner_user_id=owner_user_id,
                            visibility=visibility,
                        )
                        dst_id = self._resolve_or_create_node(
                            cur,
                            tenant_id=ctx.tenant_id,
                            kind=kind,
                            node_type=edge.dst_type,
                            key=edge.dst_key,
                            org_id=org_id,
                            team_id=team_id_col,
                            owner_user_id=owner_user_id,
                            visibility=visibility,
                        )
                        cur.execute(
                            edge_query,
                            {
                                "kind": kind,
                                "src_id": src_id,
                                "dst_id": dst_id,
                                "rel_type": edge.rel_type,
                                "props": Jsonb(dict(edge.props)),
                                "tenant_id": ctx.tenant_id,
                                "org_id": org_id,
                                "team_id": team_id_col,
                                "owner_user_id": owner_user_id,
                                "visibility": visibility,
                            },
                        )
                conn.commit()

    def _traverse(
        self,
        ctx: ScopeContext,
        kind: str,
        seed_keys: list[str],
        *,
        seed_node_type: str | None,
        depth: int,
        rel_types: list[str] | None,
    ) -> Subgraph:
        _validate_kind(kind)
        if depth < 0:
            raise ValueError(f"depth must be >= 0, got {depth}")

        query = sql.SQL(
            """
            WITH RECURSIVE reachable AS (
                SELECT n.id, n.node_type, n.key, n.props, 0 AS depth, ARRAY[n.id] AS visited
                FROM {nodes} n
                WHERE n.tenant_id = %(tenant_id)s
                  AND n.graph_kind = %(kind)s
                  AND n.key = ANY(%(seed_keys)s)
                  AND (%(seed_node_type)s::text IS NULL OR n.node_type = %(seed_node_type)s::text)
                  AND {node_scope}

                UNION ALL

                SELECT n2.id, n2.node_type, n2.key, n2.props, r.depth + 1, r.visited || n2.id
                FROM reachable r
                JOIN {edges} e
                  ON e.tenant_id = %(tenant_id)s
                 AND e.graph_kind = %(kind)s
                 AND (e.src_id = r.id OR e.dst_id = r.id)
                 AND (%(rel_types)s::text[] IS NULL OR e.rel_type = ANY(%(rel_types)s::text[]))
                 AND {edge_scope}
                JOIN {nodes} n2
                  ON n2.id = CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END
                 AND n2.tenant_id = %(tenant_id)s
                 AND n2.graph_kind = %(kind)s
                 AND {node2_scope}
                WHERE r.depth < %(depth)s
                  AND NOT (n2.id = ANY(r.visited))
            )
            SELECT DISTINCT id, node_type, key, props FROM reachable
            """
        ).format(
            nodes=self._table("graph_nodes"),
            edges=self._table("graph_edges"),
            node_scope=_scope_predicate("n"),
            edge_scope=_scope_predicate("e"),
            node2_scope=_scope_predicate("n2"),
        )

        params: dict[str, Any] = {
            "tenant_id": ctx.tenant_id,
            "kind": kind,
            "seed_keys": list(seed_keys),
            "seed_node_type": seed_node_type,
            "team_ids": list(ctx.team_ids),
            "user_id": ctx.user_id,
            "rel_types": list(rel_types) if rel_types else None,
            "depth": depth,
        }

        with timed_store_operation(
            "graph_query",
            "graph.traverse",
            backend="postgres",
            graph_kind=kind,
            depth=depth,
        ):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    node_rows = cur.fetchall()

                    id_to_node: dict[UUID, GraphNode] = {
                        node_id: GraphNode(node_type=node_type, key=key, props=props)
                        for node_id, node_type, key, props in node_rows
                    }

                    if not id_to_node:
                        return Subgraph(nodes=[], edges=[])

                    ids = list(id_to_node.keys())
                    edge_query = sql.SQL(
                        """
                        SELECT src_id, dst_id, rel_type, props FROM {edges}
                        WHERE tenant_id = %(tenant_id)s AND graph_kind = %(kind)s
                          AND src_id = ANY(%(ids)s) AND dst_id = ANY(%(ids)s)
                          AND (%(rel_types)s::text[] IS NULL OR rel_type = ANY(%(rel_types)s::text[]))
                          AND {scope}
                        """
                    ).format(
                        edges=self._table("graph_edges"), scope=_scope_predicate("graph_edges")
                    )
                    cur.execute(
                        edge_query,
                        {
                            "tenant_id": ctx.tenant_id,
                            "kind": kind,
                            "ids": ids,
                            "rel_types": list(rel_types) if rel_types else None,
                            "team_ids": list(ctx.team_ids),
                            "user_id": ctx.user_id,
                        },
                    )
                    edge_rows = cur.fetchall()

        edges: list[GraphEdge] = []
        for src_id, dst_id, rel_type, props in edge_rows:
            src_node = id_to_node[src_id]
            dst_node = id_to_node[dst_id]
            edges.append(
                GraphEdge(
                    src_type=src_node.node_type,
                    src_key=src_node.key,
                    dst_type=dst_node.node_type,
                    dst_key=dst_node.key,
                    rel_type=rel_type,
                    props=props,
                )
            )

        return Subgraph(nodes=list(id_to_node.values()), edges=edges)

    def neighbors(
        self,
        ctx: ScopeContext,
        kind: str,
        node_key: str,
        *,
        node_type: str | None = None,
        depth: int,
        rel_types: list[str] | None = None,
    ) -> Subgraph:
        """k-hop scoped expansion from one seed key (see module docstring for
        the `node_type` disambiguation and undirected-traversal decisions)."""
        return self._traverse(
            ctx, kind, [node_key], seed_node_type=node_type, depth=depth, rel_types=rel_types
        )

    def subgraph(
        self, ctx: ScopeContext, kind: str, seed_keys: list[str], *, depth: int
    ) -> Subgraph:
        """k-hop scoped expansion from multiple seed keys, merged into one result."""
        return self._traverse(
            ctx, kind, seed_keys, seed_node_type=None, depth=depth, rel_types=None
        )

    def delete_by_scope(
        self, ctx: ScopeContext, kind: str, *, node_keys: list[str] | None = None
    ) -> None:
        """Delete in-scope nodes (all of `kind` under `ctx`, or only `node_keys`).

        Edges are removed automatically via `ON DELETE CASCADE` on both
        `src_id`/`dst_id` (T1 schema) -- an edge cannot outlive either of its
        endpoint nodes. Only nodes visible under the standard read filter are
        deleted; a node scoped to a team/user outside `ctx` is left alone even
        when it matches `kind`/`node_keys`.
        """
        _validate_kind(kind)
        query = sql.SQL(
            """
            DELETE FROM {nodes}
            WHERE tenant_id = %(tenant_id)s AND graph_kind = %(kind)s
              AND (%(node_keys)s::text[] IS NULL OR key = ANY(%(node_keys)s::text[]))
              AND {scope}
            """
        ).format(nodes=self._table("graph_nodes"), scope=_scope_predicate("graph_nodes"))

        with timed_store_operation(
            "graph_query",
            "graph.delete_by_scope",
            backend="postgres",
            graph_kind=kind,
        ):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        query,
                        {
                            "tenant_id": ctx.tenant_id,
                            "kind": kind,
                            "node_keys": list(node_keys) if node_keys else None,
                            "team_ids": list(ctx.team_ids),
                            "user_id": ctx.user_id,
                        },
                    )
                conn.commit()


def create_graph_store(config: GraphConfig) -> GraphStore:
    """Select and construct a `GraphStore` from `config.backend`.

    `"postgres"` (default) returns `PostgresGraphStore`. `"kuzu"` is a
    recognized config value with no driver yet -- it raises
    `NotImplementedError` rather than silently falling back, per the
    platform plan's explicit escape-hatch design (Kùzu/LadybugDB, MIT
    licensed, stays available if the code graph outgrows recursive-CTE
    traversal; see spec section 6.2). Do not implement it here.
    """
    if config.backend == "postgres":
        return PostgresGraphStore(dsn=config.postgres.url, schema=config.postgres.schema)
    if config.backend == "kuzu":
        raise NotImplementedError(
            "the kuzu GraphStore backend is not implemented yet -- PostgresGraphStore's "
            "recursive-CTE traversal is the only driver today; see spec section 6.2"
        )
    raise ValueError(f"unknown graph backend: {config.backend!r}")
