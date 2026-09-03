"""Contract tests for vendor-neutral graph types (Task 2, graph platform Phase 1).

These pin down TenantScope's key/scope-prop derivation, the node/edge
allowlists, slots/frozen enforcement, and the exception hierarchy that every
later task (Neo4j driver, coderag graph, REST, MCP) builds on.
"""

from __future__ import annotations

import pytest

from shared.graph.types import (
    _EDGE_TYPES,
    _NODE_LABELS,
    GraphQuery,
    GraphRecord,
    GraphScopeError,
    GraphUnavailableError,
    TenantScope,
)


def test_scope_key_and_props() -> None:
    """node_key() and scope_props() include org/repo/branch when branch is set."""
    s = TenantScope(org_id="7", repo_id="42", branch_ref="main")
    assert s.node_key("pkg.Cls.method") == "7:42:main:pkg.Cls.method"
    assert s.scope_props() == {"org_id": "7", "repo_id": "42", "branch_ref": "main"}


def test_scope_props_omits_none_branch() -> None:
    """scope_props() drops branch_ref entirely when it was never set."""
    s = TenantScope(org_id="7", repo_id="42")
    assert s.scope_props() == {"org_id": "7", "repo_id": "42"}


def test_scope_rejects_empty_org_or_repo() -> None:
    """org_id and repo_id are the mandatory tenant predicate -- empty is a scope error."""
    with pytest.raises(GraphScopeError):
        TenantScope(org_id="", repo_id="42")
    with pytest.raises(GraphScopeError):
        TenantScope(org_id="7", repo_id="")


def test_label_and_edge_allowlists_frozen() -> None:
    """The node label and edge type allowlists are the exact frozensets in the spec."""
    assert _NODE_LABELS == frozenset({"Module", "Class", "Method", "Function", "Field"})
    assert _EDGE_TYPES == frozenset({"CALLS", "EXTENDS", "IMPLEMENTS", "CONTAINS", "REFERENCES"})


def test_slots_enforced() -> None:
    """TenantScope is a slots dataclass -- no __dict__, no ad hoc attributes."""
    s = TenantScope(org_id="7", repo_id="42")
    with pytest.raises(AttributeError):
        s.__dict__  # noqa: B018 -- slots dataclass has no __dict__


def test_errors_are_distinct_exceptions() -> None:
    """GraphUnavailableError maps to 503 semantics; GraphScopeError to 4xx validation."""
    assert issubclass(GraphUnavailableError, RuntimeError)
    assert issubclass(GraphScopeError, ValueError)


def test_dataclasses_are_slotted_and_frozen() -> None:
    """Every value type in the module is an immutable, slotted dataclass."""
    scope = TenantScope(org_id="7", repo_id="42")
    query = GraphQuery(labels=("Class",), where={"name": "Foo"})
    record = GraphRecord(key="7:42:None:Foo", label="Class", properties={"name": "Foo"})

    for instance in (scope, query, record):
        assert not hasattr(instance, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            instance.__setattr__("extra_field_not_declared", 1)  # noqa: B010


def test_graph_query_defaults() -> None:
    """GraphQuery defaults to no label filter, no predicates, keys returned, no limit."""
    q = GraphQuery()
    assert q.labels == ()
    assert q.where == {}
    assert q.return_keys is True
    assert q.limit is None


def test_graph_store_is_runtime_checkable_protocol() -> None:
    """GraphStore is a structural, runtime-checkable Protocol -- no ABC inheritance needed."""
    from typing import Protocol

    from shared.graph.store import GraphStore

    assert isinstance(GraphStore, type(Protocol))
    assert getattr(GraphStore, "_is_runtime_protocol", False) is True

    class FakeStore:
        """A structural stand-in exercising isinstance() against the Protocol."""

        async def upsert_node(self, tenant, label, key, properties) -> None:  # noqa: ANN001
            """No-op stand-in."""

        async def upsert_edge(  # noqa: ANN001
            self, tenant, edge_type, src_key, dst_key, properties
        ) -> None:
            """No-op stand-in."""

        async def query(self, tenant, query) -> list:  # noqa: ANN001
            """No-op stand-in."""
            return []

        async def traverse(  # noqa: ANN001
            self, tenant, start_key, edge_types, max_depth, direction
        ) -> list:
            """No-op stand-in."""
            return []

        async def delete_scope(self, tenant, path=None) -> int:  # noqa: ANN001
            """No-op stand-in."""
            return 0

        async def close(self) -> None:
            """No-op stand-in."""

    assert isinstance(FakeStore(), GraphStore)

    class IncompleteStore:
        """Missing every method -- must NOT satisfy the Protocol."""

    assert not isinstance(IncompleteStore(), GraphStore)
