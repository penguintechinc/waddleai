"""Tests for the org -> graph-instance resolver and its dev-mode short-circuit.

Covers: a ready row resolves; every other status (or a missing row) raises
GraphUnavailableError promptly (never a hang); dev-mode upserts a ready row
from WADDLEAI_GRAPH_BOLT_URL; resolve_or_dev chains both end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.graph.resolver import ensure_dev_instance, resolve_instance, resolve_or_dev
from shared.graph.types import GraphUnavailableError


class FakeDB:
    """Minimal db stand-in: SELECT reads the current row; an upsert write updates it.

    Simulates real Postgres read-after-write behavior (INSERT ... ON CONFLICT
    then a subsequent SELECT sees the committed row) so resolve_or_dev's
    write-then-read round trip is honestly exercised.
    """

    def __init__(self, row: tuple | None) -> None:
        """Seed the single row a SELECT should return (None means no row)."""
        self._row = row
        self.written: list[tuple[str, list[Any]]] = []

    def executesql(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        """Return the current row for SELECT; apply and record an upsert write."""
        if sql.strip().upper().startswith("SELECT"):
            return [self._row] if self._row else []
        params = params or []
        self.written.append((sql, params))
        if "graph_instances" in sql and "INSERT" in sql.upper():
            _org_id, bolt_url = params
            self._row = ("ready", bolt_url)
        return []

    def commit(self) -> None:
        """No-op commit -- the fake has nothing to flush."""


@pytest.mark.asyncio
async def test_ready_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status='ready' row with a bolt_url resolves with env-sourced creds."""
    monkeypatch.setenv("WADDLEAI_GRAPH_USER", "neo4j")
    monkeypatch.setenv("WADDLEAI_GRAPH_PASSWORD", "secret")
    db = FakeDB(("ready", "bolt://neo4j:7687"))
    inst = await resolve_instance(db, org_id=7)
    assert inst.bolt_url == "bolt://neo4j:7687"
    assert inst.user == "neo4j"
    assert inst.password == "secret"  # noqa: S105 -- test fixture value, not a real secret


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [None, ("pending", None), ("failed", "bolt://x"), ("ready", None)],
)
async def test_non_ready_is_unavailable(row: tuple | None) -> None:
    """Missing row, non-ready status, or a ready row missing bolt_url all raise."""
    with pytest.raises(GraphUnavailableError):
        await resolve_instance(FakeDB(row), org_id=7)


@pytest.mark.asyncio
async def test_ready_row_defaults_user_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """WADDLEAI_GRAPH_USER defaults to 'neo4j' when not set."""
    monkeypatch.delenv("WADDLEAI_GRAPH_USER", raising=False)
    monkeypatch.setenv("WADDLEAI_GRAPH_PASSWORD", "secret")
    db = FakeDB(("ready", "bolt://neo4j:7687"))
    inst = await resolve_instance(db, org_id=7)
    assert inst.user == "neo4j"


@pytest.mark.asyncio
async def test_dev_mode_autocreates_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_or_dev upserts a ready row from WADDLEAI_GRAPH_BOLT_URL when no row exists."""
    monkeypatch.setenv("WADDLEAI_GRAPH_BOLT_URL", "bolt://localhost:7687")
    monkeypatch.setenv("WADDLEAI_GRAPH_PASSWORD", "secret")
    db = FakeDB(None)  # no row yet
    inst = await resolve_or_dev(db, org_id=7)
    assert inst.bolt_url == "bolt://localhost:7687"
    assert any("graph_instances" in sql for sql, _ in db.written)  # upserted a ready row


@pytest.mark.asyncio
async def test_ensure_dev_instance_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_dev_instance is a no-op (no write) when WADDLEAI_GRAPH_BOLT_URL is unset."""
    monkeypatch.delenv("WADDLEAI_GRAPH_BOLT_URL", raising=False)
    db = FakeDB(None)
    await ensure_dev_instance(db, org_id=7)
    assert db.written == []


@pytest.mark.asyncio
async def test_no_dev_and_no_row_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_or_dev still raises GraphUnavailableError when dev mode is off and no row exists."""
    monkeypatch.delenv("WADDLEAI_GRAPH_BOLT_URL", raising=False)
    with pytest.raises(GraphUnavailableError):
        await resolve_or_dev(FakeDB(None), org_id=7)


class RaisingDB:
    """A db stand-in whose `executesql` raises, simulating a real driver's type error.

    Models what a real Postgres `org_id` integer column does when handed a
    non-numeric value (e.g. `psycopg2.errors.InvalidTextRepresentation:
    invalid input syntax for type integer: "not-a-number"`) -- FakeDB above
    doesn't validate types at all, so it can't exercise this path.
    """

    def executesql(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        """Always raise, regardless of the query -- the DB driver rejected the params."""
        raise ValueError('invalid input syntax for type integer: "not-a-number"')

    def commit(self) -> None:
        """No-op -- never reached."""


@pytest.mark.asyncio
async def test_bad_org_id_db_error_is_unavailable_not_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric org_id that trips the DB driver's own type check fails closed.

    Carry-forward from Task 8's review: `TenantGraphClient` passes
    `scope.org_id` (str) straight through without coercion (see
    `shared/graph/client.py`'s module docstring), so a malformed value can
    reach `resolve_instance` unfiltered. The lookup must fail closed as
    `GraphUnavailableError`, never leak the raw DB exception.
    """
    with pytest.raises(GraphUnavailableError) as exc_info:
        await resolve_instance(RaisingDB(), org_id="not-a-number")  # type: ignore[arg-type]
    assert isinstance(exc_info.value.__cause__, ValueError)  # original error preserved, not lost


@pytest.mark.asyncio
async def test_dev_mode_upsert_is_parameterized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev-mode upsert binds org_id/bolt_url as params, never string-formats them in."""
    monkeypatch.setenv("WADDLEAI_GRAPH_BOLT_URL", "bolt://localhost:7687")
    db = FakeDB(None)
    await ensure_dev_instance(db, org_id=42)
    assert len(db.written) == 1
    sql, params = db.written[0]
    assert "42" not in sql
    assert "bolt://localhost:7687" not in sql
    assert 42 in params
    assert "bolt://localhost:7687" in params
