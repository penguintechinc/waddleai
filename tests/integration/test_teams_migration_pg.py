"""Migration 024_teams against real Postgres (FK constraints, expanding IN, boolean params).

The SQLite unit test (``tests/unit/management/test_migration_024_teams.py``) cannot see the
Postgres-only paths: the real ``ON DELETE SET NULL`` foreign keys 024 adds via
``op.create_foreign_key``, the ``IN :admin_roles`` expanding bind, and boolean parameters.
Skips (not fails) when Docker is unavailable -- same convention as ``test_gh213_schema_drift.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from tests.e2e.conftest import _free_port

MANAGEMENT_DIR = Path(__file__).resolve().parents[2] / "services" / "management"
_PG_IMAGE = "postgres:17-bookworm"
_PG_PASSWORD = "teams-migration"  # noqa: S105 -- ephemeral local test container password

_PRE_024_DDL = (
    "CREATE TABLE organizations (id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE users (id SERIAL PRIMARY KEY, username VARCHAR(255) NOT NULL, "
    "role VARCHAR(50) NOT NULL, organization_id INTEGER NOT NULL REFERENCES organizations(id))",
    "CREATE TABLE api_keys (id SERIAL PRIMARY KEY, key_id VARCHAR(255) NOT NULL, "
    "user_id INTEGER NOT NULL REFERENCES users(id), "
    "organization_id INTEGER NOT NULL REFERENCES organizations(id), name VARCHAR(255) NOT NULL)",
    "CREATE TABLE virtual_keys (id SERIAL PRIMARY KEY, user_id INTEGER, organization_id INTEGER, "
    "name VARCHAR(255) NOT NULL)",
    "INSERT INTO organizations (name) VALUES ('acme'), ('globex')",
    "INSERT INTO users (username, role, organization_id) VALUES "
    "('boss', 'admin', 1), ('dev', 'user', 1), ('gx', 'user', 2)",
    "INSERT INTO api_keys (key_id, user_id, organization_id, name) VALUES "
    "('k1', 1, 1, 'a'), ('k3', 3, 2, 'c')",
)


@pytest.fixture(scope="module")
def pg_admin_dsn() -> Iterator[str]:
    """DSN to the maintenance DB of a module-lived Postgres container."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available -- teams migration test needs a real Postgres")
    port = _free_port()
    name = f"waddleai-teams024-{uuid.uuid4().hex[:8]}"
    run_argv = [
        "docker", "run", "--rm", "-d", "--name", name,
        "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}", "-p", f"{port}:5432", _PG_IMAGE,
    ]  # fmt: skip
    try:
        subprocess.run(run_argv, check=True, capture_output=True, timeout=120, text=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"could not start Postgres: {exc.stderr}")
        return
    dsn = f"postgresql://postgres:{_PG_PASSWORD}@127.0.0.1:{port}/postgres"
    try:
        for _ in range(60):
            try:
                eng = sa.create_engine(dsn)
                with eng.connect() as conn:
                    conn.execute(sa.text("SELECT 1"))
                eng.dispose()
                break
            except sa.exc.OperationalError:
                time.sleep(1)
        else:
            pytest.fail(f"Postgres container {name!r} never accepted connections")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)  # noqa: S603, S607


@pytest.fixture
def migrated(pg_admin_dsn, monkeypatch):
    """A fresh DB with the pre-024 schema seeded, stamped 023 and upgraded to 024; yields engine."""
    db_name = f"t024_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(pg_admin_dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()
    url = f"{pg_admin_dsn.rsplit('/', 1)[0]}/{db_name}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _PRE_024_DDL:
            conn.execute(sa.text(ddl))
    cfg = Config()
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.stamp(cfg, "023_schema_drift_gh213")
    command.upgrade(cfg, "024_teams")
    yield engine, cfg
    engine.dispose()


def test_upgrade_backfills_and_adds_real_foreign_keys(migrated) -> None:
    """On Postgres 024 seats users, points keys at teams and creates the two real FKs."""
    engine, _cfg = migrated
    with engine.connect() as conn:
        roles = dict(
            conn.execute(
                sa.text(
                    "SELECT u.username, tm.team_role FROM team_members tm "
                    "JOIN users u ON u.id = tm.user_id"
                )
            ).all()
        )
        keyed = dict(
            conn.execute(sa.text("SELECT key_id, team_id IS NOT NULL FROM api_keys")).all()
        )
        fks = {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT constraint_name FROM information_schema.table_constraints "
                    "WHERE constraint_type = 'FOREIGN KEY'"
                )
            )
        }
    assert roles == {"boss": "admin", "dev": "member", "gx": "member"}
    assert keyed == {"k1": True, "k3": True}
    assert {"fk_api_keys_team_id", "fk_virtual_keys_api_key_id"} <= fks


def test_unique_user_org_is_enforced_by_postgres(migrated) -> None:
    """A second team for the same (user, org) is rejected by the real UNIQUE constraint."""
    engine, _cfg = migrated
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO teams (organization_id, name) VALUES (1, 'Beta')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO team_members (team_id, user_id, organization_id, team_role) "
                "SELECT t.id, 2, 1, 'member' FROM teams t WHERE t.name = 'Beta'"
            )
        )


def test_downgrade_then_reupgrade_round_trips(migrated) -> None:
    """Down then up again leaves the same Default teams (the data step is re-runnable)."""
    engine, cfg = migrated
    command.downgrade(cfg, "023_schema_drift_gh213")
    command.upgrade(cfg, "024_teams")
    with engine.connect() as conn:
        names = conn.execute(sa.text("SELECT name FROM teams ORDER BY organization_id")).all()
    assert names == [("Default",), ("Default",)]


def test_team_delete_nulls_key_link_and_backfill_rerun_is_a_noop(migrated) -> None:
    """The real ``ON DELETE SET NULL`` FK fires, and re-running the data step changes nothing."""
    from alembic.script import ScriptDirectory

    engine, cfg = migrated
    module = ScriptDirectory.from_config(cfg).get_revision("024_teams").module
    snapshot_sql = "SELECT team_id, user_id, team_role FROM team_members ORDER BY id"
    with engine.begin() as conn:
        before = conn.execute(sa.text(snapshot_sql)).all()
        module._backfill(conn)
        assert conn.execute(sa.text(snapshot_sql)).all() == before
        assert conn.execute(sa.text("SELECT count(*) FROM teams")).scalar_one() == 2
        # Members CASCADE with the team; the key's team link must be SET NULL, not dangling/blocked.
        conn.execute(sa.text("DELETE FROM teams WHERE organization_id = 1"))
        assert (
            conn.execute(sa.text("SELECT team_id FROM api_keys WHERE key_id = 'k1'")).scalar_one()
            is None
        )
