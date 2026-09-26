"""Tests for ``penguincode_cli.lessons.store`` -- PendingLessonStore.

TDD: written before ``penguincode_cli/lessons/store.py`` existed; must fail
with an ImportError/ModuleNotFoundError until the module is implemented.

Static tests (no DB needed) always run. The live-Postgres tests connect to
``TEST_DATABASE_URL`` and are skipped -- with an explicit reason, never
silently -- when that env var is unset, mirroring
``tests/test_stores_vector.py``/``tests/test_db_migrate.py``.

# regression: lessons-promotion (T-L2a -- pending-lessons store)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.lessons.scrub import Finding, IssueKind
from penguincode_cli.lessons.store import PendingLessonRecord, PendingLessonStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres lessons store tests are CI-pending",
)


def _ctx(
    *,
    tenant_id: str,
    org_id: str | None = None,
    team_ids: tuple[str, ...] = (),
    user_id: str | None = None,
    scopes: tuple[str, ...] = (),
) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id or str(uuid.uuid4()),
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# Static tests: validation that does not require a live DB.
# ---------------------------------------------------------------------------


class TestPendingLessonStoreCreateValidation:
    """Validation that must reject before ever opening a DB connection."""

    def test_rejects_source_team_id_not_in_callers_own_teams(self) -> None:
        store = PendingLessonStore(dsn="postgresql://unreachable-host-for-test/db")
        ctx = _ctx(tenant_id="t1", team_ids=("team-a",))
        with pytest.raises(ValueError, match="team"):
            store.create_pending(ctx, "a generalized lesson", [], "team-not-mine")


class TestPendingLessonStoreListValidation:
    def test_rejects_unknown_status(self) -> None:
        store = PendingLessonStore(dsn="postgresql://unreachable-host-for-test/db")
        with pytest.raises(ValueError, match="status"):
            store.list_pending(_ctx(tenant_id="t1"), status="not-a-status")


class TestPendingLessonStoreSetStatusValidation:
    def test_rejects_status_back_to_pending(self) -> None:
        """`set_status` only accepts the two terminal review outcomes ("pending" is
        the row's initial state only, never a valid target of a review action)."""
        store = PendingLessonStore(dsn="postgresql://unreachable-host-for-test/db")
        with pytest.raises(ValueError, match="status"):
            store.set_status(
                _ctx(tenant_id="t1"), str(uuid.uuid4()), "pending", reviewer=str(uuid.uuid4())
            )

    def test_rejects_completely_unknown_status(self) -> None:
        store = PendingLessonStore(dsn="postgresql://unreachable-host-for-test/db")
        with pytest.raises(ValueError, match="status"):
            store.set_status(
                _ctx(tenant_id="t1"), str(uuid.uuid4()), "not-a-status", reviewer=str(uuid.uuid4())
            )


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
class TestPendingLessonStoreRoundTrip:
    def test_create_then_get_round_trips_every_field(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        team_id = str(uuid.uuid4())
        ctx = _ctx(tenant_id=str(uuid.uuid4()), org_id=str(uuid.uuid4()), team_ids=(team_id,))

        pending_id = store.create_pending(ctx, "a generalized, client-agnostic lesson", [], team_id)
        record = store.get(ctx, pending_id)

        assert record is not None
        assert isinstance(record, PendingLessonRecord)
        assert record.id == pending_id
        assert record.tenant_id == ctx.tenant_id
        assert record.org_id == ctx.org_id
        assert record.source_team_id == team_id
        assert record.proposer_user_id == ctx.user_id
        assert record.generalized_text == "a generalized, client-agnostic lesson"
        assert record.status == "pending"
        assert record.findings == []
        assert record.reviewed_by is None
        assert record.reviewed_at is None

    def test_create_persists_findings_as_audit_trail(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        findings = [Finding(kind=IssueKind.EMAIL, detail="residual email address detected")]

        pending_id = store.create_pending(ctx, "lesson text", findings, None)
        record = store.get(ctx, pending_id)

        assert record is not None
        assert len(record.findings) == 1
        assert record.findings[0].kind == "email"
        assert record.findings[0].detail == "residual email address detected"

    def test_create_without_team_id_is_allowed(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))

        pending_id = store.create_pending(ctx, "lesson text", [], None)
        record = store.get(ctx, pending_id)

        assert record is not None
        assert record.source_team_id is None

    def test_get_unknown_id_returns_none(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        assert store.get(ctx, str(uuid.uuid4())) is None

    def test_list_pending_defaults_to_pending_status_oldest_first(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))

        first_id = store.create_pending(ctx, "first lesson", [], None)
        second_id = store.create_pending(ctx, "second lesson", [], None)

        results = store.list_pending(ctx)
        assert [r.id for r in results] == [first_id, second_id]
        assert all(r.status == "pending" for r in results)

    def test_list_pending_excludes_approved_and_rejected(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        reviewer = str(uuid.uuid4())

        approved_id = store.create_pending(ctx, "will be approved", [], None)
        rejected_id = store.create_pending(ctx, "will be rejected", [], None)
        still_pending_id = store.create_pending(ctx, "still pending", [], None)

        store.set_status(ctx, approved_id, "approved", reviewer=reviewer)
        store.set_status(ctx, rejected_id, "rejected", reviewer=reviewer, reason="names the client")

        results = store.list_pending(ctx, status="pending")
        assert [r.id for r in results] == [still_pending_id]

    def test_approve_stamps_reviewer_and_reviewed_at(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        reviewer = str(uuid.uuid4())

        pending_id = store.create_pending(ctx, "lesson text", [], None)
        store.set_status(ctx, pending_id, "approved", reviewer=reviewer)

        record = store.get(ctx, pending_id)
        assert record is not None
        assert record.status == "approved"
        assert record.reviewed_by == reviewer
        assert record.reviewed_at is not None

    def test_reject_with_reason_appends_to_findings_audit_trail(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        reviewer = str(uuid.uuid4())

        pending_id = store.create_pending(ctx, "lesson text", [], None)
        store.set_status(ctx, pending_id, "rejected", reviewer=reviewer, reason="still identifying")

        record = store.get(ctx, pending_id)
        assert record is not None
        assert record.status == "rejected"
        assert record.reviewed_by == reviewer
        assert any(
            f.kind == "rejection_reason" and f.detail == "still identifying"
            for f in record.findings
        )

    def test_set_status_on_already_reviewed_row_raises_lookup_error(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        reviewer = str(uuid.uuid4())

        pending_id = store.create_pending(ctx, "lesson text", [], None)
        store.set_status(ctx, pending_id, "approved", reviewer=reviewer)

        with pytest.raises(LookupError):
            store.set_status(ctx, pending_id, "rejected", reviewer=reviewer)

    def test_set_status_on_unknown_id_raises_lookup_error(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        with pytest.raises(LookupError):
            store.set_status(ctx, str(uuid.uuid4()), "approved", reviewer=str(uuid.uuid4()))


@requires_postgres
class TestPendingLessonStoreScopeIsolation:
    def test_list_pending_is_tenant_scoped(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        tenant_a = _ctx(tenant_id=str(uuid.uuid4()))
        tenant_b = _ctx(tenant_id=str(uuid.uuid4()))

        store.create_pending(tenant_a, "tenant A's lesson", [], None)

        assert len(store.list_pending(tenant_a)) == 1
        assert store.list_pending(tenant_b) == []

    def test_get_cannot_read_another_tenants_row(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        tenant_a = _ctx(tenant_id=str(uuid.uuid4()))
        tenant_b = _ctx(tenant_id=str(uuid.uuid4()))

        pending_id = store.create_pending(tenant_a, "tenant A's lesson", [], None)

        assert store.get(tenant_a, pending_id) is not None
        assert store.get(tenant_b, pending_id) is None

    def test_list_pending_is_not_restricted_by_source_team(self, live_dsn: str) -> None:
        """Review is tenant-wide -- a reviewer in team B still sees team A's pending lesson."""
        store = PendingLessonStore(dsn=live_dsn)
        tenant_id = str(uuid.uuid4())
        team_a = str(uuid.uuid4())
        proposer_ctx = _ctx(tenant_id=tenant_id, team_ids=(team_a,))
        reviewer_ctx = _ctx(tenant_id=tenant_id, team_ids=(str(uuid.uuid4()),))

        store.create_pending(proposer_ctx, "team A's lesson", [], team_a)

        assert len(store.list_pending(reviewer_ctx)) == 1

    def test_set_status_cannot_reach_across_tenants(self, live_dsn: str) -> None:
        store = PendingLessonStore(dsn=live_dsn)
        tenant_a = _ctx(tenant_id=str(uuid.uuid4()))
        tenant_b = _ctx(tenant_id=str(uuid.uuid4()))

        pending_id = store.create_pending(tenant_a, "tenant A's lesson", [], None)

        with pytest.raises(LookupError):
            store.set_status(tenant_b, pending_id, "approved", reviewer=str(uuid.uuid4()))

        # Untouched by the other tenant's failed attempt.
        record = store.get(tenant_a, pending_id)
        assert record is not None
        assert record.status == "pending"
