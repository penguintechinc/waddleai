"""Tests for ``penguincode_cli.db.pool`` -- the shared, bounded `ConnectionPool` (ops-audit O7).

Static tests (lifecycle, kill-switch routing) always run against a fake
`ConnectionPool`/`psycopg.connect` double. Live-Postgres tests connect to
`TEST_DATABASE_URL` and are skipped -- with an explicit reason, never
silently -- when that env var is unset, mirroring every other store test in
this suite. The live tests prove the pool is actually bounded (via
`pg_stat_activity`, not just an assertion on this process's own bookkeeping)
and that `statement_timeout` is enforced server-side.

# regression: penguincode-ops-audit-O7 (shared db pool + clamps)
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import DbConfig
from penguincode_cli.db import pool as db_pool

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

#: ``penguincode.disable-db-pool`` -> ``PENGUINCODE_FLAG_DISABLE_DB_POOL``,
#: per ``flags.client._env_var_name``'s suffix-uppercase convention --
#: hardcoded literally here rather than importing that private helper,
#: mirroring ``tests/test_flags_client.py``'s own style.
_DISABLE_DB_POOL_ENV_VAR = "PENGUINCODE_FLAG_DISABLE_DB_POOL"

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres db pool tests are CI-pending",
)


def _ctx(tenant_id: str = "tenant-a") -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id, org_id=None, team_ids=(), user_id=str(uuid.uuid4()), scopes=()
    )


@pytest.fixture(autouse=True)
def _reset_shared_pool() -> Iterator[None]:
    """Every test starts and ends with no shared pool open -- no cross-test leakage."""
    db_pool.close_pool()
    yield
    db_pool.close_pool()


# ---------------------------------------------------------------------------
# Static tests: lifecycle + kill-switch routing, no live DB required for the
# "never touches the pool" assertion; the others need a reachable DSN but
# not necessarily Postgres (open(wait=False) doesn't block on connectivity).
# ---------------------------------------------------------------------------


class TestKillSwitchRouting:
    def test_disabled_flag_never_calls_get_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`DISABLE_DB_POOL_FLAG` ON -> legacy direct-connect path, pool untouched."""
        monkeypatch.setenv(_DISABLE_DB_POOL_ENV_VAR, "true")

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("get_pool must not be called when the kill-switch is on")

        monkeypatch.setattr(db_pool, "get_pool", _explode)

        connect_calls: list[str] = []

        class _FakeConn:
            def __enter__(self) -> _FakeConn:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        def _fake_connect(dsn: str, autocommit: bool = False) -> _FakeConn:
            connect_calls.append(dsn)
            return _FakeConn()

        monkeypatch.setattr(psycopg, "connect", _fake_connect)

        with db_pool.connection("postgresql://unused/db", _ctx()) as _conn:
            pass

        assert connect_calls == ["postgresql://unused/db"]

    def test_enabled_default_uses_the_shared_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag unseen (default OFF) -> the pool path is used, not a direct connect."""
        monkeypatch.delenv(_DISABLE_DB_POOL_ENV_VAR, raising=False)

        class _FakeConn:
            autocommit = False

        class _FakePool:
            def connection(self, timeout: float | None = None) -> _FakeConnCtx:
                return _FakeConnCtx()

            def get_stats(self) -> dict[str, int]:
                return {"pool_size": 1, "pool_available": 0, "requests_waiting": 0}

        class _FakeConnCtx:
            def __enter__(self) -> _FakeConn:
                return _FakeConn()

            def __exit__(self, *exc: object) -> None:
                return None

        fake_pool = _FakePool()
        monkeypatch.setattr(db_pool, "get_pool", lambda dsn=None: fake_pool)

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("psycopg.connect must not be called on the pooled path")

        monkeypatch.setattr(psycopg, "connect", _explode)

        with db_pool.connection("postgresql://unused/db", _ctx()) as conn:
            assert isinstance(conn, _FakeConn)


class TestPoolLifecycle:
    def test_open_pool_is_idempotent(self) -> None:
        pool1 = db_pool.open_pool("postgresql://unused/db", DbConfig(pool_min_size=0))
        pool2 = db_pool.open_pool("postgresql://a-different-dsn/db", DbConfig(pool_min_size=0))
        assert pool1 is pool2

    def test_get_pool_lazily_opens_from_env_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://from-env/db")
        pool = db_pool.get_pool()
        assert pool is db_pool.get_pool()

    def test_close_pool_is_idempotent_and_safe_before_open(self) -> None:
        db_pool.close_pool()  # never opened -- must not raise
        db_pool.open_pool("postgresql://unused/db", DbConfig(pool_min_size=0))
        db_pool.close_pool()
        db_pool.close_pool()  # already closed -- must not raise


# ---------------------------------------------------------------------------
# Live-Postgres tests: prove the pool is actually bounded and that
# statement_timeout is enforced server-side -- neither is provable against a
# fake.
# ---------------------------------------------------------------------------


def _other_backend_count(dsn: str) -> int:
    """Count Postgres backends connected to this DB, excluding this monitoring connection.

    Authoritative proof the pool is bounded: unlike `pool.get_stats()` (this
    process's own bookkeeping), this asks Postgres itself how many physical
    connections actually exist -- nothing else talks to the ephemeral test
    container, so every other backend is one of the pool's own connections.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid != pg_backend_pid()"
        ).fetchone()
    assert row is not None
    return int(row[0])


@requires_postgres
class TestPoolIsBoundedLive:
    def test_n_plus_one_borrowers_block_rather_than_opening_n_plus_one_connections(self) -> None:
        assert TEST_DATABASE_URL is not None
        max_size = 2
        pool = db_pool.open_pool(
            TEST_DATABASE_URL,
            DbConfig(pool_min_size=0, pool_max_size=max_size, pool_timeout_seconds=1.0),
        )
        pool.wait(timeout=5.0)
        ctx = _ctx()

        released = threading.Event()
        held = threading.Barrier(max_size + 1)  # max_size borrowers + this thread

        def _hold_connection() -> None:
            with db_pool.connection(TEST_DATABASE_URL, ctx, pool=pool):
                held.wait(timeout=5.0)
                released.wait(timeout=5.0)

        threads = [threading.Thread(target=_hold_connection) for _ in range(max_size)]
        for t in threads:
            t.start()
        held.wait(timeout=5.0)  # both borrowers are holding a connection now

        # A third, concurrent borrow must time out -- the pool is bounded at
        # max_size, it does not silently open a 3rd physical connection.
        with pytest.raises(PoolTimeout):
            with db_pool.connection(TEST_DATABASE_URL, ctx, pool=pool):
                pass

        # Prove it from Postgres's own side (`pg_stat_activity`), not just
        # this process's bookkeeping: never more than `max_size` backends.
        assert _other_backend_count(TEST_DATABASE_URL) <= max_size

        released.set()
        for t in threads:
            t.join(timeout=5.0)
        db_pool.close_pool()

    def test_stores_work_through_the_pool(self) -> None:
        """A normal borrow/release cycle round-trips a real query."""
        assert TEST_DATABASE_URL is not None
        pool = db_pool.open_pool(TEST_DATABASE_URL, DbConfig(pool_min_size=1, pool_max_size=5))
        pool.wait(timeout=5.0)
        ctx = _ctx()

        with db_pool.connection(TEST_DATABASE_URL, ctx, pool=pool) as conn:
            row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)
        db_pool.close_pool()


@requires_postgres
class TestStatementTimeoutLive:
    def test_statement_timeout_kills_a_runaway_query(self) -> None:
        assert TEST_DATABASE_URL is not None
        pool = db_pool.open_pool(
            TEST_DATABASE_URL,
            DbConfig(pool_min_size=1, pool_max_size=2, statement_timeout_ms=200),
        )
        pool.wait(timeout=5.0)
        ctx = _ctx()

        with pytest.raises(psycopg.errors.QueryCanceled):
            with db_pool.connection(TEST_DATABASE_URL, ctx, pool=pool) as conn:
                conn.execute("SELECT pg_sleep(5)")
        db_pool.close_pool()

    def test_default_timeout_allows_a_fast_query(self) -> None:
        assert TEST_DATABASE_URL is not None
        pool = db_pool.open_pool(
            TEST_DATABASE_URL,
            DbConfig(pool_min_size=1, pool_max_size=2, statement_timeout_ms=15000),
        )
        pool.wait(timeout=5.0)
        ctx = _ctx()

        with db_pool.connection(TEST_DATABASE_URL, ctx, pool=pool) as conn:
            row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)
        db_pool.close_pool()
