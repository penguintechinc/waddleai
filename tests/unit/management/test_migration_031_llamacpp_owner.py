"""Migration 031: llamacpp_deployments.owner_org_id column and ownership backfill."""

import os

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "031_llamacpp_owner_org"
PREV = "030_local_generation_samples"
LINKED, ORPHAN = "22222222-2222-2222-2222-222222222222", "33333333-3333-3333-3333-333333333333"


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_revision_chain_031_follows_030_and_precedes_032() -> None:
    """031 revises 030 and is followed by 032 (Plan F), the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_revision("032_llamacpp_rpc_head").down_revision == REV
    assert script.get_heads() == ["032_llamacpp_rpc_head"]


def test_upgrade_backfills_from_link_leaves_orphan_null_downgrade_drops(tmp_path, monkeypatch):
    """Link-owned row gets its org, an orphan stays NULL (system-only); downgrade drops it."""
    url = f"sqlite:///{tmp_path / 'm031.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE llamacpp_deployments (id INTEGER PRIMARY KEY, public_id VARCHAR(36),"
                " fleet_backend_id INTEGER)"
            )
        )
        conn.execute(sa.text("CREATE TABLE organizations (id INTEGER PRIMARY KEY)"))
        conn.execute(
            sa.text("CREATE TABLE fleet_backends (id INTEGER PRIMARY KEY, org_id INTEGER)")
        )
        conn.execute(
            sa.text("CREATE TABLE connection_links (name VARCHAR(255), owner_org_id INTEGER)")
        )
        conn.execute(
            sa.text("INSERT INTO llamacpp_deployments (id, public_id) VALUES (1, :a), (2, :b)"),
            {"a": LINKED, "b": ORPHAN},
        )
        conn.execute(
            sa.text("INSERT INTO connection_links VALUES (:n, 7)"), {"n": f"llamacpp-{LINKED}"}
        )
    command.stamp(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)
    with engine.connect() as conn:
        rows = dict(
            conn.execute(sa.text("SELECT id, owner_org_id FROM llamacpp_deployments")).all()
        )
    assert rows == {1: 7, 2: None}
    command.downgrade(_cfg(url), PREV)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("llamacpp_deployments")}
    assert "owner_org_id" not in cols
