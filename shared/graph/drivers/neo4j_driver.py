"""The Neo4j GraphStore driver: Cypher compilation plus the async execution shell.

Module-level ``compile_*`` functions (Task 4) are the ONLY place Cypher
strings are built for the graph platform (spec's vendor-abstraction
requirement) -- pure, dependency-free functions turning the vendor-neutral
graph types (Task 2) into ``(cypher, params)`` pairs. Every value is passed
as a ``$``-param, never string-interpolated (Cypher-injection safe). Labels,
relationship types, and dynamic property keys are the things Cypher cannot
parameterize, so they are validated -- labels/edge types against the
``_NODE_LABELS``/``_EDGE_TYPES`` allowlists, property keys against a safe
identifier shape -- and rejected with ``GraphScopeError`` rather than ever
interpolated unchecked.
The tenant scope predicate is unconditional and always wins over a
caller-supplied ``where``, mirroring the in-memory fake's
``{**query.where, **tenant.scope_props()}`` merge order
(``tests/unit/graph/fakes.py``) so isolation holds against both backends.

``Neo4jGraphStore`` (Task 5) is the async I/O shell that runs those compiled
pairs against an injected ``neo4j`` async driver and maps result rows to the
Task-2 typed values; ``create_neo4j_store`` builds the real driver. This
module is the ONLY place the ``neo4j`` package is imported and an async
session/transaction is opened -- every other consumer depends on the
``GraphStore`` Protocol, never on this module or the ``neo4j`` package.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from neo4j import AsyncGraphDatabase

from shared.graph.types import (
    _EDGE_TYPES,
    _NODE_LABELS,
    GraphPath,
    GraphQuery,
    GraphRecord,
    GraphScopeError,
    TenantScope,
)

if TYPE_CHECKING:
    from neo4j import AsyncDriver

_DIR_ARROWS: dict[str, tuple[str, str]] = {
    "out": ("-", "->"),
    "in": ("<-", "-"),
    "both": ("-", "-"),
}

_SAFE_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def _check_label(label: str) -> str:
    """Validate a node label against the fixed allowlist before it is interpolated.

    Labels cannot be Cypher ``$``-params, so this is the sole guard against
    a query built with an attacker-controlled label string.
    """
    if label not in _NODE_LABELS:
        raise GraphScopeError(f"label not in allowlist: {label!r}")
    return label


def _check_edge(edge_type: str) -> str:
    """Validate a relationship type against the fixed allowlist before it is interpolated."""
    if edge_type not in _EDGE_TYPES:
        raise GraphScopeError(f"edge_type not in allowlist: {edge_type!r}")
    return edge_type


def _check_property_key(key: str) -> str:
    """Validate a `GraphQuery.where` property key before it is interpolated.

    Property names, like labels and relationship types, cannot be Cypher
    ``$``-params -- only a safe identifier shape (letters/digits/underscore,
    not leading with a digit) is allowed, so a caller-controlled key can
    never break out of the `n.<key> = $wN` fragment.
    """
    if not _SAFE_PROPERTY_KEY.match(key):
        raise GraphScopeError(f"unsafe property key: {key!r}")
    return key


def _scope_where(alias: str, scope: TenantScope) -> tuple[str, dict[str, Any]]:
    """Build the mandatory tenant predicate for one node alias.

    Always includes ``org_id``/``repo_id``; ``branch_ref`` only when set --
    mirroring ``TenantScope.scope_props()`` so a compiled query and the
    in-memory fake enforce the identical isolation boundary.
    """
    clauses = [f"{alias}.org_id = $org_id", f"{alias}.repo_id = $repo_id"]
    params: dict[str, Any] = {"org_id": scope.org_id, "repo_id": scope.repo_id}
    if scope.branch_ref is not None:
        clauses.append(f"{alias}.branch_ref = $branch_ref")
        params["branch_ref"] = scope.branch_ref
    return " AND ".join(clauses), params


def compile_upsert_node(
    scope: TenantScope, label: str, key: str, properties: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Compile a MERGE that creates or updates one node, stamped with the tenant scope.

    The node key and every property value are ``$``-params; only the
    allowlisted ``label`` is interpolated into the pattern.
    """
    _check_label(label)
    props = {**properties, **scope.scope_props()}
    cypher = f"MERGE (n:{label} {{key: $key}}) SET n += $props"
    return cypher, {"key": key, "props": props}


def compile_upsert_edge(
    scope: TenantScope, edge_type: str, src_key: str, dst_key: str, properties: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Compile a MERGE for a directed, allowlisted edge, requiring both endpoints in-scope.

    ``src_key``/``dst_key`` and every property value are ``$``-params; only
    the allowlisted ``edge_type`` is interpolated. This module is the
    designated query-layer enforcement point, so it never trusts the
    caller to have derived ``dst_key`` safely: both the source and
    destination nodes must match the caller's tenant scope (reusing the
    same ``$org_id``/``$repo_id``/``$branch_ref`` params for both), so a
    cross-tenant ``dst_key`` fails the MATCH and no edge is created
    (fail-safe -- never a cross-tenant write).
    """
    _check_edge(edge_type)
    where_source, scope_params = _scope_where("s", scope)
    where_dest, _ = _scope_where("d", scope)
    props = {**properties, **scope.scope_props()}
    cypher = (
        "MATCH (s {key: $src_key}), (d {key: $dst_key}) "
        f"WHERE {where_source} AND {where_dest} "
        f"MERGE (s)-[r:{edge_type}]->(d) "
        "SET r += $props"
    )
    params = {"src_key": src_key, "dst_key": dst_key, "props": props, **scope_params}
    return cypher, params


def compile_query(scope: TenantScope, query: GraphQuery) -> tuple[str, dict[str, Any]]:
    """Compile a scoped node MATCH; the tenant predicate always wins over ``query.where``.

    Any ``org_id``/``repo_id``/``branch_ref`` in ``query.where`` is dropped
    before building the predicate -- the compiled query can only ever
    filter on *this* tenant's scope, matching the in-memory fake's
    ``{**query.where, **tenant.scope_props()}`` merge (tenant spread last,
    so it always wins).
    """
    label_fragment = "".join(f":{_check_label(label)}" for label in query.labels)
    where_clause, params = _scope_where("n", scope)
    extra = {
        key: value
        for key, value in query.where.items()
        if key not in ("org_id", "repo_id", "branch_ref")
    }
    for index, (key, value) in enumerate(extra.items()):
        where_clause += f" AND n.{_check_property_key(key)} = $w{index}"
        params[f"w{index}"] = value
    limit_clause = f" LIMIT {int(query.limit)}" if query.limit is not None else ""
    cypher = (
        f"MATCH (n{label_fragment}) WHERE {where_clause} "
        f"RETURN n.key AS key, labels(n) AS labels, properties(n) AS props{limit_clause}"
    )
    return cypher, params


def compile_traverse(
    scope: TenantScope,
    start_key: str,
    edge_types: list[str],
    max_depth: int,
    direction: Literal["out", "in", "both"],
) -> tuple[str, dict[str, Any]]:
    """Compile a bounded variable-length traversal, scoping both endpoints.

    Every hop type must be in the edge allowlist; the start node and every
    reachable end node are constrained to the caller's tenant scope, so a
    traversal can never step onto -- or return -- another tenant's node.
    """
    rel_fragment = "|".join(_check_edge(edge_type) for edge_type in edge_types)
    left_arrow, right_arrow = _DIR_ARROWS[direction]
    where_start, params = _scope_where("start", scope)
    params["start_key"] = start_key
    end_clauses = ["end.org_id = $org_id", "end.repo_id = $repo_id"]
    if scope.branch_ref is not None:
        end_clauses.append("end.branch_ref = $branch_ref")
    cypher = (
        f"MATCH (start {{key: $start_key}}) WHERE {where_start} "
        f"MATCH p = (start){left_arrow}[:{rel_fragment}*1..{int(max_depth)}]{right_arrow}(end) "
        f"WHERE {' AND '.join(end_clauses)} "
        "RETURN [n IN nodes(p) | n.key] AS node_keys, "
        "[r IN relationships(p) | type(r)] AS edge_types"
    )
    return cypher, params


def compile_delete_scope(scope: TenantScope, path: str | None) -> tuple[str, dict[str, Any]]:
    """Compile a scoped DETACH DELETE, optionally narrowed to one file's nodes.

    ``path`` is always a ``$``-param; omitting it (``None``) deletes every
    node in the tenant scope, matching the in-memory fake's ``path is
    None`` shortcut.
    """
    where_clause, params = _scope_where("n", scope)
    if path is not None:
        where_clause += " AND n.path = $path"
        params["path"] = path
    cypher = f"MATCH (n) WHERE {where_clause} DETACH DELETE n RETURN count(n) AS deleted"
    return cypher, params


def _row_label(row: dict[str, Any]) -> str:
    """Return the row's first Cypher label, or '' when `labels(n)` came back empty."""
    labels = row.get("labels") or []
    return str(labels[0]) if labels else ""


class Neo4jGraphStore:
    """`GraphStore` implementation running the Task-4 `compile_*` Cypher over a real driver.

    This class and `create_neo4j_store` are the ONLY place the `neo4j`
    package is imported and an async session/transaction is opened for the
    graph platform (spec's vendor-abstraction requirement) -- every other
    consumer depends on the `GraphStore` Protocol (Task 2), never on this
    module. It builds no Cypher itself: each method's entire query comes
    from one `compile_*` call above, and the returned `params` dict is
    passed straight through to `session.run()` as keyword arguments, never
    string-interpolated.

    The async driver is injected rather than constructed here, so a fake
    driver/session can stand in for unit tests without a live Neo4j server;
    `create_neo4j_store` is the only place a real driver gets built.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        """Bind to an already-constructed async driver (injectable for tests)."""
        self._driver = driver

    async def _run(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Open a session, run one compiled `(cypher, params)` pair, and return rows as dicts.

        `params` is always passed as `session.run(cypher, **params)` keyword
        arguments -- the Neo4j driver's own parameterization, never string
        formatting -- so this method cannot introduce an injection path even
        if a `compile_*` function above ever did.
        """
        async with self._driver.session() as session:
            result = await session.run(cypher, **params)
            return await result.data()

    async def upsert_node(
        self, tenant: TenantScope, label: str, key: str, properties: dict[str, Any]
    ) -> None:
        """Create or update a single node, scoped to the tenant."""
        await self._run(*compile_upsert_node(tenant, label, key, properties))

    async def upsert_edge(
        self,
        tenant: TenantScope,
        edge_type: str,
        src_key: str,
        dst_key: str,
        properties: dict[str, Any],
    ) -> None:
        """Create or update a directed edge between two existing node keys."""
        await self._run(*compile_upsert_edge(tenant, edge_type, src_key, dst_key, properties))

    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]:
        """Return the nodes matching the given label/property predicates."""
        rows = await self._run(*compile_query(tenant, query))
        return [
            GraphRecord(key=row["key"], label=_row_label(row), properties=row["props"])
            for row in rows
        ]

    async def traverse(
        self,
        tenant: TenantScope,
        start_key: str,
        edge_types: list[str],
        max_depth: int,
        direction: Literal["out", "in", "both"],
    ) -> list[GraphPath]:
        """Walk the graph from `start_key` up to `max_depth` hops."""
        compiled = compile_traverse(tenant, start_key, edge_types, max_depth, direction)
        rows = await self._run(*compiled)
        return [
            GraphPath(node_keys=tuple(row["node_keys"]), edge_types=tuple(row["edge_types"]))
            for row in rows
        ]

    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int:
        """Delete all nodes/edges in the tenant scope, optionally under `path`.

        Returns the number of nodes deleted (0 if the compiled query somehow
        returns no rows, matching the `GraphStore` Protocol's contract).
        """
        rows = await self._run(*compile_delete_scope(tenant, path))
        return int(rows[0]["deleted"]) if rows else 0

    async def close(self) -> None:
        """Release the underlying driver's connection pool."""
        await self._driver.close()


def create_neo4j_store(bolt_url: str, user: str, password: str) -> Neo4jGraphStore:
    """Construct a `Neo4jGraphStore` backed by a real, bounded-timeout async driver.

    Credentials are taken as parameters (from `penguin_sal`/env at the
    caller), never hardcoded. Connection/acquisition timeouts are bounded so
    a broken graph backend fails fast instead of hanging a request.
    """
    driver = AsyncGraphDatabase.driver(
        bolt_url,
        auth=(user, password),
        connection_timeout=5.0,
        connection_acquisition_timeout=10.0,
        max_connection_lifetime=300,
    )
    return Neo4jGraphStore(driver)
