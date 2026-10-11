"""Shared, bounded Postgres connection pool for penguincode's vector/graph stores.

Before this module, every `stores.vector.PgVectorStore` / `stores.graph.
PostgresGraphStore` call opened a fresh `psycopg.connect()` per query
(ops-audit O7: "Every vector/graph store call opens a fresh
`psycopg.connect()` ... no pool"). Under load this churns connections
against Postgres's `max_connections` ceiling, which can starve unrelated
consumers of the same shared database (e.g. `services/management`'s
SQLAlchemy pool) -- a connection-exhaustion cascade into the control plane.

This module owns exactly ONE process-wide `psycopg_pool.ConnectionPool`,
sized and timed out from `config.settings.DbConfig` (env-driven, see that
class's docstring), opened once at server startup (`server/main.py`) and
closed on shutdown. `stores.vector`/`stores.graph` borrow a connection via
:func:`connection` instead of calling `psycopg.connect()` directly -- the
pool itself is otherwise invisible to them (same `with ... as conn:` call
shape as before).

**Kill-switch (`flags.client.DISABLE_DB_POOL_FLAG`).** Unseen/OFF (default)
= pooled connections. ON = legacy per-call `psycopg.connect()`, exactly the
pre-fix behavior -- an operational escape hatch if the pool itself
misbehaves in some environment, evaluated per-call (every store method
already carries a `ScopeContext`, so no separate plumbing is needed to
reach the flag client).

**Statement timeout.** Every pooled *physical* connection has
`SET statement_timeout = <DbConfig.statement_timeout_ms>` applied once, via
the pool's `configure` callback (run only when a new physical connection is
created, not on every borrow -- the GUC is session-scoped and persists for
the connection's lifetime). This kills a runaway traversal/query
server-side rather than letting a borrower hang indefinitely. The
kill-switch's legacy `psycopg.connect()` path does NOT get a statement
timeout -- it is a deliberate full revert to pre-fix behavior, not a hybrid.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

from penguincode_cli.config.settings import DbConfig
from penguincode_cli.flags.client import DISABLE_DB_POOL_FLAG, ScopeContextLike, is_enabled
from penguincode_cli.observability.otel import record_pool_wait_duration, update_pool_gauges

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_shared_pool: ConnectionPool | None = None


def _configure_statement_timeout(
    statement_timeout_ms: int,
) -> Callable[[psycopg.Connection], None]:
    """Build a pool `configure` callback that sets `statement_timeout` once per physical connection."""

    def _configure(conn: psycopg.Connection) -> None:
        conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        conn.commit()

    return _configure


def open_pool(dsn: str, config: DbConfig | None = None) -> ConnectionPool:
    """Idempotently open the process-wide shared pool bound to `dsn`.

    Safe to call more than once: a second+ call is a no-op that returns the
    already-open pool unchanged (`server/main.py`'s startup calls this once;
    `get_pool` below calls it lazily for CLI/test code paths that never went
    through server startup). `config` is only consulted on the call that
    actually opens the pool.
    """
    global _shared_pool
    with _lock:
        if _shared_pool is not None:
            return _shared_pool
        cfg = config or DbConfig()
        pool = ConnectionPool(
            dsn,
            min_size=cfg.pool_min_size,
            max_size=cfg.pool_max_size,
            timeout=cfg.pool_timeout_seconds,
            configure=_configure_statement_timeout(cfg.statement_timeout_ms),
            open=False,
        )
        pool.open(wait=False)
        _shared_pool = pool
        logger.info(
            "db.pool opened: min_size=%d max_size=%d timeout_s=%.1f statement_timeout_ms=%d",
            cfg.pool_min_size,
            cfg.pool_max_size,
            cfg.pool_timeout_seconds,
            cfg.statement_timeout_ms,
        )
        return pool


def is_pool_open() -> bool:
    """Report whether the process-wide shared pool is currently open.

    Non-raising, read-only check used by `server/grpc_health.py` to derive
    the standard `grpc.health.v1.Health` service's per-service SERVING/
    NOT_SERVING status for `KnowledgeService`/`LessonsService` -- both are
    backed by this pool, so a never-opened or already-closed pool means
    they cannot actually serve traffic even though the gRPC channel itself
    is still reachable.
    """
    return _shared_pool is not None


def get_pool(dsn: str | None = None, config: DbConfig | None = None) -> ConnectionPool:
    """Return the shared pool, lazily opening it from `dsn`/env defaults if not already open."""
    if _shared_pool is not None:
        return _shared_pool
    resolved_dsn = dsn if dsn is not None else os.environ.get("PGVECTOR_URL", "")
    return open_pool(resolved_dsn, config)


def close_pool(timeout: float = 5.0) -> None:
    """Idempotently close the shared pool (server shutdown; test teardown). No-op if never opened."""
    global _shared_pool
    with _lock:
        if _shared_pool is None:
            return
        pool, _shared_pool = _shared_pool, None
    pool.close(timeout=timeout)
    logger.info("db.pool closed")


@contextmanager
def connection(
    dsn: str,
    ctx: ScopeContextLike,
    *,
    pool: ConnectionPool | None = None,
    autocommit: bool = False,
) -> Iterator[psycopg.Connection]:
    """Borrow one connection for a single store call -- pooled by default.

    Drop-in replacement for `psycopg.connect(dsn, autocommit=autocommit)`:
    same `with connection(...) as conn:` shape, same per-call `autocommit`
    control. Default path borrows from the shared pool (`pool`, or the
    process-wide singleton via `get_pool(dsn)` when `pool` is `None`),
    recording the borrow-wait latency and in-flight/waiting-borrower gauges.
    Falls back to a direct, unpooled `psycopg.connect()` -- the exact
    pre-fix behavior -- when `flags.client.DISABLE_DB_POOL_FLAG` is on for
    `ctx`'s tenant.
    """
    if is_enabled(DISABLE_DB_POOL_FLAG, ctx):
        with psycopg.connect(dsn, autocommit=autocommit) as conn:
            yield conn
        return

    active_pool = pool if pool is not None else get_pool(dsn)
    start = time.perf_counter()
    try:
        with active_pool.connection() as conn:
            record_pool_wait_duration((time.perf_counter() - start) * 1000)
            stats = active_pool.get_stats()
            update_pool_gauges(
                stats.get("pool_size", 0),
                stats.get("pool_available", 0),
                stats.get("requests_waiting", 0),
            )
            conn.autocommit = autocommit
            yield conn
    except PoolTimeout:
        record_pool_wait_duration((time.perf_counter() - start) * 1000)
        raise
    finally:
        stats = active_pool.get_stats()
        update_pool_gauges(
            stats.get("pool_size", 0),
            stats.get("pool_available", 0),
            stats.get("requests_waiting", 0),
        )


__all__ = ["open_pool", "get_pool", "close_pool", "connection"]
