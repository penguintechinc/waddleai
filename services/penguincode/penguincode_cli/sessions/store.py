"""SessionStore: scope-aware, cross-pod persistence for PenguinCode chat sessions.

**The bug this fixes (security audit O4-a, High).** `ChatServiceImpl`
(`server/services/chat.py`) kept `sessions: dict[str, SessionState]` as
in-process state while `CreateSession`/`Chat`/`GetHistory`/`CloseSession`
are four independent gRPC RPCs, with nothing guaranteeing they land on the
same pod -- prod runs `replicas=3`
(`k8s/helm/penguincode/production.yml`). A session created on one pod 404s
on every other pod behind the same Service, and a rolling deploy silently
drops every session mid-conversation. This module makes session state live
in the shared `penguincode` Postgres schema instead (`db/migrations/
0007_chat_sessions.sql`), the same pattern `stores.vector`/`stores.graph`/
`lessons.store` already use for their own tables -- `PostgresSessionStore`
is the sole read/write chokepoint; `server/services/chat.py` never issues
raw SQL.

**Scope model (deliberately narrower than `GraphStore`'s three tiers).** A
chat session is visible to its own tenant AND its owning user only --
never team- or tenant-shared, unlike `graph_nodes`'/`docs_vectors`' three-
tier (user/team/tenant) *read* visibility. `get`/`update_state`/`delete`
therefore filter on `(tenant_id, user_id, id)`, not tenant alone.
`team_ids` is still stamped from `ctx.team_ids` at `create()` time and
persisted, for provenance/audit only (mirrors `pending_lessons.
source_team_id`'s rationale) -- it is never part of the read filter. A
session that does not exist, belongs to a different tenant, or belongs to
a different user within the same tenant are all indistinguishable `None`/
`LookupError` outcomes, so a caller can never use this store to probe
whether a given session id exists elsewhere.

**Legacy standalone mode (kill-switch only).** Every `ChatService` RPC is
now RS256-gated by default (`server/interceptors.py`'s Chat RS256 gate --
tenancy-gap fix), so `server/services/chat.py`'s `_scope_for_request`
synthesizes a fixed single pseudo-tenant (`LEGACY_TENANT_ID`), scoped by
the authenticated caller's HS256 token `sub` only, exclusively when an
operator has explicitly set the `penguincode.disable-chat-rs256-gate`
opt-out kill switch -- an emergency rollback, never the steady-state
default. Both modes go through the exact same `SessionStore` methods
below.

**TTL and expiry.** Every `create`/`update_state` call sets `expires_at` to
`now() + settings.sessions.ttl_seconds` (`PENGUINCODE_SESSION_TTL_SECONDS`
env, default 24h, see `config.settings.SessionsConfig`) -- a session that
is read or chatted with keeps extending its own lease; one left idle for
the full TTL is swept. `SessionSweeper` deletes expired rows in bounded
batches on `PENGUINCODE_SESSION_SWEEP_INTERVAL_SECONDS` (default 5 min),
sized by `PENGUINCODE_SESSION_SWEEP_BATCH_SIZE` (default 500) so one sweep
never issues an unbounded `DELETE`.

**Kill switch.** `DISABLE_SHARED_SESSIONS_FLAG`
(`penguincode.disable-shared-sessions`) is an *opt-out* mechanism flag,
unseen/OFF = the new shared-Postgres mechanism is ON (the fix below is
active); an operator turning it ON reverts to `InMemorySessionStore` --
the pre-fix, per-pod-only behavior -- as an emergency rollback path.
Evaluated against a fixed, tenant-independent `_SYSTEM_SCOPE` (not the
caller's own `ScopeContext`): this is a global operational switch, not a
per-tenant rollout, so every pod and every tenant must agree on the same
answer at the same moment -- a per-tenant evaluation would let a session
silently become invisible mid-conversation if the two backends ever
disagree about where it lives.

Every store method here is a *blocking* psycopg call -- `server/services/
chat.py` runs each one via `asyncio.to_thread`, mirroring `server/services/
lessons.py`'s `PendingLessonStore` usage, and never holds a connection open
across an LLM call (`ChatAgent.process`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.config.settings import Settings
from penguincode_cli.flags import is_enabled
from penguincode_cli.sessions.metrics import record_active_sessions, timed_session_store_operation

logger = logging.getLogger(__name__)

#: Opt-out kill switch (see module docstring) -- unseen/OFF means the
#: shared-Postgres mechanism is active.
DISABLE_SHARED_SESSIONS_FLAG = "penguincode.disable-shared-sessions"

#: Fixed-identity scope used ONLY to evaluate `DISABLE_SHARED_SESSIONS_FLAG`
#: -- see module docstring "Kill switch" for why this is deliberately not
#: the caller's own `ScopeContext`.
_SYSTEM_SCOPE = ScopeContext(
    tenant_id="__system__", org_id=None, team_ids=(), user_id="__system__", scopes=()
)

#: Legacy standalone client-server mode's fixed pseudo-tenant -- see module
#: docstring "Legacy standalone mode". Exported so `server/services/chat.py`
#: can build the fallback `ScopeContext` without duplicating the literal.
LEGACY_TENANT_ID = "_legacy"


@dataclass(slots=True, frozen=True)
class SessionRecord:
    """One `chat_sessions` row, as read back by a `SessionStore`.

    `state` is the serializable conversation state (`{"messages": [...],
    "conversation_summary": "..."}`, see `server/services/chat.py`'s
    `_state_from_agent`/`_restore_agent_state`) -- never a live `ChatAgent`
    or `OllamaClient`, which cannot survive a round trip through Postgres
    (or a different pod's process) and must be reconstructed by the
    servicer on every `Chat`/`GetHistory` call instead.
    """

    id: str
    tenant_id: str
    org_id: str | None
    team_ids: tuple[str, ...]
    user_id: str
    project_dir: str
    client_tools: tuple[str, ...]
    state: dict[str, Any]
    created_at: str
    updated_at: str
    expires_at: str


@runtime_checkable
class SessionStore(Protocol):
    """Scope-aware persistence contract every chat-session backend implements.

    See the module docstring for the tenant+user scope filter every method
    below enforces, and the TTL/expiry contract `create`/`update_state`
    extend on every call.
    """

    def create(
        self,
        ctx: ScopeContext,
        session_id: str,
        project_dir: str,
        client_tools: Sequence[str],
        state: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        """Insert a new session row, stamping tenant/org/team/user from `ctx`."""
        ...

    def get(self, ctx: ScopeContext, session_id: str) -> SessionRecord | None:
        """One session by id, scoped to `ctx`'s tenant+user; `None` if not found/expired."""
        ...

    def update_state(
        self, ctx: ScopeContext, session_id: str, state: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        """Overwrite `state` and extend `expires_at` by `ttl_seconds` from now.

        Raises `LookupError` if no matching, non-expired row exists for
        `ctx`'s tenant+user -- the caller (`Chat`'s handler) maps that to a
        `SESSION_NOT_FOUND` response rather than resurrecting the row.
        """
        ...

    def delete(self, ctx: ScopeContext, session_id: str) -> bool:
        """Delete one session, scoped to `ctx`'s tenant+user. Returns whether a row was removed."""
        ...

    def sweep_expired(self, *, batch_size: int) -> int:
        """Delete up to `batch_size` expired rows (any tenant). Returns the count removed."""
        ...

    def count_active(self) -> int:
        """Count of all non-expired rows (any tenant) -- feeds the `active_sessions` gauge."""
        ...


def _row_to_record(row: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        org_id=str(row["org_id"]) if row["org_id"] is not None else None,
        team_ids=tuple(str(t) for t in (row["team_ids"] or [])),
        user_id=str(row["user_id"]),
        project_dir=row["project_dir"],
        client_tools=tuple(row["client_tools"] or []),
        state=row["state"] or {},
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        expires_at=row["expires_at"].isoformat(),
    )


_SELECT_COLUMNS = (
    "id, tenant_id, org_id, team_ids, user_id, project_dir, client_tools, "
    "state, created_at, updated_at, expires_at"
)


class PostgresSessionStore:
    """Shared-Postgres `SessionStore` -- the O4-a High fix. See module docstring."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create(
        self,
        ctx: ScopeContext,
        session_id: str,
        project_dir: str,
        client_tools: Sequence[str],
        state: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        with timed_session_store_operation("create"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    INSERT INTO penguincode.chat_sessions
                        (id, tenant_id, org_id, team_ids, user_id, project_dir,
                         client_tools, state, expires_at)
                    VALUES (
                        %(id)s::uuid, %(tenant_id)s::uuid, %(org_id)s::uuid,
                        %(team_ids)s::uuid[], %(user_id)s::uuid, %(project_dir)s,
                        %(client_tools)s, %(state)s, now() + %(ttl_seconds)s * interval '1 second'
                    )
                    """,
                    {
                        "id": session_id,
                        "tenant_id": ctx.tenant_id,
                        "org_id": ctx.org_id,
                        "team_ids": list(ctx.team_ids),
                        "user_id": ctx.user_id,
                        "project_dir": project_dir,
                        "client_tools": Jsonb(list(client_tools)),
                        "state": Jsonb(state),
                        "ttl_seconds": ttl_seconds,
                    },
                )

    def get(self, ctx: ScopeContext, session_id: str) -> SessionRecord | None:
        with timed_session_store_operation("get"):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    # _SELECT_COLUMNS is a module-level constant column list,
                    # never user input; every actual value is bound via the
                    # params dict below, never interpolated.
                    cur.execute(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                        FROM penguincode.chat_sessions
                        WHERE id = %(id)s::uuid AND tenant_id = %(tenant_id)s::uuid
                          AND user_id = %(user_id)s::uuid AND expires_at > now()
                        """,  # nosec B608
                        {"id": session_id, "tenant_id": ctx.tenant_id, "user_id": ctx.user_id},
                    )
                    row = cur.fetchone()

        return _row_to_record(row) if row is not None else None

    def update_state(
        self, ctx: ScopeContext, session_id: str, state: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        with timed_session_store_operation("update_state"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE penguincode.chat_sessions
                        SET state = %(state)s,
                            updated_at = now(),
                            expires_at = now() + %(ttl_seconds)s * interval '1 second'
                        WHERE id = %(id)s::uuid AND tenant_id = %(tenant_id)s::uuid
                          AND user_id = %(user_id)s::uuid AND expires_at > now()
                        """,
                        {
                            "state": Jsonb(state),
                            "ttl_seconds": ttl_seconds,
                            "id": session_id,
                            "tenant_id": ctx.tenant_id,
                            "user_id": ctx.user_id,
                        },
                    )
                    if cur.rowcount == 0:
                        raise LookupError(
                            f"no active session {session_id!r} found for this caller's tenant+user"
                        )

    def delete(self, ctx: ScopeContext, session_id: str) -> bool:
        with timed_session_store_operation("delete"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM penguincode.chat_sessions
                        WHERE id = %(id)s::uuid AND tenant_id = %(tenant_id)s::uuid
                          AND user_id = %(user_id)s::uuid
                        """,
                        {"id": session_id, "tenant_id": ctx.tenant_id, "user_id": ctx.user_id},
                    )
                    return cur.rowcount > 0

    def sweep_expired(self, *, batch_size: int) -> int:
        with timed_session_store_operation("sweep_expired"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM penguincode.chat_sessions
                        WHERE id IN (
                            SELECT id FROM penguincode.chat_sessions
                            WHERE expires_at <= now()
                            ORDER BY expires_at
                            LIMIT %(batch_size)s
                        )
                        """,
                        {"batch_size": batch_size},
                    )
                    return cur.rowcount

    def count_active(self) -> int:
        with timed_session_store_operation("count_active"):
            with psycopg.connect(self._dsn) as conn:
                row = conn.execute(
                    "SELECT count(*) FROM penguincode.chat_sessions WHERE expires_at > now()"
                ).fetchone()
        assert row is not None  # SELECT count(*) always yields exactly one row
        return int(row[0])


@dataclass(slots=True)
class _MemoryRow:
    """Mutable in-process row backing `InMemorySessionStore` -- never shared across processes."""

    record: SessionRecord


class InMemorySessionStore:
    """Process-local `SessionStore` -- tests, local dev, and the kill-switch fallback.

    Enforces the identical tenant+user scope filter and TTL/expiry contract
    as `PostgresSessionStore` (same indistinguishable-`None` behavior for a
    wrong-scope or expired lookup), just backed by a dict instead of a
    table -- deliberately NOT shared across pods, which is exactly the
    pre-fix (O4-a) behavior this backend intentionally reproduces when
    selected.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _MemoryRow] = {}
        self._lock = threading.Lock()

    def create(
        self,
        ctx: ScopeContext,
        session_id: str,
        project_dir: str,
        client_tools: Sequence[str],
        state: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        now = datetime.now(UTC)
        record = SessionRecord(
            id=session_id,
            tenant_id=ctx.tenant_id,
            org_id=ctx.org_id,
            team_ids=ctx.team_ids,
            user_id=ctx.user_id,
            project_dir=project_dir,
            client_tools=tuple(client_tools),
            state=dict(state),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        )
        with self._lock:
            self._rows[session_id] = _MemoryRow(record=record)

    def _visible(self, ctx: ScopeContext, session_id: str) -> SessionRecord | None:
        row = self._rows.get(session_id)
        if row is None:
            return None
        rec = row.record
        if rec.tenant_id != ctx.tenant_id or rec.user_id != ctx.user_id:
            return None
        if datetime.fromisoformat(rec.expires_at) <= datetime.now(UTC):
            return None
        return rec

    def get(self, ctx: ScopeContext, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._visible(ctx, session_id)

    def update_state(
        self, ctx: ScopeContext, session_id: str, state: dict[str, Any], *, ttl_seconds: int
    ) -> None:
        now = datetime.now(UTC)
        with self._lock:
            current = self._visible(ctx, session_id)
            if current is None:
                raise LookupError(
                    f"no active session {session_id!r} found for this caller's tenant+user"
                )
            self._rows[session_id] = _MemoryRow(
                record=SessionRecord(
                    id=current.id,
                    tenant_id=current.tenant_id,
                    org_id=current.org_id,
                    team_ids=current.team_ids,
                    user_id=current.user_id,
                    project_dir=current.project_dir,
                    client_tools=current.client_tools,
                    state=dict(state),
                    created_at=current.created_at,
                    updated_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
                )
            )

    def delete(self, ctx: ScopeContext, session_id: str) -> bool:
        with self._lock:
            if self._visible(ctx, session_id) is None:
                return False
            del self._rows[session_id]
            return True

    def sweep_expired(self, *, batch_size: int) -> int:
        now = datetime.now(UTC)
        with self._lock:
            expired = [
                sid
                for sid, row in self._rows.items()
                if datetime.fromisoformat(row.record.expires_at) <= now
            ][:batch_size]
            for sid in expired:
                del self._rows[sid]
            return len(expired)

    def count_active(self) -> int:
        now = datetime.now(UTC)
        with self._lock:
            return sum(
                1
                for row in self._rows.values()
                if datetime.fromisoformat(row.record.expires_at) > now
            )


_inmemory_store_singleton: InMemorySessionStore | None = None
_inmemory_store_lock = threading.Lock()
_postgres_store_cache: dict[str, PostgresSessionStore] = {}
_postgres_store_cache_lock = threading.Lock()


def _shared_inmemory_store() -> InMemorySessionStore:
    """The one process-wide `InMemorySessionStore` -- must persist across calls on a pod."""
    global _inmemory_store_singleton
    with _inmemory_store_lock:
        if _inmemory_store_singleton is None:
            _inmemory_store_singleton = InMemorySessionStore()
        return _inmemory_store_singleton


def _shared_postgres_store(dsn: str) -> PostgresSessionStore:
    with _postgres_store_cache_lock:
        store = _postgres_store_cache.get(dsn)
        if store is None:
            store = PostgresSessionStore(dsn=dsn)
            _postgres_store_cache[dsn] = store
        return store


def create_session_store(settings: Settings) -> SessionStore:
    """Resolve the process-wide `SessionStore`: shared Postgres, or the kill-switch fallback.

    See module docstring "Kill switch" for why this reads
    `DISABLE_SHARED_SESSIONS_FLAG` against the fixed `_SYSTEM_SCOPE` rather
    than a caller's own `ScopeContext`. Both branches return a cached
    singleton (never a fresh object per call) so `InMemorySessionStore`'s
    rows actually persist across RPCs on the same pod, and so
    `PostgresSessionStore` instances aren't needlessly churned per request.
    """
    if is_enabled(DISABLE_SHARED_SESSIONS_FLAG, _SYSTEM_SCOPE):
        logger.warning(
            "penguincode.disable-shared-sessions is ON -- chat sessions are NOT "
            "shared across pods (legacy in-process behavior, O4-a reintroduced "
            "deliberately as an emergency rollback)"
        )
        return _shared_inmemory_store()
    return _shared_postgres_store(settings.sessions.postgres.url)


def reset_for_testing() -> None:
    """Drop every cached store singleton -- test isolation only."""
    global _inmemory_store_singleton
    with _inmemory_store_lock:
        _inmemory_store_singleton = None
    with _postgres_store_cache_lock:
        _postgres_store_cache.clear()


class SessionSweeper:
    """Background loop deleting expired `chat_sessions` rows and refreshing the gauge.

    Owned by `ChatServiceImpl` (lazily started on first RPC, see
    `server/services/chat.py`), not a standalone process -- every pod runs
    its own sweeper against the same shared store, so sweeps are naturally
    idempotent (a row another pod already deleted is just absent on the
    next sweep) and the `active_sessions` gauge is refreshed from every
    pod's own vantage point.
    """

    def __init__(self, store: SessionStore, *, interval_seconds: float, batch_size: int) -> None:
        """Bind this sweeper to `store`, running every `interval_seconds` in batches of `batch_size`."""
        self._store = store
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size

    async def run_forever(self) -> None:
        """Sweep expired rows and republish `active_sessions` every `interval_seconds`, forever.

        Never raises: a single sweep's failure (e.g. a transient DB outage)
        is logged and the loop continues on the next interval -- a sweeper
        crash must never take the whole chat service down with it.
        """
        while True:
            try:
                deleted = await asyncio.to_thread(
                    self._store.sweep_expired, batch_size=self._batch_size
                )
                if deleted:
                    logger.info("session sweeper: removed %d expired session(s)", deleted)
                active = await asyncio.to_thread(self._store.count_active)
                record_active_sessions(active)
            except Exception as exc:  # noqa: BLE001 -- a sweep failure must never kill the loop
                logger.warning("session sweeper iteration failed: %s", exc)
            await asyncio.sleep(self._interval_seconds)


__all__ = [
    "DISABLE_SHARED_SESSIONS_FLAG",
    "LEGACY_TENANT_ID",
    "InMemorySessionStore",
    "PostgresSessionStore",
    "SessionRecord",
    "SessionStore",
    "SessionSweeper",
    "create_session_store",
    "reset_for_testing",
]
