"""Migration 029: deployment scope on model_configs offers (security review)."""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "029_model_config_offer_scope"
PREV = "028_vllm_ray_fleet"


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch SQLite with a pre-029 model_configs table, stamped at 028."""
    url = f"sqlite:///{tmp_path / 'm029.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE model_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "model_name VARCHAR(255) NOT NULL UNIQUE, preferred_providers JSON NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO model_configs (model_name, preferred_providers) "
                "VALUES ('gpt-4o', '[\"openai\"]')"
            )
        )
    command.stamp(_cfg(url), PREV)
    yield _cfg(url), engine
    engine.dispose()


def test_revision_chain_head_is_029():
    """029 revises 028 and is the single head."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_heads() == [REV]


def test_upgrade_adds_nullable_scope_columns_keeping_existing_rows(scratch):
    """Existing offers stay global (NULL scope); the new columns are nullable."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    engine.dispose()
    cols = {c["name"]: c for c in sa.inspect(engine).get_columns("model_configs")}
    assert {"owner_org_id", "deployment_public_id"} <= set(cols)
    assert cols["owner_org_id"]["nullable"] and cols["deployment_public_id"]["nullable"]
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT owner_org_id, deployment_public_id FROM model_configs")
        ).one()
    assert tuple(row) == (None, None)


def test_downgrade_removes_the_columns(scratch):
    """Downgrade restores the pre-029 shape."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    command.downgrade(cfg, PREV)
    engine.dispose()
    assert {"owner_org_id", "deployment_public_id"}.isdisjoint(
        {c["name"] for c in sa.inspect(engine).get_columns("model_configs")}
    )
