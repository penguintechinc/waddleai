"""Migration 022 round-trip test: users.is_service_account / users.service_kind (H1).

# regression: headless-auth

Same technique as ``test_migration_020``-style ADD COLUMN migrations (e.g.
``021_audit_log``'s own precedent test) -- a scratch SQLite DB stamped at the
real ``021_audit_log`` head so exactly one step runs. Unlike 021 (which
creates a brand-new table), 022 alters the pre-existing ``users`` table, so
this test builds a minimal pre-migration ``users`` table by hand (only the
columns 022 doesn't touch) before stamping+upgrading -- mirroring how
``test_migration_019`` builds its own minimal pre-migration tables for a
data migration over pre-existing schema.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ALEMBIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
)


def _alembic_config(db_url: str) -> Config:
    """Alembic config pointed at the management service's migration scripts."""
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """A scratch DB with a minimal pre-022 ``users`` table, stamped at 021."""
    db_path = tmp_path / "migration022.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
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
                "INSERT INTO users (username, email, password_hash, role, organization_id, "
                "enabled) VALUES ('pre-existing', 'pre@example.com', 'x', 'user', 1, 1)"
            )
        )
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "021_audit_log")
    yield db_url, engine
    engine.dispose()


def _columns(conn) -> dict[str, dict]:
    rows = conn.execute(sa.text("PRAGMA table_info('users')")).mappings().all()
    return {r["name"]: dict(r) for r in rows}


def test_upgrade_adds_is_service_account_not_null_default_false(scratch_db) -> None:
    """022 adds is_service_account as NOT NULL with a false default; existing rows backfill to 0."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "022_service_account_fields")

    with engine.connect() as conn:
        cols = _columns(conn)
        pre_existing_value = conn.execute(
            sa.text("SELECT is_service_account FROM users WHERE username = 'pre-existing'")
        ).scalar()

    assert "is_service_account" in cols
    assert cols["is_service_account"]["notnull"] == 1
    # SQLite stores BOOLEAN as an integer; the pre-existing row must have
    # been backfilled by the server_default, not left NULL.
    assert pre_existing_value in (0, False)


def test_upgrade_adds_nullable_service_kind(scratch_db) -> None:
    """022 adds service_kind as a nullable, free-form string column."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "022_service_account_fields")

    with engine.connect() as conn:
        cols = _columns(conn)
        pre_existing_value = conn.execute(
            sa.text("SELECT service_kind FROM users WHERE username = 'pre-existing'")
        ).scalar()

    assert "service_kind" in cols
    assert cols["service_kind"]["notnull"] == 0
    assert pre_existing_value is None


def test_a_service_account_row_is_insertable_after_upgrade(scratch_db) -> None:
    """The exact TDD ask: a service-account user is creatable once 022 lands."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "022_service_account_fields")

    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (username, email, password_hash, role, organization_id, "
                "enabled, is_service_account, service_kind) VALUES "
                "('svc-ci', 'svc-ci@example.com', 'x', 'user', 1, 1, 1, 'ci')"
            )
        )

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT is_service_account, service_kind FROM users WHERE username = 'svc-ci'")
        ).one()

    assert row[0] in (1, True)
    assert row[1] == "ci"


def test_downgrade_removes_both_columns(scratch_db) -> None:
    """Downgrade to 021 cleanly drops both new columns (no FK, plain drop_column)."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "022_service_account_fields")
    command.downgrade(cfg, "021_audit_log")

    with engine.connect() as conn:
        cols = _columns(conn)

    assert "is_service_account" not in cols
    assert "service_kind" not in cols
