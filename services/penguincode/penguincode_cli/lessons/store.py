"""PendingLessonStore: scope-aware persistence for the lessons-promotion review queue (T-L2a).

Backs the ``penguincode.pending_lessons`` table (``db/migrations/
0006_pending_lessons.sql``): the review queue a team-visibility "lesson
learned" sits in after passing ``lessons/scrub.py``'s (T-L1)
``generalize_and_scrub``/``verify_scrubbed`` pipeline, awaiting a human
reviewer's approval before it is ever written firm-wide (tenant
visibility). This module is the sole read/write chokepoint for that table
-- T-L2b's gRPC handlers call into it rather than issuing raw SQL, the same
convention every other store in this package (``VectorStore``,
``GraphStore``) already follows.

**Review is tenant-wide, never team-scoped.** Unlike ``VectorStore``/
``GraphStore``'s three-tier (user/team/tenant) *read* visibility, a pending
lesson has no per-row visibility of its own while under review: any
reviewer within the proposing tenant can see and act on any pending lesson
in that tenant, regardless of which team originally proposed it -- because
the entire point of the workflow is deciding whether to share it *beyond*
that team, firm-wide. ``source_team_id``/``proposer_user_id`` therefore
record *provenance* only (who/which team proposed it, for the reviewer's
context and the audit trail), not a read filter. The one hard boundary
enforced everywhere below is the tenant: every read and write is scoped to
``ctx.tenant_id``, exactly as every other store in this package enforces
the same hard boundary -- a pending lesson can never be listed, fetched, or
reviewed from outside its own tenant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.lessons.scrub import Finding
from penguincode_cli.observability.otel import timed_store_operation

#: The two terminal review outcomes `set_status` may transition a row to --
#: a row can never be moved back to "pending" through this API (that is
#: the row's initial state only, set by `create_pending`).
_VALID_REVIEW_STATUSES: frozenset[str] = frozenset({"approved", "rejected"})

#: Every status `list_pending`'s `status` filter accepts.
_VALID_LIST_STATUSES: frozenset[str] = frozenset({"pending", "approved", "rejected"})


@dataclass(slots=True, frozen=True)
class FindingRecord:
    """One finding as persisted in ``pending_lessons.findings``.

    Deliberately a plain ``(kind, detail)`` string pair, decoupled from
    ``lessons.scrub.Finding``'s closed ``IssueKind`` enum -- the persisted
    audit trail also carries entries `scrub.py` never produces (e.g. a
    reviewer's rejection reason, see `PendingLessonStore.set_status`), so
    constraining it to `IssueKind`'s value set would be wrong here.
    """

    kind: str
    detail: str


@dataclass(slots=True, frozen=True)
class PendingLessonRecord:
    """One ``pending_lessons`` row, as read back by `PendingLessonStore`.

    Deliberately distinct from ``proto.lessons.v1``'s ``PendingLesson`` wire
    message -- T-L2b's gRPC handler maps between the two; this dataclass is
    the DB-facing shape only, with no protobuf dependency.
    """

    id: str
    tenant_id: str
    org_id: str | None
    source_team_id: str | None
    proposer_user_id: str | None
    generalized_text: str
    status: str
    findings: list[FindingRecord]
    reviewed_by: str | None
    reviewed_at: str | None
    created_at: str


def _serialize_findings(findings: Sequence[Finding]) -> list[dict[str, str]]:
    """`Finding` -> the plain-dict shape stored in the `findings` jsonb column."""
    return [{"kind": finding.kind.value, "detail": finding.detail} for finding in findings]


def _deserialize_findings(raw: list[dict[str, Any]]) -> list[FindingRecord]:
    return [FindingRecord(kind=str(item["kind"]), detail=str(item["detail"])) for item in raw]


def _row_to_record(row: dict[str, Any]) -> PendingLessonRecord:
    return PendingLessonRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        org_id=str(row["org_id"]) if row["org_id"] is not None else None,
        source_team_id=(str(row["source_team_id"]) if row["source_team_id"] is not None else None),
        proposer_user_id=(
            str(row["proposer_user_id"]) if row["proposer_user_id"] is not None else None
        ),
        generalized_text=row["generalized_text"],
        status=row["status"],
        findings=_deserialize_findings(row["findings"]),
        reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] is not None else None,
        reviewed_at=(row["reviewed_at"].isoformat() if row["reviewed_at"] is not None else None),
        created_at=row["created_at"].isoformat(),
    )


_SELECT_COLUMNS = (
    "id, tenant_id, org_id, source_team_id, proposer_user_id, "
    "generalized_text, status, findings, reviewed_by, reviewed_at, created_at"
)


class PendingLessonStore:
    """Scope-aware persistence for the ``penguincode.pending_lessons`` review queue.

    See the module docstring for the tenant-hard-boundary / tenant-wide-
    review contract every method below enforces.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def create_pending(
        self,
        ctx: ScopeContext,
        generalized_text: str,
        findings: Sequence[Finding],
        source_team_id: str | None,
    ) -> str:
        """Insert a new PENDING lesson, stamping tenant/org/proposer from `ctx`.

        `findings` is normally empty -- only a CLEAN `ScrubResult` (see
        `lessons.scrub`) is ever promoted to a pending row in the first
        place -- but is persisted as an audit trail regardless of length.
        `source_team_id`, when given, must be one of the caller's own teams
        (the same defense-in-depth pattern `stores.vector._validate_team_id`
        uses): a caller can never attribute a pending lesson to a team it is
        not itself a member of. Returns the new row's id.
        """
        if source_team_id is not None and source_team_id not in ctx.team_ids:
            raise ValueError(
                f"source_team_id {source_team_id!r} is not one of the caller's own teams"
            )

        with timed_store_operation("vector_query", "pending_lessons.create", backend="postgres"):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        """
                        INSERT INTO penguincode.pending_lessons
                            (tenant_id, org_id, source_team_id, proposer_user_id,
                             generalized_text, findings)
                        VALUES (
                            %(tenant_id)s::uuid, %(org_id)s::uuid, %(source_team_id)s::uuid,
                            %(proposer_user_id)s::uuid, %(generalized_text)s, %(findings)s
                        )
                        RETURNING id
                        """,
                        {
                            "tenant_id": ctx.tenant_id,
                            "org_id": ctx.org_id,
                            "source_team_id": source_team_id,
                            "proposer_user_id": ctx.user_id,
                            "generalized_text": generalized_text,
                            "findings": Jsonb(_serialize_findings(findings)),
                        },
                    )
                    row = cur.fetchone()

        assert row is not None  # INSERT ... RETURNING always yields exactly one row
        return str(row["id"])

    def list_pending(
        self, ctx: ScopeContext, *, status: str = "pending"
    ) -> list[PendingLessonRecord]:
        """Every row in `ctx`'s tenant with the given `status`, oldest first.

        Tenant-scoped only -- never filtered by team or proposer, see this
        module's docstring on why review is a tenant-wide action.
        """
        if status not in _VALID_LIST_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_LIST_STATUSES)}, got {status!r}"
            )

        with timed_store_operation(
            "vector_query", "pending_lessons.list", backend="postgres", status=status
        ):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                        FROM penguincode.pending_lessons
                        WHERE tenant_id = %(tenant_id)s::uuid AND status = %(status)s
                        ORDER BY created_at ASC
                        """,
                        {"tenant_id": ctx.tenant_id, "status": status},
                    )
                    rows = cur.fetchall()

        return [_row_to_record(row) for row in rows]

    def get(self, ctx: ScopeContext, pending_id: str) -> PendingLessonRecord | None:
        """One row by id, scoped to `ctx`'s tenant.

        Returns `None` both when the id does not exist at all and when it
        belongs to a different tenant -- the two cases are indistinguishable
        by design, so a caller can never use this method to probe whether a
        given id exists in someone else's tenant.
        """
        with timed_store_operation("vector_query", "pending_lessons.get", backend="postgres"):
            with psycopg.connect(self._dsn) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        f"""
                        SELECT {_SELECT_COLUMNS}
                        FROM penguincode.pending_lessons
                        WHERE tenant_id = %(tenant_id)s::uuid AND id = %(id)s::uuid
                        """,
                        {"tenant_id": ctx.tenant_id, "id": pending_id},
                    )
                    row = cur.fetchone()

        return _row_to_record(row) if row is not None else None

    def set_status(
        self,
        ctx: ScopeContext,
        pending_id: str,
        status: str,
        *,
        reviewer: str,
        reason: str | None = None,
    ) -> None:
        """Transition a PENDING row to `status` ("approved"/"rejected").

        Stamps `reviewed_by`/`reviewed_at`. Tenant-scoped, and only a row
        still in "pending" status may transition -- both the tenant filter
        and the ``status = 'pending'`` guard are enforced in the same SQL
        statement (never a separate read-then-write), so this is race-free
        against a concurrent reviewer and can never re-review an
        already-decided row or reach across tenants.

        `reason`, when given, is appended to the row's `findings` audit
        trail as a ``{"kind": "rejection_reason", "detail": reason}`` entry
        -- `pending_lessons` has no dedicated reason column (see the
        migration's module docstring for why), so this keeps the review
        rationale on the row without a schema change.

        Raises `LookupError` if no row matched (id not found, wrong tenant,
        or already reviewed) -- T-L2b's RPC handler is responsible for
        mapping that to the appropriate gRPC status.
        """
        if status not in _VALID_REVIEW_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_REVIEW_STATUSES)}, got {status!r}"
            )

        extra_findings = [{"kind": "rejection_reason", "detail": reason}] if reason else []

        with timed_store_operation(
            "vector_query", "pending_lessons.set_status", backend="postgres", status=status
        ):
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE penguincode.pending_lessons
                        SET status = %(status)s,
                            reviewed_by = %(reviewer)s::uuid,
                            reviewed_at = now(),
                            findings = findings || %(extra_findings)s::jsonb
                        WHERE tenant_id = %(tenant_id)s::uuid
                          AND id = %(id)s::uuid
                          AND status = 'pending'
                        """,
                        {
                            "status": status,
                            "reviewer": reviewer,
                            "extra_findings": Jsonb(extra_findings),
                            "tenant_id": ctx.tenant_id,
                            "id": pending_id,
                        },
                    )
                    if cur.rowcount == 0:
                        raise LookupError(
                            f"no pending lesson {pending_id!r} found in tenant "
                            f"{ctx.tenant_id!r} awaiting review"
                        )
