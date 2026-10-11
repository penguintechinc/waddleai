"""Regression tests for gh-215: ``alembic upgrade head`` must bootstrap an EMPTY database.

``001_baseline`` is a documented no-op and ``002``+ assume pre-existing tables,
so the Helm migration Job's bare ``alembic upgrade head`` died at migration 002
on a fresh install. These tests run the exact Job command
(``python3 -m alembic upgrade head`` from ``services/management``) against a
REAL, empty ``pgvector/pgvector:pg17`` container -- no DB mocking.

Skips (not fails) when Docker is unavailable, matching the gh-207 suite.
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

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.e2e.conftest import _free_port

REPO = Path(__file__).resolve().parents[2]
MANAGEMENT_DIR = REPO / "services" / "management"
_PG_IMAGE = "pgvector/pgvector:pg17"

# Tables that only exist if the full head schema was built (spans baseline
# tables, the tables migrations 002..021 add, and the pgvector-backed ones).
_KEY_TABLES = (
    "users",
    "organizations",
    "api_keys",
    "provider_credentials",
    "content_filter_audit_log",
    "memory_embeddings",
    "audit_log",
)


@dataclass(slots=True)
class _EmptyPostgres:
    """A running, EMPTY throwaway Postgres container: its DSN."""

    database_url: str


def _head_revision() -> str:
    """Return the single Alembic head revision declared by the migration scripts."""
    cfg = Config(str(MANAGEMENT_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
    return heads[0]


def _run_job_command(database_url: str) -> subprocess.CompletedProcess[str]:
    """Run the migration Job's command: ``python3 -m alembic upgrade head`` in the service dir."""
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", "upgrade", "head"],
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
        yield _EmptyPostgres(database_url=database_url)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


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


# regression: gh-215
def test_upgrade_head_bootstraps_empty_database(empty_db: _EmptyPostgres) -> None:
    """The Job command succeeds on an EMPTY DB and lands at head with the real tables."""
    assert _tables(empty_db.database_url) == set(), "fixture DB must start empty"

    result = _run_job_command(empty_db.database_url)

    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr[-2000:]}"
    assert _revision(empty_db.database_url) == [_head_revision()]
    tables = _tables(empty_db.database_url)
    missing = [t for t in _KEY_TABLES if t not in tables]
    assert not missing, f"tables missing at head: {missing} (have {len(tables)})"


# regression: gh-215
def test_upgrade_head_is_idempotent_and_leaves_populated_db_alone(
    empty_db: _EmptyPostgres,
) -> None:
    """A second run on an already-stamped DB takes the normal path and never rebuilds the schema."""
    assert _run_job_command(empty_db.database_url).returncode == 0

    # Make the already-at-head DB observably different from a fresh bootstrap.
    engine = sa.create_engine(empty_db.database_url)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE audit_log"))
    engine.dispose()

    second = _run_job_command(empty_db.database_url)

    assert second.returncode == 0, f"second upgrade failed:\n{second.stderr[-2000:]}"
    assert _revision(empty_db.database_url) == [_head_revision()]
    assert "audit_log" not in _tables(empty_db.database_url), (
        "a populated, stamped DB must NOT be re-bootstrapped (create_all re-ran)"
    )
