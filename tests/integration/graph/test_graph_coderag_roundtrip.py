"""Live-Neo4j proof of the extract -> emit -> query round trip (spec Section 8b).

``extract_graph`` (``shared/knowledge/code_graph.py``, Tasks 9/10) is pure
AST-walking with no I/O -- it never touches Neo4j itself. This module
closes that gap for real: run a small Python fixture through
``extract_graph``, emit every ``GraphNodeDraft``/``GraphEdgeDraft`` through
the real ``TenantGraphClient`` (Task 8) into the live test Neo4j, then
round-trip ``call_graph``/``class_hierarchy`` and assert the exact known
structure of the fixture comes back -- not merely "non-empty". A broken
emit (wrong label/edge_type, an unstamped scope) or a broken query
(wrong traversal direction, a dropped tenant predicate) would surface here
as a missing or wrong ``node_keys`` tuple, the same way a mutation-tested
assertion would in ``test_isolation.py``.

A second test proves ``delete_scope(scope, path=...)`` narrows to one
file's nodes only, using two Module nodes from two different `path`s
emitted into the same scope -- the file-level granularity the coderag
worker (Task 11) depends on for incremental re-extraction.

Uses ``unique_scope`` (``tests/integration/graph/conftest.py``) for a
fresh UUID-suffixed ``TenantScope`` per test, not a fixed org/repo id --
the same collision-avoidance convention ``test_isolation.py`` uses, so a
leftover/interrupted prior run can never make this test's assertions
ambiguous. Session-scoped ``graph_client`` requires
``pytest.mark.asyncio(loop_scope="session")`` on this module (see
``conftest.py``'s module docstring).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphQuery, TenantScope
from shared.knowledge.code_graph import extract_graph

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

# One module, two classes (single-inheritance EXTENDS), one method that
# calls a known top-level function (CALLS), plus the CONTAINS nesting
# extract_graph always emits. Known structure this test asserts against:
#   m.py -CONTAINS-> Base, Derived, helper
#   Derived -CONTAINS-> Derived.run
#   Derived -EXTENDS-> Base
#   Derived.run -CALLS-> helper
_SRC = """
class Base:
    pass

class Derived(Base):
    def run(self):
        return helper()

def helper():
    return 1
"""


async def _emit(client: TenantGraphClient, scope: TenantScope, path: str, source: str) -> None:
    """Run ``extract_graph`` and write every resulting node/edge through ``client``.

    Mirrors the shape the coderag worker (Task 11) uses in production --
    one ``upsert_node``/``upsert_edge`` call per draft, scoped throughout.
    """
    frag = extract_graph(path, source)
    for node in frag.nodes:
        await client.upsert_node(
            scope, node.label, node.qualified_name, {"name": node.name, "path": node.path}
        )
    for edge in frag.edges:
        await client.upsert_edge(
            scope, edge.edge_type, edge.src_qn, edge.dst_qn, {"path": edge.path}
        )


async def test_extraction_and_queries_round_trip(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """extract_graph -> emit -> call_graph/class_hierarchy returns the fixture's exact structure.

    Asserts the precise single path each traversal must return (node_keys
    and edge_types), not just that a target key is somewhere in the
    result set -- a wrong direction, a dropped edge, or an unstamped
    scope would change the path shape and fail this, not just an
    emptiness check.
    """
    scope = unique_scope("main")
    try:
        await _emit(graph_client, scope, "m.py", _SRC)

        calls = await graph_client.call_graph(scope, "Derived.run", direction="out", depth=3)
        assert len(calls) == 1, f"expected exactly one CALLS path, got {calls}"
        assert calls[0].node_keys == (
            scope.node_key("Derived.run"),
            scope.node_key("helper"),
        )
        assert calls[0].edge_types == ("CALLS",)

        hierarchy = await graph_client.class_hierarchy(scope, "Derived", direction="out")
        assert len(hierarchy) == 1, f"expected exactly one EXTENDS path, got {hierarchy}"
        assert hierarchy[0].node_keys == (
            scope.node_key("Derived"),
            scope.node_key("Base"),
        )
        assert hierarchy[0].edge_types == ("EXTENDS",)

        # Honesty check: the fixture has no CALLS edge *into* Derived.run
        # (nothing calls it) and no EXTENDS edge out of Base (it has no
        # superclass) -- a query that returned paths regardless of the
        # fixture's real structure would wrongly pass these too.
        no_callers = await graph_client.call_graph(scope, "helper", direction="in", depth=3)
        assert no_callers and no_callers[0].node_keys == (
            scope.node_key("helper"),
            scope.node_key("Derived.run"),
        )
        no_base_parent = await graph_client.class_hierarchy(scope, "Base", direction="out")
        assert no_base_parent == []
    finally:
        await graph_client.delete_scope(scope)


async def test_file_delete_scrubs_only_that_file(
    graph_client: TenantGraphClient,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """`delete_scope(scope, path=...)` removes only the targeted file's nodes, within one scope.

    Emits the fixture's Module node (`path="m.py"`) alongside an unrelated
    Module node (`path="other.py"`) in the same tenant scope, then proves
    the file-scoped delete leaves `other.py` intact -- the granularity the
    coderag worker relies on to re-extract one changed file without
    wiping the rest of the repo's graph.
    """
    scope = unique_scope("main")
    try:
        await _emit(graph_client, scope, "m.py", _SRC)
        await graph_client.upsert_node(scope, "Module", "other.py", {"path": "other.py"})

        deleted = await graph_client.delete_scope(scope, path="m.py")
        assert deleted >= 1

        remaining = await graph_client.query(scope, GraphQuery(labels=("Module",)))
        paths = {r.properties.get("path") for r in remaining}
        assert paths == {"other.py"}, f"m.py should be scrubbed, other.py kept; got {paths}"
    finally:
        await graph_client.delete_scope(scope)
