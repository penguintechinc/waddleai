"""Live-Postgres tests for `penguincode_cli.indexing.store.IndexJobStore` (O10-a).

Mirrors `tests/test_lessons_store.py`'s pattern: `TEST_DATABASE_URL`-gated,
skipped with an explicit reason when unset. Proves the real SQL scope
filter (tenant + owner) the mocked worker/queue tests above cannot --
`stores.vector`-style isolation, scoped to `index_jobs` instead.

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.db.migrate import run_migrations
from penguincode_cli.indexing.jobs import JobState, JobType
from penguincode_cli.indexing.store import IndexJobOutcome, IndexJobStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres index-job store tests are CI-pending",
)


def _ctx(*, tenant_id: str | None = None, user_id: str | None = None) -> ScopeContext:
    return ScopeContext(
        tenant_id=tenant_id or str(uuid.uuid4()),
        org_id=None,
        team_ids=(),
        user_id=user_id or str(uuid.uuid4()),
        scopes=(),
    )


@pytest.fixture
def live_dsn() -> Iterator[str]:
    """Fresh, migrated `penguincode` schema for every test."""
    assert TEST_DATABASE_URL is not None  # narrows type; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    run_migrations(dsn=TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


class TestCreateValidation:
    def test_rejects_team_id_not_in_callers_own_teams(self) -> None:
        store = IndexJobStore(dsn="postgresql://unreachable-host-for-test/db")
        ctx = _ctx()
        with pytest.raises(ValueError, match="team"):
            store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1, team_id="not-mine")


class TestListJobsValidation:
    def test_rejects_zero_limit(self) -> None:
        store = IndexJobStore(dsn="postgresql://unreachable-host-for-test/db")
        with pytest.raises(ValueError, match="limit"):
            store.list_jobs(_ctx(), limit=0)

    def test_rejects_negative_limit(self) -> None:
        store = IndexJobStore(dsn="postgresql://unreachable-host-for-test/db")
        with pytest.raises(ValueError, match="limit"):
            store.list_jobs(_ctx(), limit=-1)


@requires_postgres
class TestCreateGetRoundTrip:
    def test_create_then_get_is_queued_with_chunks_total(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()

        job_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=5)
        job = store.get(ctx, job_id)

        assert job is not None
        assert job.id == job_id
        assert job.job_type is JobType.INDEX_DOCS
        assert job.state is JobState.QUEUED
        assert job.chunks_total == 5
        assert job.chunks_done == 0
        assert job.tenant_id == ctx.tenant_id
        assert job.owner_user_id == ctx.user_id

    def test_mark_running_then_succeeded_persists_final_result(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()
        job_id = store.create_queued(ctx, JobType.INDEX_CODE, chunks_total=0)

        store.mark_running(job_id)
        running = store.get(ctx, job_id)
        assert running is not None
        assert running.state is JobState.RUNNING

        store.mark_succeeded(
            job_id, IndexJobOutcome(extra={"indexed": True, "node_count": 4, "edge_count": 6})
        )
        done = store.get(ctx, job_id)
        assert done is not None
        assert done.state is JobState.SUCCEEDED
        assert done.result == {"indexed": True, "node_count": 4, "edge_count": 6}

    def test_mark_failed_persists_error(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()
        job_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1)

        store.mark_failed(job_id, "ollama unreachable")

        job = store.get(ctx, job_id)
        assert job is not None
        assert job.state is JobState.FAILED
        assert job.error == "ollama unreachable"


@requires_postgres
class TestScopeIsolation:
    def test_get_returns_none_for_a_different_tenant(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        tenant_a_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        tenant_b_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        job_id = store.create_queued(tenant_a_ctx, JobType.INDEX_DOCS, chunks_total=1)

        assert store.get(tenant_b_ctx, job_id) is None
        assert store.get(tenant_a_ctx, job_id) is not None

    def test_get_returns_none_for_a_different_owner_same_tenant(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        tenant = str(uuid.uuid4())
        owner_a = _ctx(tenant_id=tenant, user_id=str(uuid.uuid4()))
        owner_b = _ctx(tenant_id=tenant, user_id=str(uuid.uuid4()))
        job_id = store.create_queued(owner_a, JobType.INDEX_DOCS, chunks_total=1)

        assert store.get(owner_b, job_id) is None
        assert store.get(owner_a, job_id) is not None

    def test_list_jobs_never_leaks_another_tenants_rows(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        tenant_a_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        tenant_b_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        store.create_queued(tenant_a_ctx, JobType.INDEX_DOCS, chunks_total=1)
        store.create_queued(tenant_a_ctx, JobType.INDEX_DOCS, chunks_total=1)

        assert store.list_jobs(tenant_b_ctx) == []
        assert len(store.list_jobs(tenant_a_ctx)) == 2

    def test_list_jobs_most_recent_first_and_respects_limit(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()
        ids = [store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1) for _ in range(3)]

        jobs = store.list_jobs(ctx, limit=2)

        assert len(jobs) == 2
        assert [j.id for j in jobs] == list(reversed(ids))[:2]


@requires_postgres
class TestReapInterrupted:
    def test_reaps_only_running_rows_leaves_others_untouched(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()
        running_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1)
        store.mark_running(running_id)
        queued_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1)
        succeeded_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1)
        store.mark_succeeded(succeeded_id, IndexJobOutcome(chunks_done=1, chunks_total=1))

        reaped = store.reap_interrupted()

        assert reaped == 1
        running_after = store.get(ctx, running_id)
        assert running_after is not None
        assert running_after.state is JobState.FAILED
        assert running_after.error == "interrupted"
        assert store.get(ctx, queued_id).state is JobState.QUEUED  # type: ignore[union-attr]
        assert store.get(ctx, succeeded_id).state is JobState.SUCCEEDED  # type: ignore[union-attr]

    def test_reaps_zero_when_nothing_is_running(self, live_dsn: str) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = _ctx()
        store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=1)

        assert store.reap_interrupted() == 0


@requires_postgres
class TestNonUuidScopeIds:
    """Regression: `0008_index_jobs.sql` typed `tenant_id`/`org_id`/`team_id`/
    `owner_user_id` as `uuid`, but WaddleAI's real JWT claims are opaque
    strings -- in production, the stringified integer `organizations.id`
    primary key (see `shared/auth/penguin_auth.py`), never a UUID. Every
    other test in this file uses `str(uuid.uuid4())` fixtures, which never
    exercised this -- this test deliberately uses non-UUID scope ids to
    prove migration `0009_index_jobs_scope_text.sql` actually fixed the
    column types, not just that UUID-shaped strings happen to work.

    # regression: penguincode-index-jobs-scope-text (0009 -- uuid-vs-text tenant_id)
    """

    def test_create_get_and_list_round_trip_with_integer_like_scope_ids(
        self, live_dsn: str
    ) -> None:
        store = IndexJobStore(dsn=live_dsn)
        ctx = ScopeContext(
            tenant_id="42",
            org_id="7",
            team_ids=("3",),
            user_id="user-not-a-uuid",
            scopes=(),
        )

        job_id = store.create_queued(ctx, JobType.INDEX_DOCS, chunks_total=2, team_id="3")
        job = store.get(ctx, job_id)

        assert job is not None
        assert job.tenant_id == "42"
        assert job.owner_user_id == "user-not-a-uuid"

        jobs = store.list_jobs(ctx)
        assert [j.id for j in jobs] == [job_id]

        other_tenant_ctx = ScopeContext(
            tenant_id="99", org_id=None, team_ids=(), user_id="someone-else", scopes=()
        )
        assert store.get(other_tenant_ctx, job_id) is None
        assert store.list_jobs(other_tenant_ctx) == []
