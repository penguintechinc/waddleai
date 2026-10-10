"""`IndexJobStore`: scope-aware persistence for `penguincode.index_jobs` (O10-a).

The sole read/write chokepoint for the `index_jobs` table (`db/migrations/
0008_index_jobs.sql`) -- `server/services/knowledge.py`'s `Index`/
`IndexCode`/`IndexStatus`/`ListIndexJobs` handlers call into this module
rather than issuing raw SQL, the same convention `lessons/store.py`'s
`PendingLessonStore` and every `stores/*.py` module already follows.
Opens a fresh connection per call (no pooling) -- mirrors `stores/graph.py`'s
`PostgresGraphStore` and `lessons/store.py`'s own documented choice; a pool
can be added later (see `db/pool.py`, owned by a sibling task) without
changing this public interface.

**Job visibility is tenant + owner, not the three-tier user/team/tenant
model** `VectorStore`/`GraphStore` use for row content -- see
`0008_index_jobs.sql`'s module comment for why. `get`/`list_jobs` therefore
always filter on both `ctx.tenant_id` and `ctx.user_id`; there is no
team- or tenant-wide job listing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.indexing.jobs import IndexJob, JobState, JobType
from penguincode_cli.observability.otel import store_span

_SELECT_COLUMNS = (
    "id, tenant_id, owner_user_id, job_type, state, chunks_done, chunks_total, "
    "result, error, created_at, updated_at"
)


@dataclass(slots=True, frozen=True)
class IndexJobOutcome:
    """A finished job's result, as the worker hands it back to the store.

    `chunks_done`/`chunks_total` are the docs-indexing progress counters;
    `extra` carries job-type-specific detail that doesn't fit those two
    ints (`IndexCode`'s `node_count`/`edge_count`) into the `result` jsonb
    column verbatim -- `server/services/knowledge.py` reads it back out by
    key when mapping a finished job to a proto response.
    """

    chunks_done: int = 0
    chunks_total: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _row_to_job(row: dict[str, Any]) -> IndexJob:
    return IndexJob(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        owner_user_id=str(row["owner_user_id"]),
        job_type=JobType(row["job_type"]),
        state=JobState(row["state"]),
        chunks_done=int(row["chunks_done"]),
        chunks_total=int(row["chunks_total"]),
        result=row["result"] or {},
        error=row["error"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


class IndexJobStoreLike(Protocol):
    """Structural match for `IndexJobStore`'s public methods.

    `server/services/knowledge.py` and `indexing/worker.py` type against
    this Protocol, not the concrete class -- mirrors `VectorStore`'s own
    Protocol-based test-double seam (`stores/vector.py`) so a fast unit
    test can inject an in-memory double with no live Postgres at all.
    """

    def create_queued(
        self,
        ctx: ScopeContext,
        job_type: JobType,
        *,
        chunks_total: int,
        team_id: str | None = None,
    ) -> str: ...

    def mark_running(self, job_id: str) -> None: ...
    def mark_succeeded(self, job_id: str, outcome: IndexJobOutcome) -> None: ...
    def mark_failed(self, job_id: str, error: str) -> None: ...
    def get(self, ctx: ScopeContext, job_id: str) -> IndexJob | None: ...
    def list_jobs(self, ctx: ScopeContext, *, limit: int = 20) -> list[IndexJob]: ...
    def reap_interrupted(self) -> int: ...


class IndexJobStore:
    """Scope-aware CRUD for `penguincode.index_jobs`. See module docstring."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create_queued(
        self,
        ctx: ScopeContext,
        job_type: JobType,
        *,
        chunks_total: int,
        team_id: str | None = None,
    ) -> str:
        """Insert a new `queued` row stamped from `ctx`. Returns the new job's id.

        `team_id`, when given, must be one of the caller's own teams -- the
        same defense-in-depth `stores.vector._validate_team_id` and
        `lessons.store.PendingLessonStore.create_pending` both apply; it is
        provenance only here (see module docstring), never a visibility
        filter.
        """
        if team_id is not None and team_id not in ctx.team_ids:
            raise ValueError(f"team_id {team_id!r} is not one of the caller's own teams")

        with store_span("index_jobs.create_queued", job_type=job_type.value):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        INSERT INTO penguincode.index_jobs
                            (tenant_id, org_id, team_id, owner_user_id, job_type,
                             chunks_total)
                        VALUES (
                            %(tenant_id)s, %(org_id)s, %(team_id)s,
                            %(owner_user_id)s, %(job_type)s, %(chunks_total)s
                        )
                        RETURNING id
                        """,
                        {
                            "tenant_id": ctx.tenant_id,
                            "org_id": ctx.org_id,
                            "team_id": team_id,
                            "owner_user_id": ctx.user_id,
                            "job_type": job_type.value,
                            "chunks_total": chunks_total,
                        },
                    )
                    row = cur.fetchone()

        assert row is not None  # INSERT ... RETURNING always yields exactly one row
        return str(row["id"])

    def mark_running(self, job_id: str) -> None:
        """Transition `job_id` to `running`. Called once, by the worker that dequeued it."""
        with store_span("index_jobs.mark_running", job_id=job_id):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    UPDATE penguincode.index_jobs
                    SET state = 'running', updated_at = now()
                    WHERE id = %(id)s::uuid
                    """,
                    {"id": job_id},
                )

    def mark_succeeded(self, job_id: str, outcome: IndexJobOutcome) -> None:
        """Transition `job_id` to `succeeded`, persisting its final counters/result."""
        with store_span("index_jobs.mark_succeeded", job_id=job_id):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    UPDATE penguincode.index_jobs
                    SET state = 'succeeded', chunks_done = %(chunks_done)s,
                        chunks_total = %(chunks_total)s, result = %(result)s,
                        updated_at = now()
                    WHERE id = %(id)s::uuid
                    """,
                    {
                        "id": job_id,
                        "chunks_done": outcome.chunks_done,
                        "chunks_total": outcome.chunks_total,
                        "result": Jsonb(outcome.extra),
                    },
                )

    def mark_failed(self, job_id: str, error: str) -> None:
        """Transition `job_id` to `failed`, recording `error` (truncated defensively)."""
        with store_span("index_jobs.mark_failed", job_id=job_id):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                conn.execute(
                    """
                    UPDATE penguincode.index_jobs
                    SET state = 'failed', error = %(error)s, updated_at = now()
                    WHERE id = %(id)s::uuid
                    """,
                    {"id": job_id, "error": error[:2000]},
                )

    def get(self, ctx: ScopeContext, job_id: str) -> IndexJob | None:
        """One job by id, scoped to `ctx`'s tenant + owner.

        Returns `None` both when the id does not exist at all and when it
        belongs to another tenant/owner -- indistinguishable by design, so
        a caller can never probe whether a given job id exists elsewhere
        (same non-leaking contract as `PendingLessonStore.get`).
        """
        with store_span("index_jobs.get", job_id=job_id):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                        FROM penguincode.index_jobs
                        WHERE id = %(id)s::uuid AND tenant_id = %(tenant_id)s
                            AND owner_user_id = %(owner_user_id)s
                        """,
                        {"id": job_id, "tenant_id": ctx.tenant_id, "owner_user_id": ctx.user_id},
                    )
                    row = cur.fetchone()

        return _row_to_job(row) if row is not None else None

    def list_jobs(self, ctx: ScopeContext, *, limit: int = 20) -> list[IndexJob]:
        """`ctx`'s own jobs (tenant + owner scoped), most recent first."""
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit!r}")

        with store_span("index_jobs.list", limit=limit):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                        FROM penguincode.index_jobs
                        WHERE tenant_id = %(tenant_id)s
                            AND owner_user_id = %(owner_user_id)s
                        ORDER BY created_at DESC
                        LIMIT %(limit)s
                        """,
                        {"tenant_id": ctx.tenant_id, "owner_user_id": ctx.user_id, "limit": limit},
                    )
                    rows = cur.fetchall()

        return [_row_to_job(row) for row in rows]

    def reap_interrupted(self) -> int:
        """Mark every still-`running` row `failed` (reason `"interrupted"`).

        Called once at server startup, before the gRPC server starts
        accepting traffic (see `server/main.py`) -- a `running` row at boot
        can only mean the worker that owned it died with the previous
        process, so its progress is unknown and it is never re-queued (see
        `0008_index_jobs.sql`'s module comment). Not scope-filtered: this is
        process-wide startup maintenance, not a request-scoped read.
        Returns the number of rows reaped.
        """
        with store_span("index_jobs.reap_interrupted"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE penguincode.index_jobs
                        SET state = 'failed', error = 'interrupted', updated_at = now()
                        WHERE state = 'running'
                        """
                    )
                    return cur.rowcount
