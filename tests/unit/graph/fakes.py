"""In-memory `GraphStore` fake -- the unit-test isolation oracle.

Backed by plain dicts/lists, no `neo4j` import, no I/O. It stores each
node's/edge's `properties` (which carry the `org_id`/`repo_id`/`branch_ref`
stamps a real `TenantGraphClient` adds) and filters every read by the exact
property predicates it is given, plus the caller's `TenantScope` -- the
latter always wins over a caller-supplied `where`, so this fake can never be
tricked into leaking another tenant's data. That is what makes a downstream
test asserting "org A's query returns nothing for org B's data" meaningful:
a pass-through fake that ignored scope would make such a test pass for the
wrong reason (graph platform Phase 1 plan, Task 3).
"""

from __future__ import annotations

from typing import Any, Literal

from shared.graph.types import GraphEdge, GraphNode, GraphPath, GraphQuery, GraphRecord, TenantScope


def _matches(properties: dict[str, Any], where: dict[str, Any]) -> bool:
    """Return True only if every key/value in `where` is present in `properties`.

    An empty `where` matches everything -- callers are expected to always
    pass a non-empty tenant-scoped predicate for a real filter.
    """
    return all(properties.get(key) == value for key, value in where.items())


class InMemoryGraphStore:
    """Dict-backed `GraphStore` used as the isolation oracle in unit tests.

    Structurally satisfies the `GraphStore` Protocol (Task 2). Every read
    (`query`, `traverse`) and write-scoped op (`delete_scope`) filters by the
    tenant's own `scope_props()`, never trusting a caller-supplied predicate
    alone, so tests built on this fake genuinely exercise tenant isolation.
    """

    def __init__(self) -> None:
        """Start with an empty node/edge registry."""
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []

    async def upsert_node(
        self, tenant: TenantScope, label: str, key: str, properties: dict[str, Any]
    ) -> None:
        """Create or replace the node at `key`.

        A second call with the same key overwrites rather than duplicating.
        """
        self._nodes[key] = GraphNode(key=key, label=label, properties=dict(properties))

    async def upsert_edge(
        self,
        tenant: TenantScope,
        edge_type: str,
        src_key: str,
        dst_key: str,
        properties: dict[str, Any],
    ) -> None:
        """Append a directed edge; edges are not deduplicated on (src, dst, type)."""
        self._edges.append(
            GraphEdge(
                edge_type=edge_type,
                src_key=src_key,
                dst_key=dst_key,
                properties=dict(properties),
            )
        )

    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]:
        """Return nodes matching `query.labels`/`query.where`, scoped to `tenant`.

        The tenant's own `scope_props()` are merged in last, so they always
        win over a caller-supplied `where` -- a `where` that claims a
        different org's scope can never leak that org's nodes back out.
        """
        effective_where = {**query.where, **tenant.scope_props()}
        results = [
            GraphRecord(key=node.key, label=node.label, properties=node.properties)
            for node in self._nodes.values()
            if (not query.labels or node.label in query.labels)
            and _matches(node.properties, effective_where)
        ]
        return results[: query.limit] if query.limit is not None else results

    async def traverse(
        self,
        tenant: TenantScope,
        start_key: str,
        edge_types: list[str],
        max_depth: int,
        direction: Literal["out", "in", "both"],
    ) -> list[GraphPath]:
        """Walk the graph from `start_key` up to `max_depth` hops.

        Only edges whose own properties match `tenant.scope_props()` are
        eligible hops -- matching key namespace alone is not sufficient, so
        an edge stamped for another tenant never appears in the walk even if
        its node keys happen to sit under the caller's key namespace.
        """
        scope = tenant.scope_props()
        allowed_types = set(edge_types)
        paths: list[GraphPath] = []

        def extend(node_keys: tuple[str, ...], edge_type_path: tuple[str, ...]) -> None:
            if len(edge_type_path) >= max_depth:
                return
            current = node_keys[-1]
            for edge in self._edges:
                if edge.edge_type not in allowed_types or not _matches(edge.properties, scope):
                    continue
                if direction in ("out", "both") and edge.src_key == current:
                    next_key = edge.dst_key
                elif direction in ("in", "both") and edge.dst_key == current:
                    next_key = edge.src_key
                else:
                    continue
                if next_key in node_keys:
                    continue  # cycle guard
                new_node_keys = (*node_keys, next_key)
                new_edge_types = (*edge_type_path, edge.edge_type)
                paths.append(GraphPath(node_keys=new_node_keys, edge_types=new_edge_types))
                extend(new_node_keys, new_edge_types)

        extend((start_key,), ())
        return paths

    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int:
        """Delete nodes/edges matching `tenant.scope_props()`, optionally under `path`.

        Returns the count of nodes deleted (edges are pruned but not
        counted, matching the `GraphStore` Protocol's documented return).
        """
        scope = tenant.scope_props()

        def in_target(properties: dict[str, Any]) -> bool:
            if not _matches(properties, scope):
                return False
            return path is None or properties.get("path") == path

        removed = sum(1 for node in self._nodes.values() if in_target(node.properties))
        self._nodes = {
            key: node for key, node in self._nodes.items() if not in_target(node.properties)
        }
        self._edges = [edge for edge in self._edges if not in_target(edge.properties)]
        return removed

    async def close(self) -> None:
        """No-op -- there is no underlying connection to release."""
        return None
