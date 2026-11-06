"""model_configs runtime definition matches migration 029 (owner scope, name not unique)."""

import inspect
from pathlib import Path

from shared.database.models import get_db
from shared.routing.prefer_local import create_prefer_local_rule


def test_same_model_name_may_exist_for_two_owners(tmp_path: Path) -> None:
    """model_name is not globally unique at runtime; owner_org_id/deployment_public_id exist."""
    db = get_db(db_uri=f"sqlite:///{tmp_path / 'mc.db'}")
    row = {
        "preferred_providers": ["vllm"],
        "cost_per_token": {},
        "context_length": 1,
        "capabilities": [],
        "enabled": True,
    }
    db.model_configs.insert(model_name="m", owner_org_id=1, deployment_public_id="a", **row)
    db.model_configs.insert(model_name="m", owner_org_id=2, deployment_public_id="b", **row)
    db.model_configs.insert(model_name="m", **row)  # global offer of the same name
    rows = list(db(db.model_configs.model_name == "m").select())
    assert sorted(r.owner_org_id or 0 for r in rows) == [0, 1, 2]


def test_create_prefer_local_rule_signature_is_the_master_interface() -> None:
    """The reconciler calls this factory by name/signature: (db, org_id, deployment, ...)."""
    params = list(inspect.signature(create_prefer_local_rule).parameters)
    assert params == ["db", "org_id", "deployment", "fallback_model", "match"]
