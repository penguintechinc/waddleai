"""Contract tests for the Neo4j Cypher compilers (Task 4, graph platform Phase 1).

These are the ONLY place Cypher strings are built for the graph platform, so
the tests pin down two invariants beyond the happy path: every value is a
`$`-param (never interpolated), and the tenant scope predicate is always
present in the compiled WHERE/MERGE and always wins over a caller-supplied
`where` -- mirroring the in-memory fake's `{**query.where, **scope_props()}`
merge order (`tests/unit/graph/fakes.py`) so isolation tests hold against
both backends.
"""

from __future__ import annotations

import pytest

from shared.graph.drivers import neo4j_driver as nd
from shared.graph.types import GraphQuery, GraphScopeError, TenantScope

SCOPE = TenantScope(org_id="7", repo_id="42", branch_ref="main")
UNBRANCHED_SCOPE = TenantScope(org_id="9", repo_id="99")


def test_upsert_node_merges_on_key_and_stamps_scope() -> None:
    """MERGE targets the allowlisted label; key and props are both $-params."""
    cypher, params = nd.compile_upsert_node(SCOPE, "Class", SCOPE.node_key("Foo"), {"name": "Foo"})
    assert "MERGE" in cypher and ":Class" in cypher
    assert "$key" in cypher and "$props" in cypher  # no value interpolation
    assert params["key"] == "7:42:main:Foo"
    assert params["props"]["org_id"] == "7" and params["props"]["repo_id"] == "42"
    assert params["props"]["branch_ref"] == "main" and params["props"]["name"] == "Foo"


def test_unknown_label_rejected() -> None:
    """A label outside the Task 2 allowlist is rejected, never interpolated."""
    with pytest.raises(GraphScopeError):
        nd.compile_upsert_node(SCOPE, "Secret", SCOPE.node_key("x"), {})


def test_unknown_edge_type_rejected() -> None:
    """An edge type outside the Task 2 allowlist is rejected, never interpolated."""
    with pytest.raises(GraphScopeError):
        nd.compile_upsert_edge(SCOPE, "OWNS", "a", "b", {})


def test_upsert_edge_scopes_both_endpoints() -> None:
    """Both the source AND destination node predicates are mandatory in the MATCH/WHERE.

    A `dst_key` belonging to another tenant must fail the MATCH -- this
    module is the designated query-layer enforcement point and never
    trusts the caller to have derived `dst_key` safely.
    """
    cypher, params = nd.compile_upsert_edge(
        SCOPE, "CALLS", "src-in-scope", "dst-other-tenant", {"weight": 1}
    )
    assert "s.org_id = $org_id" in cypher and "s.repo_id = $repo_id" in cypher
    assert "s.branch_ref = $branch_ref" in cypher
    assert "d.org_id = $org_id" in cypher and "d.repo_id = $repo_id" in cypher
    assert "d.branch_ref = $branch_ref" in cypher
    assert params["src_key"] == "src-in-scope" and params["dst_key"] == "dst-other-tenant"
    assert params["org_id"] == "7"
    assert params["props"]["weight"] == 1 and params["props"]["org_id"] == "7"


def test_upsert_node_properties_cannot_override_tenant_scope() -> None:
    """A hostile `properties["org_id"]` loses -- compiled props always carry the real scope."""
    hostile_properties = {"org_id": "999", "repo_id": "666", "name": "Foo"}
    _, params = nd.compile_upsert_node(SCOPE, "Class", SCOPE.node_key("Foo"), hostile_properties)
    assert params["props"]["org_id"] == "7" and params["props"]["repo_id"] == "42"
    assert params["props"]["name"] == "Foo"


def test_upsert_edge_properties_cannot_override_tenant_scope() -> None:
    """A hostile `properties["org_id"]` loses on the edge write path too."""
    hostile_properties = {"org_id": "999", "repo_id": "666"}
    _, params = nd.compile_upsert_edge(SCOPE, "CALLS", "src", "dst", hostile_properties)
    assert params["props"]["org_id"] == "7" and params["props"]["repo_id"] == "42"


def test_query_where_includes_scope_predicates() -> None:
    """A caller `where` equal to the scope round-trips through as the scope, not caller data."""
    cypher, params = nd.compile_query(
        SCOPE, GraphQuery(labels=("Class",), where=SCOPE.scope_props())
    )
    assert "n.org_id = $org_id" in cypher
    assert "n.repo_id = $repo_id" in cypher
    assert "n.branch_ref = $branch_ref" in cypher
    assert params["org_id"] == "7"


def test_query_empty_where_still_includes_org_id_predicate() -> None:
    """The org_id predicate is unconditional -- present even with no caller `where` at all."""
    cypher, params = nd.compile_query(SCOPE, GraphQuery())
    assert "n.org_id = $org_id" in cypher
    assert params["org_id"] == "7" and params["repo_id"] == "42"


def test_query_caller_where_cannot_override_tenant_scope() -> None:
    """A caller `where` claiming a different org/repo loses -- only the tenant scope compiles."""
    hostile_where = {"org_id": "999", "repo_id": "999", "name": "Foo"}
    cypher, params = nd.compile_query(SCOPE, GraphQuery(where=hostile_where))
    assert params["org_id"] == "7" and params["repo_id"] == "42"
    assert "999" not in params.values()
    assert "n.name = $w0" in cypher and params["w0"] == "Foo"


def test_query_rejects_unknown_label() -> None:
    """compile_query validates every requested label against the allowlist too."""
    with pytest.raises(GraphScopeError):
        nd.compile_query(SCOPE, GraphQuery(labels=("Secret",)))


def test_query_rejects_unsafe_property_key() -> None:
    """A `where` key that isn't a safe identifier is rejected, never interpolated raw."""
    hostile_where = {"name }) DETACH DELETE (n": "x"}
    with pytest.raises(GraphScopeError):
        nd.compile_query(SCOPE, GraphQuery(where=hostile_where))


def test_query_accepts_safe_property_key() -> None:
    """A normal snake_case property key compiles into its own $wN param."""
    cypher, params = nd.compile_query(SCOPE, GraphQuery(where={"file_path": "a/b.py"}))
    assert "n.file_path = $w0" in cypher
    assert params["w0"] == "a/b.py"


def test_query_rejects_property_key_with_trailing_newline() -> None:
    r"""A key ending in a newline is rejected -- Python's `$` matches before `\n`, `\Z` does not."""
    with pytest.raises(GraphScopeError):
        nd.compile_query(SCOPE, GraphQuery(where={"name\n": "x"}))


def test_query_applies_limit() -> None:
    """A GraphQuery.limit compiles to a bounded LIMIT clause."""
    cypher, _ = nd.compile_query(SCOPE, GraphQuery(limit=5))
    assert "LIMIT 5" in cypher


def test_query_omits_branch_predicate_when_scope_unbranched() -> None:
    """A TenantScope with no branch_ref never emits a branch_ref predicate."""
    cypher, params = nd.compile_query(UNBRANCHED_SCOPE, GraphQuery())
    assert "branch_ref" not in cypher
    assert "branch_ref" not in params


def test_traverse_scopes_both_endpoints_and_uses_allowlisted_rels() -> None:
    """Variable-length hop is bounded; start and end nodes are both tenant-scoped."""
    cypher, params = nd.compile_traverse(SCOPE, "7:42:main:Foo", ["CALLS"], 3, "out")
    assert ":CALLS*1..3" in cypher  # variable-length, bounded
    assert "start.org_id = $org_id" in cypher
    assert "end.org_id = $org_id" in cypher and "end.repo_id = $repo_id" in cypher
    assert params["start_key"] == "7:42:main:Foo"


def test_traverse_omits_branch_predicate_when_scope_unbranched() -> None:
    """compile_traverse also drops the end-node branch_ref clause for an unbranched scope."""
    cypher, params = nd.compile_traverse(UNBRANCHED_SCOPE, "k", ["CALLS"], 2, "out")
    assert "branch_ref" not in cypher
    assert "branch_ref" not in params


def test_traverse_rejects_unknown_edge_type() -> None:
    """compile_traverse validates every hop type against the allowlist."""
    with pytest.raises(GraphScopeError):
        nd.compile_traverse(SCOPE, "k", ["OWNS"], 2, "out")


@pytest.mark.parametrize(
    ("direction", "fragment"),
    [
        ("out", "(start)-[:CALLS*1..2]->(end)"),
        ("in", "(start)<-[:CALLS*1..2]-(end)"),
        ("both", "(start)-[:CALLS*1..2]-(end)"),
    ],
)
def test_traverse_direction_arrows(direction: str, fragment: str) -> None:
    """Each direction ('out'/'in'/'both') compiles to the matching Cypher arrow shape."""
    cypher, _ = nd.compile_traverse(SCOPE, "k", ["CALLS"], 2, direction)  # type: ignore[arg-type]
    assert fragment in cypher


def test_delete_scope_optional_path_predicate() -> None:
    """delete_scope's WHERE always carries org_id; the path predicate is opt-in only."""
    c1, p1 = nd.compile_delete_scope(SCOPE, None)
    assert "n.org_id = $org_id" in c1 and "n.path" not in c1
    c2, p2 = nd.compile_delete_scope(SCOPE, "a/b.py")
    assert "n.path = $path" in c2 and p2["path"] == "a/b.py"


def test_delete_scope_omits_branch_predicate_when_scope_unbranched() -> None:
    """delete_scope also drops the branch_ref clause for an unbranched TenantScope."""
    cypher, params = nd.compile_delete_scope(UNBRANCHED_SCOPE, None)
    assert "branch_ref" not in cypher
    assert "branch_ref" not in params
