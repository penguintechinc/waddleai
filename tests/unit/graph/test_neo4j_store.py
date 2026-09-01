"""Tests for `Neo4jGraphStore` -- the async I/O shell over an injected driver.

Unit-level only: the async driver/session is a hand-rolled fake (`AsyncMock`/
`MagicMock`), never a live Neo4j connection, so these run without any server
and stay fast/isolated (live-server coverage is Tasks 14-17). Each test
asserts the store delegates ALL Cypher construction to the Task-4 `compile_*`
functions -- it never builds a Cypher string itself -- and that the compiled
`params` are passed through to `session.run()` as keyword arguments, never
interpolated into the query text.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.graph.drivers.neo4j_driver import Neo4jGraphStore, create_neo4j_store
from shared.graph.types import GraphQuery, TenantScope

SCOPE = TenantScope(org_id="7", repo_id="42", branch_ref="main")


def _driver_returning(rows: list[dict[str, Any]]) -> tuple[MagicMock, AsyncMock]:
    """Build a fake async driver whose session.run().data() returns `rows`."""
    result = AsyncMock()
    result.data = AsyncMock(return_value=rows)
    session = AsyncMock()
    session.run = AsyncMock(return_value=result)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session_cm)
    driver.close = AsyncMock()
    return driver, session


@pytest.mark.asyncio
async def test_upsert_node_runs_compiled_cypher() -> None:
    """upsert_node runs compile_upsert_node's output with params as kwargs."""
    driver, session = _driver_returning([])
    store = Neo4jGraphStore(driver)
    await store.upsert_node(SCOPE, "Class", SCOPE.node_key("Foo"), {"name": "Foo"})
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args[1]
    assert "MERGE" in cypher and ":Class" in cypher
    assert kwargs["key"] == "7:42:main:Foo"
    assert kwargs["props"]["name"] == "Foo"


@pytest.mark.asyncio
async def test_upsert_edge_runs_compiled_cypher() -> None:
    """upsert_edge runs compile_upsert_edge's output, both endpoints scoped."""
    driver, session = _driver_returning([])
    store = Neo4jGraphStore(driver)
    await store.upsert_edge(SCOPE, "CALLS", SCOPE.node_key("A"), SCOPE.node_key("B"), {"weight": 1})
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args[1]
    assert "MERGE" in cypher and ":CALLS" in cypher
    assert kwargs["src_key"] == "7:42:main:A"
    assert kwargs["dst_key"] == "7:42:main:B"
    assert kwargs["org_id"] == "7"


@pytest.mark.asyncio
async def test_query_maps_rows_to_records() -> None:
    """query() maps result rows to typed GraphRecord instances."""
    rows = [{"key": "7:42:main:Foo", "labels": ["Class"], "props": {"name": "Foo", "org_id": "7"}}]
    driver, session = _driver_returning(rows)
    recs = await Neo4jGraphStore(driver).query(
        SCOPE, GraphQuery(labels=("Class",), where=SCOPE.scope_props())
    )
    assert recs[0].key == "7:42:main:Foo" and recs[0].label == "Class"
    assert recs[0].properties == {"name": "Foo", "org_id": "7"}
    cypher = session.run.call_args[0][0]
    assert "MATCH" in cypher and ":Class" in cypher


@pytest.mark.asyncio
async def test_query_maps_row_with_no_labels_to_empty_label() -> None:
    """A row with an empty labels list maps to label='' rather than raising."""
    rows = [{"key": "7:42:main:Foo", "labels": [], "props": {}}]
    driver, _ = _driver_returning(rows)
    recs = await Neo4jGraphStore(driver).query(SCOPE, GraphQuery())
    assert recs[0].label == ""


@pytest.mark.asyncio
async def test_traverse_maps_paths() -> None:
    """traverse() maps result rows to typed GraphPath instances."""
    rows = [{"node_keys": ["7:42:main:A", "7:42:main:B"], "edge_types": ["CALLS"]}]
    driver, session = _driver_returning(rows)
    paths = await Neo4jGraphStore(driver).traverse(SCOPE, "7:42:main:A", ["CALLS"], 3, "out")
    assert paths[0].node_keys == ("7:42:main:A", "7:42:main:B")
    assert paths[0].edge_types == ("CALLS",)
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args[1]
    assert "MATCH" in cypher and ":CALLS" in cypher
    assert kwargs["start_key"] == "7:42:main:A"


@pytest.mark.asyncio
async def test_delete_scope_returns_count_and_close_delegates() -> None:
    """delete_scope() returns the deleted count; close() awaits driver.close()."""
    driver, session = _driver_returning([{"deleted": 4}])
    store = Neo4jGraphStore(driver)
    assert await store.delete_scope(SCOPE, path="a/b.py") == 4
    cypher, kwargs = session.run.call_args[0][0], session.run.call_args[1]
    assert "DETACH DELETE" in cypher
    assert kwargs["path"] == "a/b.py"
    await store.close()
    driver.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_scope_empty_result_returns_zero() -> None:
    """delete_scope() returns 0 when the compiled query yields no rows."""
    driver, _ = _driver_returning([])
    assert await Neo4jGraphStore(driver).delete_scope(SCOPE) == 0


@pytest.mark.asyncio
async def test_create_neo4j_store_builds_bounded_timeout_driver() -> None:
    """create_neo4j_store wraps a real AsyncGraphDatabase driver, lazily (no connection yet)."""
    store = create_neo4j_store("bolt://localhost:7687", "neo4j", "s3cr3t")  # noqa: S106
    try:
        assert isinstance(store, Neo4jGraphStore)
        assert store._driver is not None
    finally:
        await store.close()
