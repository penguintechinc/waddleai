"""Migration 028: vLLM-on-Ray fleet tables, pricing columns, UUID backfills, RPC columns."""

import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import (
    Base,
    GpuInventory,
    VllmDeployment,
    VllmDeploymentEvent,
)

ALEMBIC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "..", "services", "management", "alembic")
)  # fmt: skip
REV = "028_vllm_ray_fleet"
PREV = "027_configuration_as_code"
_PRICING_COLS = {
    "public_id",
    "price_mode",
    "price_input_per_1m",
    "price_output_per_1m",
    "price_breakdown",
    "observed_tokens_per_s",
    "comparison_model",
}
_RPC_COLS = {"topology", "rpc_workers", "tensor_split", "allow_mixed_class"}
_PRE_028_DDL = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) NOT NULL)",
    "CREATE TABLE fleet_backends (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "org_id INTEGER NOT NULL REFERENCES organizations(id), name VARCHAR(255) NOT NULL)",
    "CREATE TABLE token_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, tokens_saved INTEGER)",
    "CREATE TABLE model_registry (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255) "
    "NOT NULL UNIQUE, role VARCHAR(100) NOT NULL, license VARCHAR(100) NOT NULL, "
    "origin VARCHAR(100) NOT NULL, is_utility BOOLEAN NOT NULL)",
    "CREATE TABLE connection_links (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(255))",
    "CREATE TABLE ollama_deployments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name VARCHAR(255) NOT NULL)",
    "CREATE TABLE llamacpp_deployments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name VARCHAR(255) NOT NULL)",
    "INSERT INTO organizations (name) VALUES ('acme'), ('globex')",
    "INSERT INTO fleet_backends (org_id, name) VALUES (1, 'b1'), (2, 'b2')",
    "INSERT INTO model_registry (name, role, license, origin, is_utility) "
    "VALUES ('m1', 'r', 'apache', 'x', 0), ('m2', 'r', 'apache', 'x', 0)",
    "INSERT INTO ollama_deployments (name) VALUES ('o1'), ('o2')",
    "INSERT INTO llamacpp_deployments (name) VALUES ('l1'), ('l2')",
)


def _cfg(url: str) -> Config:
    """Alembic config pointed at the management migrations."""
    cfg = Config()
    cfg.set_main_option("script_location", ALEMBIC_DIR)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Scratch SQLite with minimal pre-028 tables, stamped at 027; yields (cfg, engine)."""
    url = f"sqlite:///{tmp_path / 'm028.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _PRE_028_DDL:
            conn.execute(sa.text(ddl))
    cfg = _cfg(url)
    command.stamp(cfg, PREV)
    yield cfg, engine
    engine.dispose()


def _insp(engine):
    """Inspector on a fresh pool (a pooled connection can serve stale reflection)."""
    engine.dispose()
    return sa.inspect(engine)


def _cols(engine, table):
    """Return {column name: reflected column dict} for a table."""
    return {c["name"]: c for c in _insp(engine).get_columns(table)}


def _up(scratch):
    """Upgrade the scratch DB to 028 and return its engine."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    return engine


def _dep(**over):
    """Return a full vllm_deployments insert dict with overrides."""
    row = {
        "public_id": uuid.uuid4().hex, "org_id": 1, "fleet_backend_id": 1, "name": "d1",
        "slug": "d1", "model_ref": "org/m", "served_model_name": "m", "vendor": 1,
        "gpu_class": "nvidia-rtx4090", "quantization": 0, "dtype": "auto",
        "tensor_parallel": 1, "pipeline_parallel": 1, "replicas_min": 1, "replicas_max": 1,
        "max_model_len": 4096, "status": 1, "serve_app_name": "vllm-d1",
        "comparison_model": "openai/gpt-4o", "price_input_per_1m": 1, "price_output_per_1m": 2,
        "created_at": "2026-01-01", "updated_at": "2026-01-01",
    }  # fmt: skip
    row.update(over)
    return row


def _insert(conn, table, row):
    """Insert a row dict into table via a bound-parameter statement."""
    cols = ", ".join(row)
    binds = ", ".join(f":{k}" for k in row)
    conn.execute(sa.text(f"INSERT INTO {table} ({cols}) VALUES ({binds})"), row)  # noqa: S608


def test_upgrade_creates_three_tables_and_token_usage_columns(scratch):
    """The three fleet tables exist and token_usage gains the snapshot columns."""
    engine = _up(scratch)
    tables = set(_insp(engine).get_table_names())
    assert {"gpu_inventory", "vllm_deployments", "vllm_deployment_events"} <= tables
    assert {
        "local_deployment_kind", "local_deployment_id", "unit_price_input", "unit_price_output",
    } <= set(_cols(engine, "token_usage"))  # fmt: skip


def test_ollama_and_llamacpp_gain_pricing_columns(scratch):
    """AM10: Ollama and llama.cpp deployments carry the same pricing columns as vLLM."""
    engine = _up(scratch)
    for table in ("ollama_deployments", "llamacpp_deployments"):
        assert _PRICING_COLS <= set(_cols(engine, table)), table
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(sa.text(f"SELECT public_id FROM {table}"))]  # noqa: S608
        assert len(ids) == 2 and all(ids) and len(set(ids)) == 2, table


def test_llamacpp_rpc_columns_folded_in(scratch):
    """AM17: Plan F's topology columns are folded into 028 with their defaults."""
    engine = _up(scratch)
    cols = _cols(engine, "llamacpp_deployments")
    assert _RPC_COLS <= set(cols)
    assert cols["topology"]["nullable"] is False
    assert cols["allow_mixed_class"]["nullable"] is False
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT topology, allow_mixed_class, rpc_workers, tensor_split "
                "FROM llamacpp_deployments"
            )  # fmt: skip
        ).fetchall()
    assert [tuple(r) for r in rows] == [(1, 0, None, None)] * 2


def test_unique_constraints_named_and_enforced(scratch):
    """Duplicate (org,name)/(org,slug)/(backend,node,gpu) rows are rejected."""
    engine = _up(scratch)
    with engine.begin() as conn:
        _insert(conn, "vllm_deployments", _dep())
        _insert(conn, "vllm_deployments", _dep(org_id=2, fleet_backend_id=2))  # other org: ok
    for over in ({"slug": "other"}, {"name": "other"}):
        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert(conn, "vllm_deployments", _dep(public_id=uuid.uuid4().hex, **over))
    gpu = {
        "org_id": 1, "fleet_backend_id": 1, "node_uid": "u", "node_name": "n", "gpu_index": 0,
        "vendor": 1, "product_name": "p", "vram_total_mb": 1, "pci_bus_id": "0", "schedulable": 1,
        "state": 1, "last_seen": "2026-01-01",
    }  # fmt: skip
    with engine.begin() as conn:
        _insert(conn, "gpu_inventory", gpu)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, "gpu_inventory", gpu)
    names = {u["name"] for u in _insp(engine).get_unique_constraints("vllm_deployments")}
    assert {"uq_vllm_deployments_org_name", "uq_vllm_deployments_org_slug"} <= names
    gpu_uq = {u["name"] for u in _insp(engine).get_unique_constraints("gpu_inventory")}
    assert "uq_gpu_inventory_backend_node_gpu" in gpu_uq


def test_price_columns_not_null_numeric_12_6(scratch):
    """Per-1M prices are NOT NULL NUMERIC(12,6)."""
    engine = _up(scratch)
    cols = _cols(engine, "vllm_deployments")
    for name in ("price_input_per_1m", "price_output_per_1m"):
        assert cols[name]["nullable"] is False
        assert (cols[name]["type"].precision, cols[name]["type"].scale) == (12, 6)


def test_am1_columns(scratch):
    """AM1 columns: vllm flags, model_registry public_id/params_b, connection_links owner."""
    engine = _up(scratch)
    dep = _cols(engine, "vllm_deployments")
    assert dep["expose_via_proxy"]["nullable"] is False
    assert dep["comparison_model"]["nullable"] is False
    assert dep["prefer_local"]["nullable"] is True
    reg = _cols(engine, "model_registry")
    assert reg["public_id"]["nullable"] is False and reg["params_b"]["nullable"] is True
    assert _cols(engine, "connection_links")["owner_org_id"]["nullable"] is True


def test_am6_pricing_columns(scratch):
    """AM6 columns: price_mode defaults to AUTO_TCO (1); breakdown/observed rate nullable."""
    engine = _up(scratch)
    dep = _cols(engine, "vllm_deployments")
    assert dep["price_mode"]["nullable"] is False
    assert dep["price_breakdown"]["nullable"] is True
    assert dep["observed_tokens_per_s"]["nullable"] is True
    with engine.begin() as conn:
        row = _dep()
        _insert(conn, "vllm_deployments", row)
        mode = conn.execute(sa.text("SELECT price_mode FROM vllm_deployments")).scalar_one()
    assert mode == 1


def test_model_registry_public_id_backfilled(scratch):
    """Existing model_registry rows get distinct UUIDs; the column is unique and NOT NULL."""
    engine = _up(scratch)
    with engine.connect() as conn:
        ids = [r[0] for r in conn.execute(sa.text("SELECT public_id FROM model_registry"))]
    assert len(ids) == 2 and all(ids) and len(set(ids)) == 2
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE model_registry SET public_id = :v"), {"v": ids[0]})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text("UPDATE model_registry SET public_id = NULL WHERE name = 'm1'"))
    uq = {u["name"] for u in _insp(engine).get_unique_constraints("model_registry")}
    assert "uq_model_registry_public_id" in uq


def test_connection_links_absent_is_tolerated(tmp_path, monkeypatch):
    """connection_links is PyDAL-created, so the migration skips it when it does not exist."""
    url = f"sqlite:///{tmp_path / 'm028b.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for ddl in _PRE_028_DDL:
            if "connection_links" not in ddl:
                conn.execute(sa.text(ddl))
    cfg = _cfg(url)
    command.stamp(cfg, PREV)
    command.upgrade(cfg, REV)
    assert "connection_links" not in _insp(engine).get_table_names()
    engine.dispose()


def test_downgrade_removes_tables_and_columns(scratch):
    """Downgrade to 027 removes the tables and every added column."""
    cfg, engine = scratch
    command.upgrade(cfg, REV)
    command.downgrade(cfg, PREV)
    tables = set(_insp(engine).get_table_names())
    assert not {"gpu_inventory", "vllm_deployments", "vllm_deployment_events"} & tables
    assert not {"local_deployment_kind", "unit_price_input"} & set(_cols(engine, "token_usage"))
    assert not {"public_id", "params_b"} & set(_cols(engine, "model_registry"))
    assert "owner_org_id" not in _cols(engine, "connection_links")
    for table in ("ollama_deployments", "llamacpp_deployments"):
        assert not _PRICING_COLS & set(_cols(engine, table)), table
    assert not _RPC_COLS & set(_cols(engine, "llamacpp_deployments"))


@pytest.mark.parametrize("model", [GpuInventory, VllmDeployment, VllmDeploymentEvent])
def test_orm_matches_migrated_schema(scratch, model):
    """ORM column names and nullability equal the migrated table."""
    engine = _up(scratch)
    mig = _cols(engine, model.__tablename__)
    orm = {c.name: c for c in model.__table__.columns}
    assert set(mig) == set(orm)
    for name, col in orm.items():
        assert mig[name]["nullable"] == (col.nullable and not col.primary_key), name


def test_orm_additions_on_existing_models_match(scratch):
    """New columns on existing ORM models appear in the migrated tables."""
    engine = _up(scratch)
    meta = Base.metadata.tables
    added = {
        "token_usage": {"local_deployment_kind", "local_deployment_id", "unit_price_input",
                        "unit_price_output"},
        "model_registry": {"public_id", "params_b"},
        "ollama_deployments": _PRICING_COLS,
        "llamacpp_deployments": _PRICING_COLS | _RPC_COLS,
    }  # fmt: skip
    for table, names in added.items():
        assert names <= set(meta[table].columns.keys()), table
        assert names <= set(_cols(engine, table)), table
