"""Shared vendor-abstracted graph-access layer (spec Section 3).

Neo4j is the interim backing store; consumers import only from this
package, never from a driver module, so the backend can be swapped later
without touching coderag graph, REST, or MCP call sites.
"""

from __future__ import annotations

from shared.graph.store import GraphStore
from shared.graph.types import (
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphQuery,
    GraphRecord,
    GraphScopeError,
    GraphUnavailableError,
    TenantScope,
)

__all__ = [
    "GraphStore",
    "TenantScope",
    "GraphQuery",
    "GraphRecord",
    "GraphNode",
    "GraphEdge",
    "GraphPath",
    "GraphUnavailableError",
    "GraphScopeError",
]
