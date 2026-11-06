"""Migration 030: local_generation_samples table (observed throughput)."""

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
REV = "030_local_generation_samples"
PREV = "029_model_config_offer_scope"


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_revision_chain_030_follows_029_and_precedes_031() -> None:
    """030 revises 029 and precedes head 031."""
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert script.get_revision(REV).down_revision == PREV
    assert script.get_heads() == ["031_llamacpp_owner_org"]


@pytest.fixture
def at_029(tmp_path, monkeypatch):
    """A scratch sqlite stamped at 029 (the table does not exist yet)."""
    url = f"sqlite:///{tmp_path / 'm030.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.stamp(_cfg(url), PREV)
    return url


def test_upgrade_creates_table_index_and_constraints_downgrade_drops(at_029) -> None:
    """Upgrade adds the table (+ index, checks refuse non-positive); downgrade removes it."""
    url = at_029
    command.upgrade(_cfg(url), REV)
    engine = sa.create_engine(url)
    insp = sa.inspect(engine)
    assert "local_generation_samples" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("local_generation_samples")} == {
        "id",
        "local_deployment_kind",
        "local_deployment_id",
        "output_tokens",
        "generation_ms",
        "timing_source",
        "user_id",
        "created_at",
    }
    assert "ix_local_generation_samples_dep_created" in {
        i["name"] for i in insp.get_indexes("local_generation_samples")
    }
    ins = sa.text(
        "INSERT INTO local_generation_samples (local_deployment_kind, local_deployment_id, "
        "output_tokens, generation_ms) VALUES (1, 7, :t, :m)"
    )
    with engine.begin() as conn:
        conn.execute(ins, {"t": 64, "m": 800})
    for tokens, ms in ((0, 800), (64, 0)):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(ins, {"t": tokens, "m": ms})
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:  # unknown timing source
        conn.execute(
            sa.text(
                "INSERT INTO local_generation_samples (local_deployment_kind, local_deployment_id, "
                "output_tokens, generation_ms, timing_source) VALUES (1, 7, 64, 800, 9)"
            )
        )
    command.downgrade(_cfg(url), PREV)
    assert "local_generation_samples" not in sa.inspect(sa.create_engine(url)).get_table_names()
