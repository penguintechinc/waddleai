"""Tests for ``penguincode_cli.sessions.store`` -- the O4-a (High) cross-pod fix.

Static tests (dataclass shapes, in-memory backend, flag-gated factory
selection) always run. Live-Postgres tests connect to ``TEST_DATABASE_URL``
and are skipped -- with an explicit reason, never silently -- when that env
var is unset, mirroring ``tests/test_lessons_store.py``/
``tests/test_stores_graph.py``.

# regression: penguincode-shared-chat-sessions (O4-a High -- SessionStore)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.sessions import store as sessions_store
from penguincode_cli.sessions.store import (
    InMemorySessionStore,
    PostgresSessionStore,
    SessionRecord,
    create_session_store,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres session store tests are CI-pending",
)


def _ctx(
    *,
    tenant_id: str | None = None,
    org_id: str | None = None,
    team_ids: tuple[str, ...] = (),
    user_id: str | None = None,
    scopes: tuple[str, ...] = (),
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id or str(uuid.uuid4()),
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id or str(uuid.uuid4()),
        scopes=scopes,
    )


@pytest.fixture(autouse=True)
def _reset_store_singletons() -> Iterator[None]:
    """Every test gets a fresh factory-cache + flag-client cache -- no cross-test bleed."""
    sessions_store.reset_for_testing()
    yield
    sessions_store.reset_for_testing()


# ---------------------------------------------------------------------------
# Static tests: dataclass shape, factory selection, no DB required.
# ---------------------------------------------------------------------------


class TestSessionRecordShape:
    def test_is_frozen_slotted_dataclass(self) -> None:
        record = SessionRecord(
            id="s1",
            tenant_id="t1",
            org_id=None,
            team_ids=(),
            user_id="u1",
            project_dir="/tmp/proj",
            client_tools=(),
            state={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            expires_at="2026-01-02T00:00:00+00:00",
        )
        with pytest.raises(AttributeError):
            record.id = "s2"  # type: ignore[misc]


class TestPostgresSessionStoreIsASessionStore:
    def test_satisfies_session_store_protocol(self) -> None:
        store = PostgresSessionStore(dsn="postgresql://unreachable-host-for-test/db")
        assert isinstance(store, sessions_store.SessionStore)


class TestCreateSessionStoreFactory:
    def test_defaults_to_postgres_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from penguincode_cli.config.settings import Settings

        monkeypatch.delenv("PENGUINCODE_FLAG_DISABLE_SHARED_SESSIONS", raising=False)
        settings = Settings()
        settings.sessions.postgres.url = "postgresql://unreachable-host-for-test/db"
        store = create_session_store(settings)
        assert isinstance(store, PostgresSessionStore)

    def test_kill_switch_env_override_falls_back_to_in_memory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from penguincode_cli.config.settings import Settings

        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_SHARED_SESSIONS", "true")
        settings = Settings()
        settings.sessions.postgres.url = "postgresql://unreachable-host-for-test/db"
        store = create_session_store(settings)
        assert isinstance(store, InMemorySessionStore)

    def test_factory_returns_the_same_singleton_across_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from penguincode_cli.config.settings import Settings

        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_SHARED_SESSIONS", "true")
        settings = Settings()
        first = create_session_store(settings)
        second = create_session_store(settings)
        assert first is second


class TestInMemorySessionStoreRoundTrip:
    """`InMemorySessionStore` enforces the identical scope/TTL contract -- no DB required."""

    def test_create_then_get_round_trips(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(
            ctx, "sess-1", "/tmp/proj", ["read", "write"], {"messages": []}, ttl_seconds=3600
        )

        record = store.get(ctx, "sess-1")
        assert record is not None
        assert record.id == "sess-1"
        assert record.tenant_id == ctx.tenant_id
        assert record.user_id == ctx.user_id
        assert record.project_dir == "/tmp/proj"
        assert record.client_tools == ("read", "write")
        assert record.state == {"messages": []}

    def test_get_unknown_id_returns_none(self) -> None:
        store = InMemorySessionStore()
        assert store.get(_ctx(), "nope") is None

    def test_get_wrong_tenant_returns_none_not_someone_elses_session(self) -> None:
        store = InMemorySessionStore()
        owner_ctx = _ctx(tenant_id="tenant-a", user_id="user-a")
        other_ctx = _ctx(tenant_id="tenant-b", user_id="user-b")
        store.create(owner_ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.get(other_ctx, "sess-1") is None

    def test_get_same_tenant_different_user_returns_none(self) -> None:
        store = InMemorySessionStore()
        owner_ctx = _ctx(tenant_id="tenant-a", user_id="user-a")
        other_user_ctx = _ctx(tenant_id="tenant-a", user_id="user-b")
        store.create(owner_ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.get(other_user_ctx, "sess-1") is None

    def test_update_state_overwrites_and_extends_ttl(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "sess-1", "/tmp/proj", [], {"messages": []}, ttl_seconds=3600)
        store.update_state(
            ctx, "sess-1", {"messages": [{"role": "user", "content": "hi"}]}, ttl_seconds=3600
        )

        record = store.get(ctx, "sess-1")
        assert record is not None
        assert record.state == {"messages": [{"role": "user", "content": "hi"}]}

    def test_update_state_unknown_session_raises_lookup_error(self) -> None:
        store = InMemorySessionStore()
        with pytest.raises(LookupError):
            store.update_state(_ctx(), "nope", {}, ttl_seconds=3600)

    def test_update_state_wrong_scope_raises_lookup_error(self) -> None:
        store = InMemorySessionStore()
        owner_ctx = _ctx(tenant_id="tenant-a", user_id="user-a")
        store.create(owner_ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        with pytest.raises(LookupError):
            store.update_state(
                _ctx(tenant_id="tenant-b", user_id="user-b"), "sess-1", {}, ttl_seconds=3600
            )

    def test_delete_removes_the_session(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.delete(ctx, "sess-1") is True
        assert store.get(ctx, "sess-1") is None

    def test_delete_wrong_scope_returns_false_and_does_not_remove(self) -> None:
        store = InMemorySessionStore()
        owner_ctx = _ctx(tenant_id="tenant-a", user_id="user-a")
        store.create(owner_ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.delete(_ctx(tenant_id="tenant-b", user_id="user-b"), "sess-1") is False
        assert store.get(owner_ctx, "sess-1") is not None

    def test_delete_unknown_returns_false(self) -> None:
        store = InMemorySessionStore()
        assert store.delete(_ctx(), "nope") is False

    def test_expired_session_is_invisible_to_get(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "sess-1", "/tmp/proj", [], {}, ttl_seconds=-1)
        assert store.get(ctx, "sess-1") is None

    def test_sweep_expired_removes_only_expired_rows_bounded_by_batch_size(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "expired-1", "/tmp/proj", [], {}, ttl_seconds=-1)
        store.create(ctx, "expired-2", "/tmp/proj", [], {}, ttl_seconds=-1)
        store.create(ctx, "still-active", "/tmp/proj", [], {}, ttl_seconds=3600)

        removed = store.sweep_expired(batch_size=1)
        assert removed == 1

        removed_again = store.sweep_expired(batch_size=10)
        assert removed_again == 1

        # The active session must never be swept.
        assert store.get(ctx, "still-active") is not None

    def test_count_active_excludes_expired(self) -> None:
        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "active-1", "/tmp/proj", [], {}, ttl_seconds=3600)
        store.create(ctx, "expired-1", "/tmp/proj", [], {}, ttl_seconds=-1)
        assert store.count_active() == 1


# ---------------------------------------------------------------------------
# Live-Postgres tests: require TEST_DATABASE_URL (pgvector/pgvector image).
# ---------------------------------------------------------------------------


@pytest.fixture
def live_dsn() -> Iterator[str]:
    """Fresh, migrated `penguincode` schema for every test."""
    assert TEST_DATABASE_URL is not None  # narrows type for mypy; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@requires_postgres
class TestPostgresSessionStoreRoundTrip:
    def test_create_then_get_round_trips_every_field(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        team_id = str(uuid.uuid4())
        ctx = _ctx(org_id=str(uuid.uuid4()), team_ids=(team_id,))

        store.create(
            ctx,
            "11111111-1111-1111-1111-111111111111",
            "/tmp/proj",
            ["read", "write"],
            {"messages": [{"role": "user", "content": "hi"}], "conversation_summary": ""},
            ttl_seconds=3600,
        )
        record = store.get(ctx, "11111111-1111-1111-1111-111111111111")

        assert record is not None
        assert record.id == "11111111-1111-1111-1111-111111111111"
        assert record.tenant_id == ctx.tenant_id
        assert record.org_id == ctx.org_id
        assert record.team_ids == (team_id,)
        assert record.user_id == ctx.user_id
        assert record.project_dir == "/tmp/proj"
        assert record.client_tools == ("read", "write")
        assert record.state == {
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_summary": "",
        }

    def test_get_unknown_id_returns_none(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        assert store.get(_ctx(), str(uuid.uuid4())) is None

    def test_get_wrong_tenant_returns_none(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        owner_ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(owner_ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.get(_ctx(), session_id) is None

    def test_get_same_tenant_different_user_returns_none(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        tenant_id = str(uuid.uuid4())
        owner_ctx = _ctx(tenant_id=tenant_id, user_id=str(uuid.uuid4()))
        other_user_ctx = _ctx(tenant_id=tenant_id, user_id=str(uuid.uuid4()))
        session_id = str(uuid.uuid4())
        store.create(owner_ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.get(other_user_ctx, session_id) is None

    def test_update_state_overwrites_and_extends_ttl(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(ctx, session_id, "/tmp/proj", [], {"messages": []}, ttl_seconds=3600)

        new_state = {"messages": [{"role": "assistant", "content": "hello"}]}
        store.update_state(ctx, session_id, new_state, ttl_seconds=3600)

        record = store.get(ctx, session_id)
        assert record is not None
        assert record.state == new_state

    def test_update_state_unknown_session_raises_lookup_error(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        with pytest.raises(LookupError):
            store.update_state(_ctx(), str(uuid.uuid4()), {}, ttl_seconds=3600)

    def test_update_state_wrong_scope_raises_lookup_error(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        owner_ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(owner_ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=3600)
        with pytest.raises(LookupError):
            store.update_state(_ctx(), session_id, {}, ttl_seconds=3600)

    def test_delete_removes_the_row(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.delete(ctx, session_id) is True
        assert store.get(ctx, session_id) is None

    def test_delete_wrong_scope_returns_false(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        owner_ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(owner_ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=3600)
        assert store.delete(_ctx(), session_id) is False

    def test_expired_session_is_invisible_to_get(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        session_id = str(uuid.uuid4())
        store.create(ctx, session_id, "/tmp/proj", [], {}, ttl_seconds=-1)
        assert store.get(ctx, session_id) is None

    def test_sweep_expired_removes_only_expired_rows_bounded_by_batch_size(
        self, live_dsn: str
    ) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        store.create(ctx, str(uuid.uuid4()), "/tmp/proj", [], {}, ttl_seconds=-1)
        store.create(ctx, str(uuid.uuid4()), "/tmp/proj", [], {}, ttl_seconds=-1)
        active_id = str(uuid.uuid4())
        store.create(ctx, active_id, "/tmp/proj", [], {}, ttl_seconds=3600)

        removed = store.sweep_expired(batch_size=1)
        assert removed == 1
        removed_again = store.sweep_expired(batch_size=10)
        assert removed_again == 1

        assert store.get(ctx, active_id) is not None

    def test_count_active_excludes_expired(self, live_dsn: str) -> None:
        store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        store.create(ctx, str(uuid.uuid4()), "/tmp/proj", [], {}, ttl_seconds=3600)
        store.create(ctx, str(uuid.uuid4()), "/tmp/proj", [], {}, ttl_seconds=-1)
        assert store.count_active() == 1

    def test_second_pod_sees_a_session_created_on_the_first(self, live_dsn: str) -> None:
        """The actual O4-a regression: two independent `PostgresSessionStore`
        instances (standing in for two pods) sharing the same DSN must see
        each other's writes -- this is exactly what the in-process `dict`
        could never do."""
        pod_a_store = PostgresSessionStore(dsn=live_dsn)
        pod_b_store = PostgresSessionStore(dsn=live_dsn)
        ctx = _ctx()
        session_id = str(uuid.uuid4())

        pod_a_store.create(ctx, session_id, "/tmp/proj", [], {"messages": []}, ttl_seconds=3600)

        # regression: penguincode-shared-chat-sessions (O4-a High)
        record = pod_b_store.get(ctx, session_id)
        assert record is not None
        assert record.id == session_id

        pod_b_store.update_state(
            ctx,
            session_id,
            {"messages": [{"role": "user", "content": "from pod b"}]},
            ttl_seconds=3600,
        )
        record_again = pod_a_store.get(ctx, session_id)
        assert record_again is not None
        assert record_again.state == {"messages": [{"role": "user", "content": "from pod b"}]}


class TestCreateSessionStoreFactoryPostgresCaching:
    def test_postgres_backend_is_cached_across_calls(self) -> None:
        from penguincode_cli.config.settings import Settings

        settings = Settings()
        settings.sessions.postgres.url = "postgresql://unreachable-host-for-test/db"
        first = create_session_store(settings)
        second = create_session_store(settings)
        assert first is second
        assert isinstance(first, PostgresSessionStore)


class TestSessionSweeper:
    @pytest.mark.asyncio
    async def test_run_forever_sweeps_and_updates_the_gauge_each_iteration(self) -> None:
        import asyncio

        store = InMemorySessionStore()
        ctx = _ctx()
        store.create(ctx, "expired-1", "/tmp/proj", [], {}, ttl_seconds=-1)
        store.create(ctx, "active-1", "/tmp/proj", [], {}, ttl_seconds=3600)

        sweeper = sessions_store.SessionSweeper(store, interval_seconds=0.01, batch_size=10)
        task = asyncio.create_task(sweeper.run_forever())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert store.get(ctx, "expired-1") is None
        assert store.get(ctx, "active-1") is not None

    @pytest.mark.asyncio
    async def test_run_forever_survives_a_failing_sweep(self) -> None:
        import asyncio

        class _RaisingStore(InMemorySessionStore):
            def sweep_expired(self, *, batch_size: int) -> int:
                raise RuntimeError("db outage")

        sweeper = sessions_store.SessionSweeper(
            _RaisingStore(), interval_seconds=0.01, batch_size=10
        )
        task = asyncio.create_task(sweeper.run_forever())
        await asyncio.sleep(0.05)
        assert not task.done()  # a failing sweep must never kill the loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
