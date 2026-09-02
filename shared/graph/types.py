"""Vendor-neutral graph value types and the node/edge allowlists.

Pure data + validation. No neo4j import, no I/O, no Cypher -- consumers
depend on these types, never on a driver type, so swapping the backing
store is a driver change, not a rewrite (spec Section 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_NODE_LABELS = frozenset({"Module", "Class", "Method", "Function", "Field"})
_EDGE_TYPES = frozenset({"CALLS", "EXTENDS", "IMPLEMENTS", "CONTAINS", "REFERENCES"})

# Shared upper bound on a traversal's `depth`/`max_depth` -- every consumer
# (REST `services/management/app/api/v1/graph.py`, MCP
# `shared/mcp/graph_adapter.py` and `shared/mcp/tools.py`) clamps to this
# same constant rather than each hand-rolling its own literal, since it
# ultimately bounds the `[:REL*1..{depth}]` variable-length Cypher pattern
# `shared/graph/drivers/neo4j_driver.py` builds -- an unbounded value there
# is an expensive-traversal / availability risk, sharper still in Phase-1
# dev-mode where every org resolves to one shared Neo4j instance.
MAX_GRAPH_DEPTH = 10


class GraphUnavailableError(RuntimeError):
    """The org's graph instance is not ready/reachable.

    Raised by a GraphStore implementation so callers can map it to a clean
    503 instead of hanging or leaking a driver-specific exception.
    """


class GraphScopeError(ValueError):
    """A TenantScope or query was missing a mandatory tenant field.

    Raised when org_id/repo_id is empty -- the tenant isolation boundary
    must never be silently satisfiable with an empty predicate.
    """


@dataclass(slots=True, frozen=True)
class TenantScope:
    """The caller's tenant context for one graph operation.

    org_id/repo_id come from the validated JWT (or dev-mode's single shared
    instance), never from request body/params -- this is the isolation
    boundary every query and write is scoped through.
    """

    org_id: str
    repo_id: str
    branch_ref: str | None = None
    scope_type: str | None = None
    scope_ref: str | None = None

    def __post_init__(self) -> None:
        """Reject a scope missing its mandatory tenant predicate fields."""
        if not self.org_id:
            raise GraphScopeError("org_id is required")
        if not self.repo_id:
            raise GraphScopeError("repo_id is required")

    def node_key(self, qualified_name: str) -> str:
        """Build the composite node identity for this scope + qualified name.

        Format: ``{org_id}:{repo_id}:{branch_ref}:{qualified_name}`` (spec
        Section 4a) -- stable and collision-free across tenants/branches.
        """
        return f"{self.org_id}:{self.repo_id}:{self.branch_ref}:{qualified_name}"

    def scope_props(self) -> dict[str, str]:
        """Return the mandatory property predicates for every query/write.

        Always includes org_id and repo_id; branch_ref is included only
        when set, so unscoped-by-branch callers don't filter on None.
        """
        props = {"org_id": self.org_id, "repo_id": self.repo_id}
        if self.branch_ref is not None:
            props["branch_ref"] = self.branch_ref
        return props


@dataclass(slots=True, frozen=True)
class GraphQuery:
    """A node-match AST for ``GraphStore.query()``.

    Carries label + property predicates and a return/limit shape so the
    driver layer can translate to Cypher (or any other query language)
    without consumers ever constructing query strings themselves.
    """

    labels: tuple[str, ...] = ()
    where: dict[str, Any] = field(default_factory=dict)
    return_keys: bool = True
    limit: int | None = None


@dataclass(slots=True, frozen=True)
class GraphNode:
    """A single vertex in the property graph, identified by its node key."""

    key: str
    label: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphEdge:
    """A directed, typed relationship between two node keys."""

    edge_type: str
    src_key: str
    dst_key: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphRecord:
    """One row returned by ``GraphStore.query()`` -- a matched node's fields."""

    key: str
    label: str
    properties: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GraphPath:
    """One traversal path: ordered node keys and the edge types between them."""

    node_keys: tuple[str, ...]
    edge_types: tuple[str, ...]
