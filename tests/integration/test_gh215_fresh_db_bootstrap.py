"""Regression tests for gh-215: ``alembic upgrade head`` on EMPTY must yield a head-equivalent DB.

``001_baseline`` used to be a documented no-op and ``002``+ assume pre-existing
tables, so the Helm migration Job's bare ``alembic upgrade head`` died at
migration 002 on a fresh install. The first fix (create_all + stamp head) made
the command succeed but produced a DIFFERENT database: no seed rows
(``model_registry`` etc. -- permanently, since the DB was stamped and the
migrations never re-ran) and none of the migration-only DDL. The fix now builds
the frozen baseline in ``001_baseline`` and runs the REAL chain on top, so these
tests assert PARITY with a migrated database, not mere success.

Every test runs the exact Job command (``python3 -m alembic upgrade head`` from
``services/management``) against a REAL ``pgvector/pgvector:pg17`` container --
no DB mocking. Skips (not fails) when Docker is unavailable, matching gh-207.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.e2e.conftest import _free_port

REPO = Path(__file__).resolve().parents[2]
MANAGEMENT_DIR = REPO / "services" / "management"
_PG_IMAGE = "pgvector/pgvector:pg17"

# Seed rows the migration chain inserts. Each of these tables was EMPTY after the
# first (create_all + stamp head) attempt -- permanently, since the DB was stamped.
# (table, minimum rows). model_assignments / security_policies are asserted exactly
# below; the rest are non-zero guards (their exact seed sizes are owned by the
# migrations and legitimately change when a later migration edits them).
_SEEDED_TABLES = (
    ("token_conversion_rates", 1),  # 007
    ("model_registry", 1),  # 008
    ("model_assignments", 5),  # 010 (renamed from routing_matrix) -- 5 internal functions
    ("security_policies", 1),  # 011 -- the global row
    ("docs_sources", 1),  # 012
    ("cache_configs", 1),  # 009a -- global default row
)

# (table, column) pairs that exist ONLY via migration DDL -- no ORM model has them,
# so create_all() can never produce them.
_MIGRATION_ONLY_COLUMNS = (
    ("embedding_cache", "embedding"),  # 009b, vector(768)
    ("code_chunks", "content_tsv"),  # 012, generated tsvector
    ("code_chunks", "embedding"),  # 012
    ("docs_cache_pages", "embedding"),  # 012
    ("response_cache_entries", "prompt_embedding"),  # 009a
    ("memory_embeddings", "embedding"),  # baseline pgvector column (023 re-asserts it)
    ("rag_documents", "embedding"),  # baseline pgvector column
)

# Tables only the migrations create (absent from the ORM), plus a cross-section of the rest.
_KEY_TABLES = (
    "users",
    "organizations",
    "api_keys",
    "provider_credentials",
    "content_filter_audit_log",
    "memory_embeddings",
    "audit_log",
    "model_registry",
    "model_assignments",
    "hook_rules",
    "hook_configs",
    "llamacpp_deployments",
    "fleet_backends",
)


@dataclass(slots=True)
class _EmptyPostgres:
    """A running, EMPTY throwaway Postgres container: its DSN and container name."""

    database_url: str
    container: str


def _head_revision() -> str:
    """Return the single Alembic head revision declared by the migration scripts."""
    cfg = Config(str(MANAGEMENT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
    return heads[0]


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python3 -m alembic <args>`` from the service dir, as the Helm Job does."""
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=MANAGEMENT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture
def empty_db() -> Iterator[_EmptyPostgres]:
    """A fresh, completely empty Postgres per test (no schema, no alembic_version)."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available -- gh-215 regression needs a real Postgres")
    port = _free_port()
    name = f"waddleai-gh215-pg-{port}"
    password = "gh215-regression-test-password"  # noqa: S105 -- throwaway local container
    run_argv = [
        "docker", "run", "-d", "--rm", "--name", name,
        "-e", "POSTGRES_USER=waddleai",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", "POSTGRES_DB=waddleai",
        "-p", f"{port}:5432",
        _PG_IMAGE,
    ]  # fmt: skip
    try:
        subprocess.run(run_argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603, S607
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"could not start Postgres for the gh-215 regression suite: {exc.stderr}")
    database_url = f"postgresql://waddleai:{password}@127.0.0.1:{port}/waddleai"
    try:
        # The official image runs a temporary init server, then restarts the real one;
        # "ready" is only trustworthy once the log shows it twice, then a SELECT works.
        deadline = time.time() + 90
        while True:
            logs = subprocess.run(  # noqa: S603
                ["docker", "logs", name],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
            )
            if (logs.stdout + logs.stderr).count("ready to accept connections") >= 2:
                break
            if time.time() > deadline:
                pytest.fail(f"Postgres {name!r} never finished init:\n{logs.stderr[-500:]}")
            time.sleep(1)
        engine = sa.create_engine(database_url)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        engine.dispose()
        yield _EmptyPostgres(database_url=database_url, container=name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


def _run_job_command(database_url: str) -> subprocess.CompletedProcess[str]:
    """Run the migration Job's exact command: ``python3 -m alembic upgrade head``."""
    return _run_alembic(database_url, "upgrade", "head")


def _tables(database_url: str) -> set[str]:
    engine = sa.create_engine(database_url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _revision(database_url: str) -> list[str]:
    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(sa.text("SELECT version_num FROM alembic_version"))]
    finally:
        engine.dispose()


def _scalar(database_url: str, sql: str, **params: str) -> Any:
    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(sql), params).scalar()
    finally:
        engine.dispose()


def _counts(database_url: str, tables: list[str]) -> dict[str, int]:
    return {t: int(_scalar(database_url, f"SELECT count(*) FROM {t}") or 0) for t in tables}  # noqa: S608


# regression: gh-215
def test_upgrade_head_on_empty_db_is_head_equivalent(empty_db: _EmptyPostgres) -> None:
    """PARITY: the Job command on an EMPTY DB yields tables + migration DDL + seed rows."""
    url = empty_db.database_url
    assert _tables(url) == set(), "fixture DB must start empty"

    result = _run_job_command(url)

    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr[-2000:]}"
    assert _revision(url) == [_head_revision()]

    tables = _tables(url)
    missing = [t for t in _KEY_TABLES if t not in tables]
    assert not missing, f"tables missing at head: {missing} (have {len(tables)})"
    assert "routing_matrix" not in tables, "010 renames routing_matrix -> model_assignments"

    # Seed rows (PERMANENTLY lost on a stamped-at-head DB, so must exist right now).
    counts = _counts(url, [t for t, _ in _SEEDED_TABLES])
    under = {t: (counts[t], n) for t, n in _SEEDED_TABLES if counts[t] < n}
    assert not under, f"seed rows missing (table: (have, need)): {under}; counts={counts}"
    assert counts["model_assignments"] == 5, "010 seeds exactly the 5 internal-function roles"
    assert _scalar(url, "SELECT count(*) FROM security_policies WHERE scope_type='global'") == 1
    # 019 raised the Gemma floor e2b -> e4b: proves the chain's CUMULATIVE end state ran, not
    # just the original 010 seed.
    assert _scalar(url, "SELECT count(*) FROM model_assignments WHERE model_name='gemma4:e2b'") == 0

    # Migration-only DDL: columns no ORM model carries.
    for table, column in _MIGRATION_ONLY_COLUMNS:
        found = _scalar(
            url,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c",
            t=table,
            c=column,
        )
        assert found == 1, f"migration-only column {table}.{column} missing"
    for column, expected in (("embedding", "vector"), ("content_tsv", "tsvector")):
        udt = _scalar(
            url,
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name='code_chunks' AND column_name=:c",
            c=column,
        )
        assert udt == expected
    gin = _scalar(
        url,
        "SELECT indexdef FROM pg_indexes WHERE indexname='code_chunks_fts_idx'",
    )
    assert gin is not None and "gin" in str(gin).lower(), f"FTS GIN index missing: {gin!r}"
    assert _scalar(url, "SELECT count(*) FROM pg_extension WHERE extname='vector'") == 1


# regression: gh-215
def test_second_run_is_a_noop_and_does_not_reseed(empty_db: _EmptyPostgres) -> None:
    """Re-running the Job on an at-head DB changes nothing (no re-bootstrap, no duplicate seeds)."""
    url = empty_db.database_url
    assert _run_job_command(url).returncode == 0
    names = [t for t, _ in _SEEDED_TABLES]
    before = _counts(url, names)

    # Make the at-head DB observably different from a fresh build.
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE audit_log"))
    engine.dispose()

    second = _run_job_command(url)

    assert second.returncode == 0, f"second upgrade failed:\n{second.stderr[-2000:]}"
    assert _revision(url) == [_head_revision()]
    assert "audit_log" not in _tables(url), "an at-head DB must NOT be rebuilt on re-run"
    assert _counts(url, names) == before, "seed rows were duplicated/changed on re-run"


# regression: gh-215
def test_mid_run_failure_rolls_back_everything_and_retry_succeeds(
    empty_db: _EmptyPostgres,
) -> None:
    """A failure late in the chain leaves NOTHING behind, so the retry completes (no wedge).

    A VIEW named ``audit_log`` is invisible to the emptiness check but makes migration 021's
    CREATE TABLE fail -- after the baseline and 002..020 already ran in the same transaction.
    """
    url = empty_db.database_url
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE VIEW audit_log AS SELECT 1 AS id"))

    failed = _run_job_command(url)

    assert failed.returncode != 0, "the sabotaged run must fail"
    assert "audit_log" in failed.stderr
    leftover = _tables(url)
    assert leftover == set(), f"failed run must roll back every table, left: {sorted(leftover)}"
    assert "alembic_version" not in leftover

    with engine.begin() as conn:
        conn.execute(sa.text("DROP VIEW audit_log"))
    engine.dispose()

    retry = _run_job_command(url)

    assert retry.returncode == 0, f"retry after failure still wedged:\n{retry.stderr[-2000:]}"
    assert _revision(url) == [_head_revision()]
    assert "audit_log" in _tables(url)


# regression: gh-215
def test_missing_pgvector_fails_loud_and_leaves_no_schema(empty_db: _EmptyPostgres) -> None:
    """Without pgvector the Job FAILS (it never stamps a vector-less schema as head)."""
    removed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker", "exec", "-u", "root", empty_db.container,
            "sh", "-c", "rm -f /usr/share/postgresql/*/extension/vector.control",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )  # fmt: skip
    assert removed.returncode == 0, removed.stderr

    result = _run_job_command(empty_db.database_url)

    assert result.returncode != 0, "must fail loudly when pgvector is unavailable"
    assert "vector" in result.stderr.lower(), result.stderr[-1500:]
    assert _tables(empty_db.database_url) == set(), "no partial schema may survive"


# regression: gh-215
def test_head_schema_without_alembic_version_is_refused_not_clobbered(
    empty_db: _EmptyPostgres,
) -> None:
    """Tables present but no alembic_version (e.g. app-startup init_schema ran first): refuse.

    No create_all over existing data, no half-run chain: the explicit error names the fix
    (``alembic stamp``), the data is untouched, and nothing is recorded.
    """
    url = empty_db.database_url
    sys.path.insert(0, str(MANAGEMENT_DIR))
    try:
        from app.models_sqlalchemy import init_schema  # noqa: PLC0415

        init_schema(url).dispose()
    finally:
        sys.path.remove(str(MANAGEMENT_DIR))
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('precious-data')"))
    engine.dispose()
    tables_before = _tables(url)
    assert "alembic_version" not in tables_before

    result = _run_job_command(url)

    assert result.returncode != 0
    assert "refusing to run 001_baseline" in result.stderr
    assert "alembic stamp" in result.stderr
    assert _tables(url) == tables_before, "refused run must not create or drop tables"
    assert _scalar(url, "SELECT name FROM organizations") == "precious-data"


# regression: gh-215
def test_populated_mid_chain_db_upgrades_through_the_normal_chain(
    empty_db: _EmptyPostgres,
) -> None:
    """A populated DB sitting mid-chain (010) takes the normal 011..head path, data intact."""
    url = empty_db.database_url
    assert _run_alembic(url, "upgrade", "010_routing_engine").returncode == 0
    assert _revision(url) == ["010_routing_engine"]
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organizations (name) VALUES ('acme')"))
        conn.execute(
            sa.text(
                "INSERT INTO content_filter_config (key, value, organization_id) "
                "SELECT 'tier2', 'on', id FROM organizations WHERE name='acme'"
            )
        )
    engine.dispose()

    result = _run_job_command(url)

    assert result.returncode == 0, f"upgrade of populated DB failed:\n{result.stderr[-2000:]}"
    assert _revision(url) == [_head_revision()]
    assert _scalar(url, "SELECT name FROM organizations") == "acme"
    # 011 folds a per-org policy row for each org with filter config, beside the global row:
    # proves the pre-existing data went through the real migration, not a rebuild.
    assert _scalar(url, "SELECT count(*) FROM security_policies") == 2
