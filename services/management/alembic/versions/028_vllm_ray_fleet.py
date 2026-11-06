"""Add the vLLM-on-Ray GPU fleet schema and shared local-deployment pricing columns.

Creates ``gpu_inventory``, ``vllm_deployments`` and ``vllm_deployment_events``;
adds the chargeback snapshot columns to ``token_usage``; gives ``model_registry``,
``ollama_deployments`` and ``llamacpp_deployments`` a backfilled ``public_id``
(plus the AM10 pricing columns and Plan F's llama.cpp RPC columns, folded in per
AM17 because Plan B was unmerged); and adds ``connection_links.owner_org_id``
when that PyDAL-managed table exists.

Revision ID: 028_vllm_ray_fleet
Revises: 027_configuration_as_code
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028_vllm_ray_fleet"
down_revision: str | None = "027_configuration_as_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOCAL_PRICED = ("ollama_deployments", "llamacpp_deployments")
_FK_OWNER = "fk_connection_links_owner_org_id"


def _uuid_default() -> sa.TextClause | None:
    """Return a DB-side UUID default on PostgreSQL (rows inserted by penguin_dal omit it)."""
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("gen_random_uuid()")
    return None


def _has_table(name: str) -> bool:
    """Return True when ``name`` exists (connection_links is created by PyDAL, not Alembic)."""
    return name in sa.inspect(op.get_bind()).get_table_names()


def _add_public_id(table: str, unique_name: str) -> None:
    """Add a nullable UUID, backfill one distinct value per row, then NOT NULL + unique."""
    with op.batch_alter_table(table) as batch:
        batch.add_column(sa.Column("public_id", sa.Uuid(), nullable=True))
    bind = op.get_bind()
    tbl = sa.table(table, sa.column("id", sa.Integer), sa.column("public_id", sa.Uuid()))
    for (row_id,) in bind.execute(sa.select(tbl.c.id)).fetchall():
        bind.execute(sa.update(tbl).where(tbl.c.id == row_id).values(public_id=uuid.uuid4()))
    with op.batch_alter_table(table) as batch:
        batch.alter_column(
            "public_id", existing_type=sa.Uuid(), nullable=False, server_default=_uuid_default()
        )
        batch.create_unique_constraint(unique_name, ["public_id"])


def _pricing_columns() -> list[sa.Column]:
    """Return the AM10 pricing columns shared by Ollama and llama.cpp deployments."""
    return [
        sa.Column("price_mode", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("price_input_per_1m", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("price_output_per_1m", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("price_breakdown", sa.JSON(), nullable=True),
        sa.Column("observed_tokens_per_s", sa.Numeric(10, 2), nullable=True),
        sa.Column("comparison_model", sa.String(255), nullable=True),
    ]


def _fk(col: str, target: str, ondelete: str = "CASCADE") -> sa.ForeignKeyConstraint:
    """Return a single-column FK constraint to ``target`` (``table.col``)."""
    return sa.ForeignKeyConstraint([col], [target], ondelete=ondelete)


def upgrade() -> None:
    """Create the fleet tables and alter the existing tables."""
    links = _has_table("connection_links")  # PyDAL-created: FK only when it already exists
    op.create_table(
        "gpu_inventory",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("fleet_backend_id", sa.Integer(), nullable=False),
        sa.Column("node_uid", sa.String(255), nullable=False),
        sa.Column("node_name", sa.String(255), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("vendor", sa.SmallInteger(), nullable=False),
        sa.Column("gpu_class", sa.String(64), nullable=True),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("vram_total_mb", sa.Integer(), nullable=False),
        sa.Column("vram_used_mb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pci_bus_id", sa.String(64), nullable=False),
        sa.Column("schedulable", sa.Boolean(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        _fk("org_id", "organizations.id"),
        _fk("fleet_backend_id", "fleet_backends.id"),
        sa.UniqueConstraint(
            "fleet_backend_id", "node_uid", "gpu_index", name="uq_gpu_inventory_backend_node_gpu"
        ),
    )
    op.create_table(
        "vllm_deployments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "public_id", sa.Uuid(), nullable=False, unique=True, server_default=_uuid_default()
        ),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("fleet_backend_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("model_ref", sa.String(512), nullable=False),
        sa.Column("served_model_name", sa.String(255), nullable=False),
        sa.Column("vendor", sa.SmallInteger(), nullable=False),
        sa.Column("gpu_class", sa.String(64), nullable=False),
        sa.Column("allow_mixed_class", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("quantization", sa.SmallInteger(), nullable=False),
        sa.Column("dtype", sa.String(16), nullable=False),
        sa.Column("tensor_parallel", sa.Integer(), nullable=False),
        sa.Column("pipeline_parallel", sa.Integer(), nullable=False),
        sa.Column("replicas_min", sa.Integer(), nullable=False),
        sa.Column("replicas_max", sa.Integer(), nullable=False),
        sa.Column("max_model_len", sa.Integer(), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("serve_app_name", sa.String(128), nullable=False),
        sa.Column("endpoint_url", sa.String(1024), nullable=True),
        sa.Column("connection_link_id", sa.Integer(), nullable=True),
        sa.Column("expose_via_proxy", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prefer_local", sa.JSON(), nullable=True),
        sa.Column("comparison_model", sa.String(255), nullable=False),
        sa.Column("price_mode", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("price_breakdown", sa.JSON(), nullable=True),
        sa.Column("observed_tokens_per_s", sa.Numeric(10, 2), nullable=True),
        sa.Column("price_input_per_1m", sa.Numeric(12, 6), nullable=False),
        sa.Column("price_output_per_1m", sa.Numeric(12, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _fk("org_id", "organizations.id"),
        _fk("fleet_backend_id", "fleet_backends.id"),
        *([_fk("connection_link_id", "connection_links.id", "SET NULL")] if links else []),
        sa.UniqueConstraint("org_id", "name", name="uq_vllm_deployments_org_name"),
        sa.UniqueConstraint("slug", name="uq_vllm_deployments_slug"),
    )
    op.create_table(
        "vllm_deployment_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        _fk("deployment_id", "vllm_deployments.id"),
    )
    op.create_index(
        "ix_vllm_deployment_events_deployment_id", "vllm_deployment_events", ["deployment_id"]
    )

    with op.batch_alter_table("token_usage") as batch:
        batch.add_column(sa.Column("local_deployment_kind", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("local_deployment_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("unit_price_input", sa.Numeric(12, 6), nullable=True))
        batch.add_column(sa.Column("unit_price_output", sa.Numeric(12, 6), nullable=True))

    with op.batch_alter_table("model_registry") as batch:
        batch.add_column(sa.Column("params_b", sa.Numeric(8, 2), nullable=True))
    _add_public_id("model_registry", "uq_model_registry_public_id")

    for table in _LOCAL_PRICED:
        _add_public_id(table, f"uq_{table}_public_id")
        with op.batch_alter_table(table) as batch:
            for col in _pricing_columns():
                batch.add_column(col)

    with op.batch_alter_table("llamacpp_deployments") as batch:
        batch.add_column(sa.Column("topology", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("rpc_workers", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("tensor_split", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("allow_mixed_class", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    if links:
        with op.batch_alter_table("connection_links") as batch:
            batch.add_column(sa.Column("owner_org_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(_FK_OWNER, "organizations", ["owner_org_id"], ["id"])


def downgrade() -> None:
    """Drop the added columns first, then the fleet tables, in reverse order."""
    if _has_table("connection_links"):
        with op.batch_alter_table("connection_links") as batch:
            batch.drop_constraint(_FK_OWNER, type_="foreignkey")
            batch.drop_column("owner_org_id")

    with op.batch_alter_table("llamacpp_deployments") as batch:
        for col in ("allow_mixed_class", "tensor_split", "rpc_workers", "topology"):
            batch.drop_column(col)

    for table in reversed(_LOCAL_PRICED):
        with op.batch_alter_table(table) as batch:
            for priced in _pricing_columns():
                batch.drop_column(priced.name)
            batch.drop_constraint(f"uq_{table}_public_id", type_="unique")
            batch.drop_column("public_id")

    with op.batch_alter_table("model_registry") as batch:
        batch.drop_constraint("uq_model_registry_public_id", type_="unique")
        batch.drop_column("public_id")
        batch.drop_column("params_b")

    with op.batch_alter_table("token_usage") as batch:
        for col in (
            "unit_price_output",
            "unit_price_input",
            "local_deployment_id",
            "local_deployment_kind",
        ):
            batch.drop_column(col)

    op.drop_index("ix_vllm_deployment_events_deployment_id", table_name="vllm_deployment_events")
    op.drop_table("vllm_deployment_events")
    op.drop_table("vllm_deployments")
    op.drop_table("gpu_inventory")
