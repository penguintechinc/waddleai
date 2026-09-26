"""Live-Postgres regression suite for H1 headless-auth service accounts.

# regression: headless-auth

Boots a throwaway ``pgvector/pgvector:pg17`` container (port 55460, ``--rm``,
torn down in ``finally``) and proves, against a real Postgres server rather
than SQLite:

1. Migration ``022_service_account_fields`` applies in isolation (stamped at
   the real ``021_audit_log`` head, upgraded exactly one step) with valid
   Postgres DDL -- SQLite's ADD COLUMN semantics are lenient enough to hide
   a boolean ``server_default`` rendering mistake that Postgres would not
   tolerate.
2. The real production bootstrap path (``models_sqlalchemy.init_schema()``
   == ``create_all()``, then ``alembic stamp head`` -- the only sequence
   that reaches migration head on a genuinely fresh database today, per
   gh-207 defect 4 / ``020_token_usage_api_key_id``'s docstring) creates a
   ``users`` table carrying both new columns.
3. ``shared.licensing.seats.count_billable_seats`` excludes WaddleAI's own
   internal service accounts (``health-check``) while counting a human user
   and a customer-provisioned machine identity (``ci``) -- against real
   PyDAL query execution, not a mock.
4. A service-account owner row still yields a valid, unchanged
   ``UserContext`` via ``RBACManager._build_user_context`` -- the exact
   claim shape H2's token exchange will consume.

Each test gets its own freshly ``CREATE DATABASE``-d database inside the one
shared container (fast: no per-test container restart, full isolation).
Skips (not fails) when Docker is unavailable -- same convention as
``tests/integration/test_gh207_schema_migration.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

REPO = Path(__file__).resolve().parents[2]
MANAGEMENT_DIR = REPO / "services" / "management"

_PG_IMAGE = "pgvector/pgvector:pg17"
_PG_PORT = 55460
_PG_PASSWORD = "ha-h1-regression"  # noqa: S105 -- ephemeral local test container password, never real


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _wait_for_postgres(container_name: str, deadline_s: float) -> None:
    """Poll ``pg_isready`` inside ``container_name`` until ready, or ``pytest.fail``."""
    deadline = time.time() + deadline_s
    last_output = ""
    check_argv = ["docker", "exec", container_name, "pg_isready", "-U", "postgres"]
    while time.time() < deadline:
        result = subprocess.run(check_argv, capture_output=True, text=True, timeout=5)  # noqa: S603
        if result.returncode == 0:
            return
        last_output = result.stdout + result.stderr
        time.sleep(1)
    pytest.fail(f"Postgres container {container_name!r} never became ready: {last_output}")


@pytest.fixture(scope="module")
def pg_admin_dsn() -> Iterator[str]:
    """DSN to the ``postgres`` maintenance DB in a session-lived container."""
    if not _docker_available():
        pytest.skip("docker is not available -- H1 live-pg regression needs a real Postgres")

    container_name = f"waddleai-ha-h1-pgvector-{uuid.uuid4().hex[:10]}"
    run_argv = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--name",
        container_name,
        "-e",
        f"POSTGRES_PASSWORD={_PG_PASSWORD}",
        "-p",
        f"{_PG_PORT}:5432",
        _PG_IMAGE,
    ]
    try:
        subprocess.run(run_argv, check=True, capture_output=True, timeout=60, text=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"could not start Postgres for the H1 regression suite: {exc.stderr}")
        return

    try:
        _wait_for_postgres(container_name, deadline_s=60.0)
        yield f"postgresql://postgres:{_PG_PASSWORD}@127.0.0.1:{_PG_PORT}/postgres"
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=30)  # noqa: S603, S607


def _fresh_database(admin_dsn: str) -> str:
    """CREATE DATABASE with a unique name and return its DSN -- full per-test isolation."""
    db_name = f"ha_h1_{uuid.uuid4().hex[:12]}"
    engine = sa.create_engine(admin_dsn, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    finally:
        engine.dispose()
    base = admin_dsn.rsplit("/", 1)[0]
    return f"{base}/{db_name}"


def _alembic_config(db_url: str):
    from alembic.config import Config  # noqa: PLC0415

    cfg = Config()
    cfg.set_main_option("script_location", str(MANAGEMENT_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


class TestMigration022IsolatedOnLivePostgres:
    """The migration file's own DDL, run in isolation against real Postgres."""

    def test_upgrade_then_downgrade_round_trips(self, pg_admin_dsn: str) -> None:
        """Upgrade adds both columns with valid Postgres DDL; downgrade cleanly removes them."""
        from alembic import command  # noqa: PLC0415

        db_url = _fresh_database(pg_admin_dsn)
        engine = sa.create_engine(db_url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "CREATE TABLE users ("
                        "id SERIAL PRIMARY KEY, "
                        "username VARCHAR(255) NOT NULL UNIQUE, "
                        "email VARCHAR(255) NOT NULL UNIQUE, "
                        "password_hash VARCHAR(255) NOT NULL, "
                        "role VARCHAR(50) NOT NULL, "
                        "organization_id INTEGER NOT NULL, "
                        "enabled BOOLEAN"
                        ")"
                    )
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO users (username, email, password_hash, role, "
                        "organization_id, enabled) VALUES "
                        "('pre-existing', 'pre@example.com', 'x', 'user', 1, true)"
                    )
                )

            os.environ["DATABASE_URL"] = db_url
            cfg = _alembic_config(db_url)
            command.stamp(cfg, "021_audit_log")
            command.upgrade(cfg, "022_service_account_fields")

            with engine.connect() as conn:
                cols = {
                    r["column_name"]: r
                    for r in conn.execute(
                        sa.text(
                            "SELECT column_name, is_nullable, column_default "
                            "FROM information_schema.columns WHERE table_name = 'users'"
                        )
                    ).mappings()
                }
                backfilled = conn.execute(
                    sa.text(
                        "SELECT is_service_account, service_kind FROM users "
                        "WHERE username = 'pre-existing'"
                    )
                ).one()

            assert cols["is_service_account"]["is_nullable"] == "NO"
            assert backfilled[0] is False
            assert cols["service_kind"]["is_nullable"] == "YES"
            assert backfilled[1] is None

            # A service-account row is creatable -- the literal TDD ask.
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO users (username, email, password_hash, role, "
                        "organization_id, enabled, is_service_account, service_kind) "
                        "VALUES ('svc-ci', 'svc-ci@example.com', 'x', 'user', 1, true, "
                        "true, 'ci')"
                    )
                )
            with engine.connect() as conn:
                svc_row = conn.execute(
                    sa.text(
                        "SELECT is_service_account, service_kind FROM users "
                        "WHERE username = 'svc-ci'"
                    )
                ).one()
            assert svc_row == (True, "ci")

            command.downgrade(cfg, "021_audit_log")
            with engine.connect() as conn:
                remaining = {
                    r[0]
                    for r in conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'users'"
                        )
                    )
                }
            assert "is_service_account" not in remaining
            assert "service_kind" not in remaining
        finally:
            engine.dispose()
            del os.environ["DATABASE_URL"]


class TestSeatMeteringAndClaimsOnRealSchema:
    """Full real-schema bootstrap: seat counting + claims derivation against live Postgres."""

    def test_seat_count_excludes_internal_kinds_and_claims_are_valid(
        self, pg_admin_dsn: str
    ) -> None:
        """Human + ci count as seats, health-check does not; the ci row's claims are valid."""
        db_url = _fresh_database(pg_admin_dsn)

        sys.path.insert(0, str(MANAGEMENT_DIR))
        try:
            from app.models_sqlalchemy import init_schema  # noqa: PLC0415

            init_schema(db_url)
        finally:
            sys.path.remove(str(MANAGEMENT_DIR))

        from alembic.command import stamp  # noqa: PLC0415

        os.environ["DATABASE_URL"] = db_url
        try:
            cfg = _alembic_config(db_url)
            stamp(cfg, "head")

            from shared.auth.rbac import RBACManager, Role  # noqa: PLC0415
            from shared.database.models import get_db  # noqa: PLC0415
            from shared.licensing.seats import count_billable_seats  # noqa: PLC0415

            db = get_db(db_url)

            org_id = db.organizations.insert(
                name="ha-h1-org",
                token_quota_monthly=1_000_000,
                token_quota_daily=100_000,
                enabled=True,
            )
            db.users.insert(
                username="ha-h1-human",
                email="human@example.com",
                password_hash="unused",  # noqa: S106 -- not a real credential
                role="user",
                organization_id=org_id,
                enabled=True,
            )
            ci_id = db.users.insert(
                username="ha-h1-ci",
                email="ci@example.com",
                password_hash="unused",  # noqa: S106 -- not a real credential
                role="user",
                organization_id=org_id,
                enabled=True,
                is_service_account=True,
                service_kind="ci",
            )
            db.users.insert(
                username="ha-h1-health-check",
                email="health-check@example.com",
                password_hash="unused",  # noqa: S106 -- not a real credential
                role="user",
                organization_id=org_id,
                enabled=True,
                is_service_account=True,
                service_kind="health-check",
            )
            db.commit()

            seat_count = count_billable_seats(db)
            assert seat_count == 2  # human + ci; health-check excluded

            manager = RBACManager(db)
            ci_row = db(db.users.id == ci_id).select().first()
            ctx = manager._build_user_context(ci_row)  # noqa: SLF001

            assert ctx.user_id == ci_id
            assert ctx.role == Role.USER
            assert ctx.organization_id == org_id
            assert ctx.managed_orgs == []
        finally:
            del os.environ["DATABASE_URL"]
