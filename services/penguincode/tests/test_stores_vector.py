"""Tests for ``penguincode_cli.stores.vector`` -- VectorStore / PgVectorStore.

TDD: written before ``penguincode_cli/stores/vector.py`` exists; must fail
with an ImportError/ModuleNotFoundError until the module is implemented.

Static tests (no DB needed) always run. The live-Postgres tests connect to
``TEST_DATABASE_URL`` and are skipped -- with an explicit reason, never
silently -- when that env var is unset, mirroring ``tests/test_db_migrate.py``.

# regression: penguincode-knowledge-platform (T6 -- VectorStore foundation)
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from db.migrate import run_migrations
from penguincode_cli.auth.scope import ScopeContext
from penguincode_cli.stores.vector import PgVectorStore, VectorHit, VectorItem, VectorStore

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres vector store tests are CI-pending (T16)",
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


def _embedding(seed: float = 1.0) -> list[float]:
    """A deterministic 768-dim embedding, matching the migrated column width."""
    vec = [0.0] * 768
    vec[0] = seed
    return vec


# ---------------------------------------------------------------------------
# Static tests: construction/validation that does not require a live DB.
# ---------------------------------------------------------------------------


class TestPgVectorStoreConstruction:
    def test_implements_vector_store_protocol(self) -> None:
        store: VectorStore = PgVectorStore(dsn="postgresql://fake/db")
        assert isinstance(store, PgVectorStore)

    def test_default_table_is_docs_vectors(self) -> None:
        store = PgVectorStore(dsn="postgresql://fake/db")
        assert store.table == "docs_vectors"

    def test_memory_vectors_table_selectable(self) -> None:
        store = PgVectorStore(dsn="postgresql://fake/db", table="memory_vectors")
        assert store.table == "memory_vectors"

    def test_rejects_unknown_table_name(self) -> None:
        with pytest.raises(ValueError, match="table"):
            PgVectorStore(dsn="postgresql://fake/db", table="not_a_real_table")  # type: ignore[arg-type]


class TestPgVectorStoreUpsertValidation:
    """Validation that must reject before ever opening a DB connection."""

    def test_empty_items_is_a_noop_without_connecting(self) -> None:
        store = PgVectorStore(dsn="postgresql://unreachable-host-for-test/db")
        store.upsert(_ctx(tenant_id="t1"), [], visibility="tenant", team_id=None)

    def test_rejects_unknown_visibility(self) -> None:
        store = PgVectorStore(dsn="postgresql://unreachable-host-for-test/db")
        item = VectorItem(id=str(uuid.uuid4()), embedding=_embedding(), document="doc")
        with pytest.raises(ValueError, match="visibility"):
            store.upsert(_ctx(tenant_id="t1"), [item], visibility="global", team_id=None)

    def test_rejects_team_id_not_in_callers_own_teams(self) -> None:
        store = PgVectorStore(dsn="postgresql://unreachable-host-for-test/db")
        item = VectorItem(id=str(uuid.uuid4()), embedding=_embedding(), document="doc")
        ctx = _ctx(tenant_id="t1", team_ids=("team-a",))
        with pytest.raises(ValueError, match="team"):
            store.upsert(ctx, [item], visibility="team", team_id="team-not-mine")


class TestPgVectorStoreDeleteValidation:
    def test_empty_ids_is_a_noop_without_connecting(self) -> None:
        store = PgVectorStore(dsn="postgresql://unreachable-host-for-test/db")
        store.delete(_ctx(tenant_id="t1"), [])


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
class TestPgVectorStoreRoundTrip:
    def test_upsert_then_query_returns_row_with_score_one_minus_distance(
        self, live_dsn: str
    ) -> None:
        store = PgVectorStore(dsn=live_dsn, table="docs_vectors")
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        item_id = str(uuid.uuid4())
        vector = _embedding(1.0)
        store.upsert(
            ctx,
            [VectorItem(id=item_id, embedding=vector, document="hello world", metadata={"k": "v"})],
            visibility="tenant",
            team_id=None,
        )

        hits = store.query(ctx, vector, n=5)

        assert len(hits) == 1
        hit = hits[0]
        assert isinstance(hit, VectorHit)
        assert hit.id == item_id
        assert hit.document == "hello world"
        assert hit.metadata == {"k": "v"}
        # Identical vector -> cosine distance 0 -> score = 1 - 0 = 1.
        assert hit.score == pytest.approx(1.0, abs=1e-6)

    def test_upsert_is_idempotent_on_id_conflict(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn, table="docs_vectors")
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        item_id = str(uuid.uuid4())
        vector = _embedding(1.0)

        store.upsert(
            ctx,
            [VectorItem(id=item_id, embedding=vector, document="v1", metadata={})],
            visibility="tenant",
            team_id=None,
        )
        store.upsert(
            ctx,
            [VectorItem(id=item_id, embedding=vector, document="v2", metadata={"updated": True})],
            visibility="tenant",
            team_id=None,
        )

        hits = store.query(ctx, vector, n=5)
        assert len(hits) == 1
        assert hits[0].document == "v2"
        assert hits[0].metadata == {"updated": True}

    def test_query_respects_n_limit(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn, table="docs_vectors")
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        items = [
            VectorItem(id=str(uuid.uuid4()), embedding=_embedding(float(i)), document=f"doc-{i}")
            for i in range(5)
        ]
        store.upsert(ctx, items, visibility="tenant", team_id=None)

        hits = store.query(ctx, _embedding(0.0), n=2)
        assert len(hits) == 2

    def test_memory_vectors_table_is_independent_of_docs_vectors(self, live_dsn: str) -> None:
        docs_store = PgVectorStore(dsn=live_dsn, table="docs_vectors")
        memory_store = PgVectorStore(dsn=live_dsn, table="memory_vectors")
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        vector = _embedding(1.0)
        item_id = str(uuid.uuid4())

        docs_store.upsert(
            ctx,
            [VectorItem(id=item_id, embedding=vector, document="in docs")],
            visibility="tenant",
            team_id=None,
        )

        assert docs_store.query(ctx, vector, n=5) != []
        assert memory_store.query(ctx, vector, n=5) == []


@requires_postgres
class TestPgVectorStoreScopeIsolation:
    def test_cross_tenant_query_returns_nothing(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn)
        owner_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        other_tenant_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        vector = _embedding(1.0)

        store.upsert(
            owner_ctx,
            [VectorItem(id=str(uuid.uuid4()), embedding=vector, document="secret")],
            visibility="tenant",
            team_id=None,
        )

        assert store.query(owner_ctx, vector, n=5) != []
        assert store.query(other_tenant_ctx, vector, n=5) == []

    def test_team_visibility_hidden_without_matching_team_visible_with_it(
        self, live_dsn: str
    ) -> None:
        store = PgVectorStore(dsn=live_dsn)
        tenant_id = str(uuid.uuid4())
        team_id = str(uuid.uuid4())
        writer_ctx = _ctx(tenant_id=tenant_id, team_ids=(team_id,))
        vector = _embedding(1.0)

        store.upsert(
            writer_ctx,
            [VectorItem(id=str(uuid.uuid4()), embedding=vector, document="team doc")],
            visibility="team",
            team_id=team_id,
        )

        outsider_ctx = _ctx(tenant_id=tenant_id, team_ids=(str(uuid.uuid4()),))
        assert store.query(outsider_ctx, vector, n=5) == []

        insider_ctx = _ctx(tenant_id=tenant_id, team_ids=(team_id,))
        hits = store.query(insider_ctx, vector, n=5)
        assert len(hits) == 1
        assert hits[0].document == "team doc"

    def test_user_visibility_only_visible_to_owner(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn)
        tenant_id = str(uuid.uuid4())
        owner_ctx = _ctx(tenant_id=tenant_id)
        vector = _embedding(1.0)

        store.upsert(
            owner_ctx,
            [VectorItem(id=str(uuid.uuid4()), embedding=vector, document="my private doc")],
            visibility="user",
            team_id=None,
        )

        other_user_ctx = _ctx(tenant_id=tenant_id)
        assert store.query(other_user_ctx, vector, n=5) == []

        same_ctx = _ctx(tenant_id=tenant_id, user_id=owner_ctx.user_id)
        hits = store.query(same_ctx, vector, n=5)
        assert len(hits) == 1
        assert hits[0].document == "my private doc"

    def test_metadata_where_filter(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        vector = _embedding(1.0)

        store.upsert(
            ctx,
            [
                VectorItem(
                    id=str(uuid.uuid4()),
                    embedding=vector,
                    document="python doc",
                    metadata={"lang": "python"},
                ),
                VectorItem(
                    id=str(uuid.uuid4()),
                    embedding=vector,
                    document="rust doc",
                    metadata={"lang": "rust"},
                ),
            ],
            visibility="tenant",
            team_id=None,
        )

        hits = store.query(ctx, vector, n=10, where={"lang": "python"})
        assert len(hits) == 1
        assert hits[0].document == "python doc"


@requires_postgres
class TestPgVectorStoreDelete:
    def test_delete_removes_row_from_subsequent_query(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn)
        ctx = _ctx(tenant_id=str(uuid.uuid4()))
        vector = _embedding(1.0)
        item_id = str(uuid.uuid4())
        store.upsert(
            ctx,
            [VectorItem(id=item_id, embedding=vector, document="to delete")],
            visibility="tenant",
            team_id=None,
        )
        assert store.query(ctx, vector, n=5) != []

        store.delete(ctx, [item_id])

        assert store.query(ctx, vector, n=5) == []

    def test_delete_is_scoped_to_tenant(self, live_dsn: str) -> None:
        store = PgVectorStore(dsn=live_dsn)
        owner_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        other_tenant_ctx = _ctx(tenant_id=str(uuid.uuid4()))
        vector = _embedding(1.0)
        item_id = str(uuid.uuid4())
        store.upsert(
            owner_ctx,
            [VectorItem(id=item_id, embedding=vector, document="not yours to delete")],
            visibility="tenant",
            team_id=None,
        )

        # Another tenant's delete call must not remove the owner's row.
        store.delete(other_tenant_ctx, [item_id])

        assert store.query(owner_ctx, vector, n=5) != []
