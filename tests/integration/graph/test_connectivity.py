"""Live-Neo4j round-trip proof for the Task 14 harness (`graph_client`, `unique_scope`).

Honestly SKIPped (not passed, not errored) when Neo4j is unreachable --
`make graph-neo4j-up` first for a real run. Tasks 15-17 build on this same
`graph_client`/`unique_scope` pair for isolation and cross-org proofs.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from shared.graph.client import TenantGraphClient
from shared.graph.types import GraphQuery, TenantScope

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_neo4j_round_trips(
    graph_client: TenantGraphClient,
    seed_ready_instance: bool,
    unique_scope: Callable[[str | None], TenantScope],
) -> None:
    """Upsert a node into a live Neo4j, query it back, then delete the scope."""
    assert seed_ready_instance
    scope = unique_scope("main")
    try:
        await graph_client.upsert_node(scope, "Module", "m.py", {"path": "m.py"})
        recs = await graph_client.query(scope, GraphQuery(labels=("Module",)))
        assert any(r.properties.get("path") == "m.py" for r in recs)
    finally:
        deleted = await graph_client.delete_scope(scope)
        assert deleted >= 1
