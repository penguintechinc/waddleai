"""The vendor-neutral GraphStore Protocol.

This is the seam every consumer (coderag graph, REST, MCP) depends on --
they call `GraphStore` methods, never Cypher or a neo4j driver type
directly, so the Neo4j driver implementation built later stays swappable
(spec Section 3).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from shared.graph.types import GraphPath, GraphQuery, GraphRecord, TenantScope


@runtime_checkable
class GraphStore(Protocol):
    """Structural interface for a tenant-scoped property-graph backend.

    Every method takes a `TenantScope` first so an implementation can
    enforce the org/repo/branch predicate at the query layer -- never left
    to the caller to remember. `runtime_checkable` so callers/tests can
    `isinstance()`-check a driver against this Protocol without inheritance.
    """

    async def upsert_node(
        self, tenant: TenantScope, label: str, key: str, properties: dict[str, Any]
    ) -> None:
        """Create or update a single node, scoped to the tenant."""
        ...

    async def upsert_edge(
        self,
        tenant: TenantScope,
        edge_type: str,
        src_key: str,
        dst_key: str,
        properties: dict[str, Any],
    ) -> None:
        """Create or update a directed edge between two existing node keys."""
        ...

    async def query(self, tenant: TenantScope, query: GraphQuery) -> list[GraphRecord]:
        """Return the nodes matching the given label/property predicates."""
        ...

    async def traverse(
        self,
        tenant: TenantScope,
        start_key: str,
        edge_types: list[str],
        max_depth: int,
        direction: Literal["out", "in", "both"],
    ) -> list[GraphPath]:
        """Walk the graph from `start_key` up to `max_depth` hops."""
        ...

    async def delete_scope(self, tenant: TenantScope, path: str | None = None) -> int:
        """Delete all nodes/edges in the tenant scope, optionally under `path`.

        Returns the number of nodes deleted.
        """
        ...

    async def close(self) -> None:
        """Release any underlying driver resources (connections, sessions)."""
        ...
