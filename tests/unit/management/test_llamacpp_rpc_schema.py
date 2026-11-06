"""Plan F, F1: topology enum and the llamacpp_deployments RPC columns (migrations 028 + 032)."""

from __future__ import annotations

from sqlalchemy import inspect

from services.management.app.models_sqlalchemy import LlamaCppDeployment
from shared.fleet.enums import LlamaCppTopology

RPC_COLS = {"topology", "rpc_workers", "tensor_split", "allow_mixed_class", "rpc_head_node_uid"}


def test_topology_values():
    """The enum codes are stored as 1 (single node) and 2 (RPC multi-node)."""
    assert (LlamaCppTopology.SINGLE_NODE, LlamaCppTopology.RPC_MULTI_NODE) == (1, 2)


def test_orm_declares_every_rpc_column():
    """The ORM model carries the four 028 columns and the 032 head node column."""
    columns = {c.name for c in LlamaCppDeployment.__table__.columns}
    assert RPC_COLS <= columns
    assert LlamaCppDeployment.__table__.c.rpc_head_node_uid.nullable is True


def test_new_row_defaults_to_single_node(vllm_db):
    """A row inserted without RPC fields is a plain single-node deployment."""
    row_id = vllm_db.llamacpp_deployments.insert(
        name="plain", deployment_type="remote", status="stopped", model_name="m",
        public_id="22222222-2222-2222-2222-222222222222",
    )  # fmt: skip
    vllm_db.commit()
    row = vllm_db(vllm_db.llamacpp_deployments.id == row_id).select().first()
    assert (row.topology, row.allow_mixed_class) == (1, False)
    assert (row.rpc_workers, row.tensor_split, row.rpc_head_node_uid) == (None, None, None)


def test_orm_matches_the_sqlite_schema(vllm_db):
    """The reflected table exposes every RPC column (ORM and migrations agree)."""
    reflected = {c["name"] for c in inspect(vllm_db.engine).get_columns("llamacpp_deployments")}
    assert RPC_COLS <= reflected
