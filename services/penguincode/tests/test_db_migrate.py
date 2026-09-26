"""Tests for penguincode_cli/db/migrate.py -- the penguincode pgvector schema runner.

Static tests (no DB needed) always run. The live-Postgres tests connect to
`TEST_DATABASE_URL` and are skipped -- with an explicit reason, never
silently -- when that env var is unset; CI (T16) wires an ephemeral Postgres
service container and sets it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from penguincode_cli.db.migrate import MIGRATIONS_DIR, MigrationResult, _resolve_dsn, run_migrations

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _scalar(cursor: psycopg.Cursor[Any]) -> Any:
    """Fetch a single scalar from a cursor, asserting the row exists.

    A thin type-narrowing helper: `Cursor.fetchone()` returns `tuple | None`,
    which mypy correctly refuses to index. Every call site here expects
    exactly one row (a `SELECT gen_random_uuid()` or an `INSERT ... RETURNING
    id`), so a `None` result is itself a bug worth surfacing as an
    AssertionError rather than silencing the type error.
    """
    row = cursor.fetchone()
    assert row is not None
    return row[0]


requires_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set -- live-Postgres migration tests are CI-pending (T16)",
)

EXPECTED_TABLES = {"docs_vectors", "memory_vectors", "graph_nodes", "graph_edges"}
SCOPE_COLUMNS = {"tenant_id", "org_id", "team_id", "owner_user_id", "visibility"}


# ---------------------------------------------------------------------------
# Static tests: migration files exist and are well-formed, no DB required.
# ---------------------------------------------------------------------------


class TestMigrationDiscovery:
    """Static checks on the migrations directory contents."""

    def test_migrations_dir_exists(self) -> None:
        """The runner's default migrations directory is db/migrations."""
        assert MIGRATIONS_DIR.is_dir()
        assert MIGRATIONS_DIR.name == "migrations"

    def test_expected_migration_files_present(self) -> None:
        """Every table/schema migration named in the plan exists on disk."""
        names = {path.name for path in MIGRATIONS_DIR.glob("*.sql")}
        assert names == {
            "0001_schema_and_extension.sql",
            "0002_docs_vectors.sql",
            "0003_memory_vectors.sql",
            "0004_graph_nodes.sql",
            "0005_graph_edges.sql",
        }

    def test_migration_files_sort_in_apply_order(self) -> None:
        """Glob + sort must yield the numeric prefix order, not lexical drift."""
        from penguincode_cli.db.migrate import _discover_migrations

        files = _discover_migrations(MIGRATIONS_DIR)
        assert [path.name for path in files] == sorted(path.name for path in files)
        assert files[0].name.startswith("0001_")
        assert files[-1].name.startswith("0005_")

    def test_every_migration_is_idempotent_sql(self) -> None:
        """Every DDL statement uses an IF NOT EXISTS / inline-constraint guard.

        A bare `CREATE TABLE` or `ALTER TABLE ... ADD CONSTRAINT` (without a
        guard) would make a second run fail instead of no-op -- this is a
        static proxy for that, so a future migration author gets caught at
        review time even without a live Postgres available.
        """
        for path in MIGRATIONS_DIR.glob("*.sql"):
            sql = path.read_text(encoding="utf-8").upper()
            if "CREATE TABLE" in sql:
                assert "CREATE TABLE IF NOT EXISTS" in sql, f"{path.name}: unguarded CREATE TABLE"
            if "CREATE SCHEMA" in sql:
                assert "CREATE SCHEMA IF NOT EXISTS" in sql, f"{path.name}: unguarded CREATE SCHEMA"
            if "CREATE EXTENSION" in sql:
                assert "CREATE EXTENSION IF NOT EXISTS" in sql, (
                    f"{path.name}: unguarded CREATE EXTENSION"
                )
            if "CREATE INDEX" in sql:
                assert "CREATE INDEX IF NOT EXISTS" in sql, f"{path.name}: unguarded CREATE INDEX"
            # ALTER TABLE ... ADD CONSTRAINT has no IF NOT EXISTS in
            # PostgreSQL -- constraints must be declared inline on CREATE
            # TABLE instead (which 0004/0005 do for their UNIQUE constraints).
            assert "ALTER TABLE" not in sql, f"{path.name}: ALTER TABLE is not idempotent-safe here"


class TestResolveDsn:
    """`_resolve_dsn` precedence and failure mode."""

    def test_explicit_dsn_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://env-value/db")
        assert _resolve_dsn("postgresql://explicit/db") == "postgresql://explicit/db"

    def test_falls_back_to_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PGVECTOR_URL", "postgresql://env-value/db")
        assert _resolve_dsn(None) == "postgresql://env-value/db"

    def test_raises_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PGVECTOR_URL", raising=False)
        with pytest.raises(RuntimeError, match="no Postgres DSN"):
            _resolve_dsn(None)


class TestRunMigrationsInputValidation:
    """`run_migrations` argument validation that does not require a live DB."""

    def test_raises_when_no_dsn_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PGVECTOR_URL", raising=False)
        with pytest.raises(RuntimeError, match="no Postgres DSN"):
            run_migrations()

    def test_raises_when_migrations_dir_is_empty(self, tmp_path: Path) -> None:
        # Reached before any DB connection is opened, so a fake DSN is fine.
        with pytest.raises(RuntimeError, match="no migration files found"):
            run_migrations(dsn="postgresql://fake/db", migrations_dir=tmp_path)


# ---------------------------------------------------------------------------
# Live-Postgres tests: require TEST_DATABASE_URL (pgvector/pgvector image).
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_dsn() -> str:
    """Drop the penguincode schema so each test starts from a blank slate.

    The `vector` extension itself lives in `public` and is left alone --
    only the schema this runner owns is reset.
    """
    assert TEST_DATABASE_URL is not None  # narrows type for mypy; skipif already guards this
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS penguincode CASCADE")
    return TEST_DATABASE_URL


@requires_postgres
class TestRunMigrationsLive:
    """Migration runner behavior against a real pgvector-enabled Postgres."""

    def test_first_run_applies_all_files_in_order(self, clean_dsn: str) -> None:
        result = run_migrations(dsn=clean_dsn)
        assert isinstance(result, MigrationResult)
        assert result.applied == (
            "0001_schema_and_extension.sql",
            "0002_docs_vectors.sql",
            "0003_memory_vectors.sql",
            "0004_graph_nodes.sql",
            "0005_graph_edges.sql",
        )
        assert result.skipped == ()

    def test_second_run_is_idempotent_no_error_no_duplicates(self, clean_dsn: str) -> None:
        run_migrations(dsn=clean_dsn)
        second = run_migrations(dsn=clean_dsn)

        assert second.applied == ()
        assert set(second.skipped) == {
            "0001_schema_and_extension.sql",
            "0002_docs_vectors.sql",
            "0003_memory_vectors.sql",
            "0004_graph_nodes.sql",
            "0005_graph_edges.sql",
        }

        with psycopg.connect(clean_dsn) as conn:
            rows = conn.execute(
                "SELECT version, count(*) FROM penguincode.schema_migrations "
                "GROUP BY version HAVING count(*) > 1"
            ).fetchall()
        assert rows == [], f"duplicate schema_migrations rows after re-run: {rows}"

    def test_running_three_times_stays_idempotent(self, clean_dsn: str) -> None:
        """Belt-and-suspenders: the exact 'run migrate twice' acceptance
        criterion plus one more, since a bug that only surfaces on the 3rd
        run (e.g. an index name collision across re-created connections)
        would otherwise slip through.
        """
        for _ in range(3):
            run_migrations(dsn=clean_dsn)

    def test_vector_extension_present(self, clean_dsn: str) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn) as conn:
            row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert row is not None, "pgvector extension was not installed"

    def test_all_four_tables_exist(self, clean_dsn: str) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'penguincode'"
            ).fetchall()
        table_names = {row[0] for row in rows}
        assert table_names >= EXPECTED_TABLES

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
    def test_table_has_all_scope_columns(self, clean_dsn: str, table_name: str) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn) as conn:
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'penguincode' AND table_name = %s",
                (table_name,),
            ).fetchall()
        columns = {row[0] for row in rows}
        assert columns >= SCOPE_COLUMNS, (
            f"{table_name} missing scope columns: {SCOPE_COLUMNS - columns}"
        )

    @pytest.mark.parametrize("table_name", ["docs_vectors", "memory_vectors"])
    def test_vector_tables_have_768_dim_embedding_column(
        self, clean_dsn: str, table_name: str
    ) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn) as conn:
            row = conn.execute(
                "SELECT atttypmod FROM pg_attribute "
                "JOIN pg_class ON pg_class.oid = pg_attribute.attrelid "
                "JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace "
                "WHERE pg_namespace.nspname = 'penguincode' AND pg_class.relname = %s "
                "AND pg_attribute.attname = 'embedding'",
                (table_name,),
            ).fetchone()
        assert row is not None
        assert row[0] == 768, f"{table_name}.embedding is not vector(768): atttypmod={row[0]}"

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
    def test_visibility_check_constraint_rejects_bad_values(
        self, clean_dsn: str, table_name: str
    ) -> None:
        run_migrations(dsn=clean_dsn)
        zero_vector = "[" + ",".join("0" for _ in range(768)) + "]"
        with psycopg.connect(clean_dsn, autocommit=True) as conn:
            if table_name == "graph_edges":
                # graph_edges has no visibility-independent columns to stand
                # in for node_type/key -- it needs two real nodes to satisfy
                # the NOT NULL FK columns before the CHECK constraint is even
                # evaluated.
                tenant = _scalar(conn.execute("SELECT gen_random_uuid()"))
                src_id = _scalar(
                    conn.execute(
                        "INSERT INTO penguincode.graph_nodes "
                        "(graph_kind, node_type, key, tenant_id, visibility) "
                        "VALUES ('code', 'file', 'v-src.py', %s, 'tenant') RETURNING id",
                        (tenant,),
                    )
                )
                dst_id = _scalar(
                    conn.execute(
                        "INSERT INTO penguincode.graph_nodes "
                        "(graph_kind, node_type, key, tenant_id, visibility) "
                        "VALUES ('code', 'file', 'v-dst.py', %s, 'tenant') RETURNING id",
                        (tenant,),
                    )
                )
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(
                        "INSERT INTO penguincode.graph_edges "
                        "(graph_kind, src_id, dst_id, rel_type, tenant_id, visibility) "
                        "VALUES ('code', %s, %s, 'imports', %s, 'not-a-visibility')",
                        (src_id, dst_id, tenant),
                    )
            elif table_name in ("docs_vectors", "memory_vectors"):
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(
                        f"INSERT INTO penguincode.{table_name} "
                        "(embedding, document, tenant_id, visibility) "
                        "VALUES (%s::vector, 'x', gen_random_uuid(), 'not-a-visibility')",
                        (zero_vector,),
                    )
            else:
                with pytest.raises(psycopg.errors.CheckViolation):
                    conn.execute(
                        f"INSERT INTO penguincode.{table_name} "
                        "(graph_kind, node_type, key, tenant_id, visibility) "
                        "VALUES ('code', 't', 'k', gen_random_uuid(), 'not-a-visibility')",
                    )

    def test_graph_nodes_unique_constraint_on_tenant_kind_type_key(self, clean_dsn: str) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn, autocommit=True) as conn:
            tenant = _scalar(conn.execute("SELECT gen_random_uuid()"))
            conn.execute(
                "INSERT INTO penguincode.graph_nodes "
                "(graph_kind, node_type, key, tenant_id, visibility) "
                "VALUES ('code', 'file', 'src/app.py', %s, 'tenant')",
                (tenant,),
            )
            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO penguincode.graph_nodes "
                    "(graph_kind, node_type, key, tenant_id, visibility) "
                    "VALUES ('code', 'file', 'src/app.py', %s, 'tenant')",
                    (tenant,),
                )

    def test_graph_edges_fk_to_graph_nodes_and_scoped_unique(self, clean_dsn: str) -> None:
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn, autocommit=True) as conn:
            tenant = _scalar(conn.execute("SELECT gen_random_uuid()"))
            src_id = _scalar(
                conn.execute(
                    "INSERT INTO penguincode.graph_nodes "
                    "(graph_kind, node_type, key, tenant_id, visibility) "
                    "VALUES ('code', 'file', 'a.py', %s, 'tenant') RETURNING id",
                    (tenant,),
                )
            )
            dst_id = _scalar(
                conn.execute(
                    "INSERT INTO penguincode.graph_nodes "
                    "(graph_kind, node_type, key, tenant_id, visibility) "
                    "VALUES ('code', 'file', 'b.py', %s, 'tenant') RETURNING id",
                    (tenant,),
                )
            )

            conn.execute(
                "INSERT INTO penguincode.graph_edges "
                "(graph_kind, src_id, dst_id, rel_type, tenant_id, visibility) "
                "VALUES ('code', %s, %s, 'imports', %s, 'tenant')",
                (src_id, dst_id, tenant),
            )

            with pytest.raises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO penguincode.graph_edges "
                    "(graph_kind, src_id, dst_id, rel_type, tenant_id, visibility) "
                    "VALUES ('code', %s, %s, 'imports', %s, 'tenant')",
                    (src_id, dst_id, tenant),
                )

            fake_node_id = _scalar(conn.execute("SELECT gen_random_uuid()"))
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "INSERT INTO penguincode.graph_edges "
                    "(graph_kind, src_id, dst_id, rel_type, tenant_id, visibility) "
                    "VALUES ('code', %s, %s, 'imports', %s, 'tenant')",
                    (fake_node_id, dst_id, tenant),
                )

    def test_scope_leading_indexes_exist(self, clean_dsn: str) -> None:
        """Composite indexes lead with tenant_id per the plan's Shared Contracts."""
        run_migrations(dsn=clean_dsn)
        expected = {
            "idx_docs_vectors_scope": ["tenant_id", "team_id"],
            "idx_memory_vectors_scope": ["tenant_id", "team_id"],
            "idx_graph_nodes_scope": ["tenant_id", "graph_kind", "team_id"],
            "idx_graph_edges_scope": ["tenant_id", "graph_kind", "team_id"],
        }
        with psycopg.connect(clean_dsn) as conn:
            for index_name, expected_cols in expected.items():
                row = conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname = 'penguincode' AND indexname = %s",
                    (index_name,),
                ).fetchone()
                assert row is not None, f"missing index {index_name}"
                indexdef = row[0]
                assert indexdef.index(expected_cols[0]) < indexdef.index(expected_cols[-1]), (
                    f"{index_name} does not lead with scope columns: {indexdef}"
                )

    @pytest.mark.parametrize("table_name", ["docs_vectors", "memory_vectors"])
    def test_cosine_ann_index_exists(self, clean_dsn: str, table_name: str) -> None:
        """Either the HNSW or the ivfflat fallback cosine index must exist."""
        run_migrations(dsn=clean_dsn)
        with psycopg.connect(clean_dsn) as conn:
            rows = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'penguincode' AND tablename = %s",
                (table_name,),
            ).fetchall()
        index_names = {row[0] for row in rows}
        assert any(
            name.endswith("_embedding_hnsw") or name.endswith("_embedding_ivfflat")
            for name in index_names
        ), f"no cosine ANN index found for {table_name}: {index_names}"
