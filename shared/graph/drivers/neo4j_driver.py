"""The Neo4j GraphStore driver's Cypher compilation layer.

Module-level ``compile_*`` functions are the ONLY place Cypher strings are
built for the graph platform (spec's vendor-abstraction requirement) --
pure, dependency-free functions turning the vendor-neutral graph types
(Task 2) into ``(cypher, params)`` pairs. Every value is passed as a
``$``-param, never string-interpolated (Cypher-injection safe). Labels,
relationship types, and dynamic property keys are the things Cypher cannot
parameterize, so they are validated -- labels/edge types against the
``_NODE_LABELS``/``_EDGE_TYPES`` allowlists, property keys against a safe
identifier shape -- and rejected with ``GraphScopeError`` rather than ever
interpolated unchecked.
The tenant scope predicate is unconditional and always wins over a
caller-supplied ``where``, mirroring the in-memory fake's
``{**query.where, **tenant.scope_props()}`` merge order
(``tests/unit/graph/fakes.py``) so isolation holds against both backends.
Task 5 appends ``Neo4jGraphStore``, the async I/O shell that actually runs
these against a live driver.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from shared.graph.types import _EDGE_TYPES, _NODE_LABELS, GraphQuery, GraphScopeError, TenantScope

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
