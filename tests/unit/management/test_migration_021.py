"""Migration 021 round-trip test: the ``audit_log`` admin-action table (G10).

regression: release-audit-2026-09-23

Same technique as ``test_migration_019`` -- a scratch SQLite DB stamped at the
real ``020_token_usage_api_key_id`` head so exactly one step runs. The table's
FKs reference ``users``/``organizations``; SQLite creates the table without
those parents present (FK enforcement is off by default), matching how
``test_migration_018`` exercises its own FK-bearing create_table.
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
    """Empty scratch DB; the test stamps it at 020 before upgrading to 021."""
    db_path = tmp_path / "migration021.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    yield db_url, engine
    engine.dispose()


def _columns(conn) -> dict[str, dict]:
    """Map audit_log column name -> its PRAGMA table_info row."""
    rows = conn.execute(sa.text("PRAGMA table_info('audit_log')")).mappings().all()
    return {r["name"]: dict(r) for r in rows}


def test_upgrade_creates_audit_log_with_expected_columns(scratch_db) -> None:
    """021 creates audit_log with the who/what/when/outcome columns."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "020_token_usage_api_key_id")
    command.upgrade(cfg, "021_audit_log")

    with engine.connect() as conn:
        cols = _columns(conn)

    assert set(cols) == {
        "id",
        "created_at",
        "user_id",
        "organization_id",
        "method",
        "path",
        "resource_id",
        "status_code",
    }
    # who/what/outcome are mandatory; the identity FKs are nullable so purging a
    # user/org never destroys their audit history.
    assert cols["method"]["notnull"] == 1
    assert cols["path"]["notnull"] == 1
    assert cols["status_code"]["notnull"] == 1
    assert cols["created_at"]["notnull"] == 1
    assert cols["user_id"]["notnull"] == 0
    assert cols["organization_id"]["notnull"] == 0


def test_upgrade_creates_indexes(scratch_db) -> None:
    """021 lands the created_at / (user, created_at) / (org, created_at) indexes."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "020_token_usage_api_key_id")
    command.upgrade(cfg, "021_audit_log")

    with engine.connect() as conn:
        names = {
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'"
                )
            )
        }

    assert {"idx_audit_log_created_at", "idx_audit_log_user", "idx_audit_log_org"} <= names


def test_downgrade_drops_audit_log(scratch_db) -> None:
    """Downgrade to 020 removes the table entirely (clean round trip)."""
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "020_token_usage_api_key_id")
    command.upgrade(cfg, "021_audit_log")
    command.downgrade(cfg, "020_token_usage_api_key_id")

    with engine.connect() as conn:
        exists = conn.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'")
        ).first()

    assert exists is None
