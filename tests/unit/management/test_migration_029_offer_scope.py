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
                "CREATE TABLE vllm_deployments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "public_id CHAR(32) NOT NULL, org_id INTEGER NOT NULL, "
                "served_model_name VARCHAR(255) NOT NULL)"
            )
        )
        for name, providers in (
            ("gpt-4o", '["openai"]'),
            ("llama-8b", '["vllm"]'),
            ("shared-name", '["vllm"]'),
            ("mixed", '["openai", "vllm"]'),
            ("orphan", '["vllm"]'),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO model_configs (model_name, preferred_providers) VALUES (:n, :p)"
                ),
                {"n": name, "p": providers},
            )
        for pid, org, served in (
            ("a" * 32, 1, "llama-8b"),
            ("b" * 32, 2, "gpt-4o"),  # commercial same-name offer: must NOT be claimed
            ("c" * 32, 1, "shared-name"),
            ("d" * 32, 2, "shared-name"),  # second claimant of the same name loses
            ("e" * 32, 3, "mixed"),  # mixed providers: not a vllm-only offer
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO vllm_deployments (public_id, org_id, served_model_name) "
                    "VALUES (:p, :o, :s)"
                ),
                {"p": pid, "o": org, "s": served},
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
            sa.text(
                "SELECT owner_org_id, deployment_public_id FROM model_configs "
                "WHERE model_name = 'gpt-4o'"
            )
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


def _offers(engine):
    """{model_name: (owner_org_id, deployment_public_id)} after the migration."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("SELECT model_name, owner_org_id, deployment_public_id FROM model_configs")
        )
        return {name: (org, dep) for name, org, dep in rows}


def test_backfill_claims_only_vllm_only_offers_matching_a_deployment(scratch):
    """Matching vllm-only offers get org + deployment; commercial/mixed/orphan stay global."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    offers = _offers(engine)
    assert offers["llama-8b"] == (1, "a" * 32)
    assert offers["shared-name"] == (1, "c" * 32)  # lowest deployment id wins the shared name
    assert offers["gpt-4o"] == (None, None)  # same name as org 2's deployment, but commercial
    assert offers["mixed"] == (None, None)
    assert offers["orphan"] == (None, None)


def test_backfill_without_vllm_deployments_table_is_a_noop(tmp_path, monkeypatch):
    """A DB without the fleet tables still upgrades."""
    url = f"sqlite:///{tmp_path / 'bare.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE model_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "model_name VARCHAR(255) NOT NULL, preferred_providers JSON NOT NULL)"
            )
        )
    command.stamp(_cfg(url), PREV)
    command.upgrade(_cfg(url), REV)
    engine.dispose()


def test_uniqueness_is_per_org_for_owned_offers_and_global_for_the_rest(scratch):
    """Two orgs may publish the same name; one org (or the global set) may not duplicate it."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    sql = (
        "INSERT INTO model_configs (model_name, preferred_providers, owner_org_id) "
        "VALUES (:n, '[\"vllm\"]', :o)"
    )
    with engine.begin() as conn:
        conn.execute(sa.text(sql), {"n": "m", "o": 10})
        conn.execute(sa.text(sql), {"n": "m", "o": 11})  # another org: fine
        conn.execute(sa.text(sql), {"n": "m", "o": None})  # global with same name: fine
    for org in (10, None):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(sql), {"n": "m", "o": org})


def test_downgrade_drops_org_offers_and_restores_global_unique(scratch):
    """Downgrade removes org-owned offers and brings back UNIQUE (model_name)."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    command.downgrade(cfg, PREV)
    assert "llama-8b" not in _offers_after_downgrade(engine)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO model_configs (model_name, preferred_providers) "
                "VALUES ('gpt-4o', '[]')"
            )
        )


def _offers_after_downgrade(engine):
    """Names present after the downgrade."""
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(sa.text("SELECT model_name FROM model_configs"))}
