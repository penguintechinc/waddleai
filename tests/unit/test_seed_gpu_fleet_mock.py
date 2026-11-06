"""Tests for scripts/seed_gpu_fleet_mock.py (idempotent mock data for the GPU Fleet UI)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.unit.management.vllm_harness import build_db

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_gpu_fleet_mock.py"
_spec = importlib.util.spec_from_file_location("seed_gpu_fleet_mock", _SCRIPT)
assert _spec is not None and _spec.loader is not None
seed_mod = importlib.util.module_from_spec(_spec)
sys.modules["seed_gpu_fleet_mock"] = seed_mod
_spec.loader.exec_module(seed_mod)

EXPECTED = {"gpu_inventory": 4, "vllm_deployments": 2, "model_registry": 1}


@pytest.fixture
def db(tmp_path):
    """A sqlite penguin-dal DB with the ORM schema and one organization."""
    handle = build_db(tmp_path)
    handle.organizations.insert(name="acme")
    handle.commit()
    return handle


def test_seed_twice_is_idempotent(db):
    """Running the seed twice leaves exactly the mock rows, never doubled."""
    assert seed_mod.seed(db) == EXPECTED
    assert seed_mod.seed(db) == EXPECTED
    assert db(db.gpu_inventory.id > 0).count() == 4
    assert db(db.vllm_deployments.id > 0).count() == 2
    assert db(db.vllm_deployment_events.id > 0).count() == 6
    assert db(db.model_registry.name == "qwen-12b-awq").count() == 1
    assert db(db.fleet_backends.type == "vllm_ray").count() == 1


def test_seed_reuses_an_existing_vllm_backend(db):
    """An org that already has a vllm_ray backend keeps it; no second backend is created."""
    backend_id = db.fleet_backends.insert(
        org_id=1, name="mine", type="vllm_ray", management_scope="full_lifecycle", status="active"
    )
    db.commit()
    seed_mod.seed(db)
    assert db(db.fleet_backends.id > 0).count() == 1
    assert db(db.gpu_inventory.fleet_backend_id == backend_id).count() == 4


def test_seed_needs_an_organization(tmp_path):
    """With no organization the seed fails loudly instead of inventing one."""
    empty = build_db(tmp_path)
    with pytest.raises(SystemExit) as exc:
        seed_mod.seed(empty)
    assert "no organization" in str(exc.value)


def test_seed_zero_rows_exits_nonzero(monkeypatch):
    """A seed that writes nothing makes main() exit 1 (zero examined is a failure)."""
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(
        seed_mod,
        "seed",
        lambda _db: {"gpu_inventory": 0, "vllm_deployments": 0, "model_registry": 0},
    )
    monkeypatch.setattr("penguin_dal.DAL", lambda _url: object())
    with pytest.raises(SystemExit) as exc:
        seed_mod.main()
    assert exc.value.code == 1


def test_main_requires_database_url(monkeypatch):
    """main() refuses to run without DATABASE_URL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        seed_mod.main()
    assert "DATABASE_URL" in str(exc.value)


def test_main_succeeds_when_rows_seeded(monkeypatch):
    """main() returns normally when rows were seeded."""
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(seed_mod, "seed", lambda _db: EXPECTED)
    monkeypatch.setattr("penguin_dal.DAL", lambda _url: object())
    seed_mod.main()
