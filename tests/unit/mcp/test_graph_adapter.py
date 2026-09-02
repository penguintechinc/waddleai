"""Tests for `shared.mcp.graph_adapter.GraphKnowledgeService`.

Security-sensitive surface (spec Section 4a): org is always the caller's own
`org_id` argument (which `WaddleAITools` sources from `ctx.org_id`, never a
tool argument -- see `test_tools.py`); the `repo` argument is a repo *name*
resolved to a repo_id filtered on that same `org_id`, so an unknown-or-
other-org repo name degrades to an empty list exactly like a nonexistent
one (IDOR-safe). Unlike the REST surface (`services/management/app/api/v1/
graph.py`), MCP tool semantics never raise or hang: the `waddleai.graph`
flag being off, the `waddleai_graph` Enterprise entitlement being absent, an
unresolvable repo, `GraphUnavailableError`, and any other unexpected
failure all degrade to `[]`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from shared.graph.types import MAX_GRAPH_DEPTH, GraphPath, GraphUnavailableError, TenantScope
from shared.mcp.graph_adapter import GraphKnowledgeService

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDB:
    """A real-filtering fake `executesql`.

    Proves the org_id predicate actually narrows the result, not just that
    an empty result 404s/[]s. Mirrors `tests/unit/management/
    test_graph_api.py`'s gotcha-4 fix: an
    empty-mock db can't distinguish "queried correctly, found nothing" from
    "silently dropped the org filter and got lucky". This fake holds two
    rows with the *same* repo name in two *different* orgs, so a query
    missing the org_id predicate would wrongly match across tenants.
    """

    def __init__(self, rows: list[tuple[int, str, int]]) -> None:
        """rows: list of (org_id, repo_name, repo_id)."""
        self._rows = rows
        self.calls: list[tuple[str, list[Any]]] = []

    def executesql(self, sql: str, placeholders: list[Any] | None = None) -> list[Any]:
        """Evaluate the real (org_id, name) predicate against the fixture rows."""
        self.calls.append((sql, list(placeholders or [])))
        assert placeholders is not None
        org_id, repo_name = placeholders
        for o, n, rid in self._rows:
            if o == org_id and n == repo_name:
                return [[rid]]
        return []


class FakeTenantGraphClient:
    """A fake `TenantGraphClient` -- no live Neo4j, records the scope it was called with."""

    def __init__(
        self,
        paths: list[GraphPath] | None = None,
        raise_unavailable: bool = False,
        raise_unexpected: bool = False,
    ) -> None:
        """Configure the fake's canned response, or that it should raise on every call.

        ``raise_unexpected`` raises a plain ``RuntimeError`` (never
        ``GraphUnavailableError``) -- simulates a neo4j `AuthError` or any
        other unanticipated backend failure (e.g. a misconfigured
        `WADDLEAI_GRAPH_PASSWORD`), proving the adapter's broad catch
        degrades to `[]` too, not just the narrow `GraphUnavailableError`
        case.
        """
        self._paths = paths or []
        self._raise_unavailable = raise_unavailable
        self._raise_unexpected = raise_unexpected
        self.call_graph_calls: list[dict[str, Any]] = []
        self.class_hierarchy_calls: list[dict[str, Any]] = []

    async def call_graph(
        self, scope: TenantScope, symbol: str, *, direction: str = "out", depth: int = 3
    ) -> list[GraphPath]:
        """Record the call and return the canned paths, or raise per configuration."""
        self.call_graph_calls.append(
            {"scope": scope, "symbol": symbol, "direction": direction, "depth": depth}
        )
        if self._raise_unavailable:
            raise GraphUnavailableError("graph instance not ready")
        if self._raise_unexpected:
            raise RuntimeError("neo4j auth error")
        return self._paths

    async def class_hierarchy(
        self, scope: TenantScope, symbol: str, *, direction: str = "out"
    ) -> list[GraphPath]:
        """Record the call and return the canned paths, or raise per configuration."""
        self.class_hierarchy_calls.append(
            {"scope": scope, "symbol": symbol, "direction": direction}
        )
        if self._raise_unavailable:
            raise GraphUnavailableError("graph instance not ready")
        if self._raise_unexpected:
            raise RuntimeError("neo4j auth error")
        return self._paths


# Two rows, same repo name "widgets", two different orgs -- the IDOR fixture.
_TWO_ORG_ROWS = [(7, "widgets", 42), (99, "widgets", 999)]


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn `waddleai.graph` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "1")


def _entitled(monkeypatch: pytest.MonkeyPatch, entitled: bool = True) -> None:
    """Patch the license-entitlement check for one test (mirrors REST's `test_graph_api.py`)."""
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "shared.mcp.graph_adapter._get_license_client",
        lambda: mock_client,
    )


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[tuple[int, str, int]] | None = None,
    client: FakeTenantGraphClient | None = None,
    flag_on: bool = True,
    entitled: bool = True,
) -> tuple[GraphKnowledgeService, _FakeDB, FakeTenantGraphClient]:
    """Build a `GraphKnowledgeService` wired to fakes; flag on + entitled by default."""
    if flag_on:
        _enable_flag(monkeypatch)
    _entitled(monkeypatch, entitled)
    db = _FakeDB(rows if rows is not None else _TWO_ORG_ROWS)
    fake_client = client or FakeTenantGraphClient()
    service = GraphKnowledgeService(db, client=fake_client)
    return service, db, fake_client


# ---------------------------------------------------------------------------
# get_call_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_call_graph_returns_serialized_paths_for_own_org(monkeypatch):
    """A resolved repo in the caller's own org returns the serialized traversal."""
    paths = [GraphPath(("7:42:main:a", "7:42:main:b"), ("CALLS",))]
    service, _db, client = _service(monkeypatch, client=FakeTenantGraphClient(paths=paths))

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == [{"nodes": ["7:42:main:a", "7:42:main:b"], "edges": ["CALLS"]}]
    assert len(client.call_graph_calls) == 1


@pytest.mark.asyncio
async def test_get_call_graph_forwards_the_exact_caller_org_into_the_scope(monkeypatch):
    """The TenantScope's org_id is the exact org_id argument -- not the default, not repo_id."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch="dev", symbol="a", direction="in", depth=2
    )

    assert len(client.call_graph_calls) == 1
    call = client.call_graph_calls[0]
    scope = call["scope"]
    assert isinstance(scope, TenantScope)
    assert scope.org_id == "7"
    assert scope.repo_id == "42"
    assert scope.branch_ref == "dev"
    assert call["symbol"] == "a"
    assert call["direction"] == "in"
    assert call["depth"] == 2


@pytest.mark.asyncio
async def test_get_call_graph_defaults_branch_to_main(monkeypatch):
    """A None branch resolves to TenantScope.branch_ref == 'main'."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert client.call_graph_calls[0]["scope"].branch_ref == "main"


@pytest.mark.asyncio
async def test_get_call_graph_other_org_repo_name_returns_empty_not_another_orgs_data(
    monkeypatch,
):
    """IDOR: org 7 asking for org 99's repo name gets [], never org 99's repo_id/data."""
    service, db, client = _service(monkeypatch)

    result = await service.get_call_graph(
        org_id=7, repo="not-my-repo", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == []
    assert client.call_graph_calls == []
    # Prove the query really carried org_id=7 (not silently dropped/widened).
    assert db.calls[-1][1] == [7, "not-my-repo"]


@pytest.mark.asyncio
async def test_get_call_graph_cross_org_same_repo_name_isolated(monkeypatch):
    """Org 7 and org 99 both have a repo named 'widgets' -- each resolves to its own repo_id."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )
    await service.get_call_graph(
        org_id=99, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert client.call_graph_calls[0]["scope"].org_id == "7"
    assert client.call_graph_calls[0]["scope"].repo_id == "42"
    assert client.call_graph_calls[1]["scope"].org_id == "99"
    assert client.call_graph_calls[1]["scope"].repo_id == "999"


@pytest.mark.asyncio
async def test_get_call_graph_unavailable_returns_empty_list_not_a_raise(monkeypatch):
    """GraphUnavailableError from the client degrades to [] -- never propagates, never hangs."""
    service, _db, _client = _service(
        monkeypatch, client=FakeTenantGraphClient(raise_unavailable=True)
    )

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == []


@pytest.mark.asyncio
async def test_get_call_graph_flag_off_returns_empty_without_touching_db_or_client(monkeypatch):
    """waddleai.graph OFF short-circuits to [] before any repo lookup or graph call."""
    service, db, client = _service(monkeypatch, flag_on=False)
    monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == []
    assert db.calls == []
    assert client.call_graph_calls == []


@pytest.mark.asyncio
async def test_get_call_graph_not_entitled_returns_empty_without_touching_db_or_client(
    monkeypatch,
):
    """Flag on but no Enterprise `waddleai_graph` entitlement -> [] (never REST's 403).

    A Professional-tier org with the flag on must not get the Enterprise
    graph feature just by reaching the MCP surface instead of REST.
    """
    service, db, client = _service(monkeypatch, entitled=False)

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == []
    assert db.calls == []
    assert client.call_graph_calls == []


@pytest.mark.asyncio
async def test_get_call_graph_unexpected_error_returns_empty_list_not_a_raise(monkeypatch):
    """A non-GraphUnavailableError failure (e.g. neo4j AuthError) still degrades to [].

    Before this fix, only `GraphUnavailableError` was caught -- any other
    exception (a misconfigured `WADDLEAI_GRAPH_PASSWORD` surfacing as a
    neo4j `AuthError`, for instance) would leak raw to the MCP transport,
    contradicting this adapter's own degrade-to-[] contract.
    """
    service, _db, _client = _service(
        monkeypatch, client=FakeTenantGraphClient(raise_unexpected=True)
    )

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=3
    )

    assert result == []


# ---------------------------------------------------------------------------
# get_class_hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_class_hierarchy_returns_serialized_paths_for_own_org(monkeypatch):
    """A resolved repo in the caller's own org returns the serialized traversal."""
    paths = [GraphPath(("7:42:main:Base", "7:42:main:Child"), ("EXTENDS",))]
    service, _db, client = _service(monkeypatch, client=FakeTenantGraphClient(paths=paths))

    result = await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch=None, symbol="Base", direction="out"
    )

    assert result == [{"nodes": ["7:42:main:Base", "7:42:main:Child"], "edges": ["EXTENDS"]}]
    assert len(client.class_hierarchy_calls) == 1


@pytest.mark.asyncio
async def test_get_class_hierarchy_forwards_the_exact_caller_org_into_the_scope(monkeypatch):
    """The TenantScope's org_id is the exact org_id argument passed to the method."""
    service, _db, client = _service(monkeypatch)

    await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch="dev", symbol="Base", direction="both"
    )

    call = client.class_hierarchy_calls[0]
    scope = call["scope"]
    assert scope.org_id == "7"
    assert scope.repo_id == "42"
    assert scope.branch_ref == "dev"
    assert call["symbol"] == "Base"
    assert call["direction"] == "both"


@pytest.mark.asyncio
async def test_get_class_hierarchy_other_org_repo_name_returns_empty(monkeypatch):
    """IDOR: an other-org repo name degrades to [] -- never crosses the tenant boundary."""
    service, db, client = _service(monkeypatch)

    result = await service.get_class_hierarchy(
        org_id=7, repo="not-my-repo", branch=None, symbol="Base", direction="out"
    )

    assert result == []
    assert client.class_hierarchy_calls == []
    assert db.calls[-1][1] == [7, "not-my-repo"]


@pytest.mark.asyncio
async def test_get_class_hierarchy_unavailable_returns_empty_list_not_a_raise(monkeypatch):
    """GraphUnavailableError from the client degrades to [] -- never propagates, never hangs."""
    service, _db, _client = _service(
        monkeypatch, client=FakeTenantGraphClient(raise_unavailable=True)
    )

    result = await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch=None, symbol="Base", direction="out"
    )

    assert result == []


@pytest.mark.asyncio
async def test_get_class_hierarchy_flag_off_returns_empty_without_touching_db_or_client(
    monkeypatch,
):
    """waddleai.graph OFF short-circuits to [] before any repo lookup or graph call."""
    service, db, client = _service(monkeypatch, flag_on=False)
    monkeypatch.setenv("WADDLEAI_FLAG_GRAPH", "0")

    result = await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch=None, symbol="Base", direction="out"
    )

    assert result == []
    assert db.calls == []
    assert client.class_hierarchy_calls == []


@pytest.mark.asyncio
async def test_get_class_hierarchy_not_entitled_returns_empty_without_touching_db_or_client(
    monkeypatch,
):
    """Flag on but no Enterprise `waddleai_graph` entitlement -> [] (never REST's 403)."""
    service, db, client = _service(monkeypatch, entitled=False)

    result = await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch=None, symbol="Base", direction="out"
    )

    assert result == []
    assert db.calls == []
    assert client.class_hierarchy_calls == []


@pytest.mark.asyncio
async def test_get_class_hierarchy_unexpected_error_returns_empty_list_not_a_raise(monkeypatch):
    """A non-GraphUnavailableError failure still degrades to [] -- see the call-graph twin test."""
    service, _db, _client = _service(
        monkeypatch, client=FakeTenantGraphClient(raise_unexpected=True)
    )

    result = await service.get_class_hierarchy(
        org_id=7, repo="widgets", branch=None, symbol="Base", direction="out"
    )

    assert result == []


# ---------------------------------------------------------------------------
# Direction coercion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_direction_falls_back_to_out_rather_than_raising(monkeypatch):
    """A bad direction string degrades to 'out' -- MCP tools never error on a cosmetic mistake."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="sideways", depth=3
    )

    assert client.call_graph_calls[0]["direction"] == "out"


@pytest.mark.asyncio
async def test_default_tenant_graph_client_constructed_when_none_injected():
    """Omitting `client=` constructs a real TenantGraphClient bound to the given db."""
    from shared.graph.client import TenantGraphClient

    service = GraphKnowledgeService(_FakeDB([]))

    assert isinstance(service._client, TenantGraphClient)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Depth clamping (fix round 1: unbounded `depth` -> expensive Cypher traversal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_call_graph_clamps_a_very_large_depth_to_max_graph_depth(monkeypatch):
    """A caller-supplied depth far above MAX_GRAPH_DEPTH is clamped, never forwarded raw.

    Asserts the *forwarded* depth is bounded, not just that the call
    doesn't raise -- an unclamped depth reaching TenantGraphClient.call_graph
    drives an expensive [:REL*1..{depth}] Cypher traversal, sharper in
    Phase-1 dev-mode where every org shares one Neo4j instance.
    """
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=1_000_000
    )

    assert client.call_graph_calls[0]["depth"] == MAX_GRAPH_DEPTH
    assert client.call_graph_calls[0]["depth"] <= MAX_GRAPH_DEPTH


@pytest.mark.asyncio
async def test_get_call_graph_clamp_is_silent_not_an_error(monkeypatch):
    """An oversized depth degrades gracefully (clamped result), never raises."""
    service, _db, client = _service(monkeypatch, client=FakeTenantGraphClient(paths=[]))

    result = await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=999
    )

    assert result == []
    assert client.call_graph_calls[0]["depth"] == MAX_GRAPH_DEPTH


@pytest.mark.asyncio
async def test_get_call_graph_clamps_a_non_positive_depth_up_to_the_minimum(monkeypatch):
    """A zero/negative depth is clamped up to 1, not forwarded as-is."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=0
    )

    assert client.call_graph_calls[0]["depth"] == 1


@pytest.mark.asyncio
async def test_get_call_graph_leaves_an_in_range_depth_unchanged(monkeypatch):
    """A depth already within [1, MAX_GRAPH_DEPTH] passes through unmodified."""
    service, _db, client = _service(monkeypatch)

    await service.get_call_graph(
        org_id=7, repo="widgets", branch=None, symbol="a", direction="out", depth=5
    )

    assert client.call_graph_calls[0]["depth"] == 5
