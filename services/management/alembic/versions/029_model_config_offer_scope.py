"""Scope ``model_configs`` offers to the deployment (and org) that created them.

vLLM deployments publish a routable offer in ``model_configs``. Without an owner, deleting one
deployment could remove an offer another deployment (or a commercial provider) still uses, and
two orgs could not serve the same model name. ``owner_org_id`` and ``deployment_public_id`` are
NULL for every global/commercial offer, so existing rows keep their meaning.

* Backfills the ownership of offers already published by a deployment (same served name and
  ``preferred_providers == ['vllm']``), first deployment (lowest id) winning a shared name.
* Replaces the global ``UNIQUE (model_name)`` with two partial unique indexes: global offers are
  unique by name; org-owned offers are unique per ``(owner_org_id, model_name)`` -- so org A and
  org B can each publish ``llama-3-8b`` while one org never gets two offers of the same name.

Revision ID: 029_model_config_offer_scope
Revises: 028_vllm_ray_fleet
"""

import json
from collections.abc import Sequence
from typing import Literal

import sqlalchemy as sa
from alembic import op

revision: str = "029_model_config_offer_scope"
down_revision: str | None = "028_vllm_ray_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_model_configs_deployment_public_id"
_GLOBAL_UQ = "uq_model_configs_global_name"
_ORG_UQ = "uq_model_configs_org_name"
_OLD_UQ = "uq_model_configs_model_name"
_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _old_unique_name() -> str | None:
    """Name of the pre-029 ``UNIQUE (model_name)`` constraint, or None when it is unnamed."""
    for uc in sa.inspect(op.get_bind()).get_unique_constraints("model_configs"):
        if list(uc["column_names"]) == ["model_name"]:
            return uc["name"]
    return None


def _has_old_unique() -> bool:
    """True while the single-column unique constraint on ``model_name`` exists."""
    return any(
        list(uc["column_names"]) == ["model_name"]
        for uc in sa.inspect(op.get_bind()).get_unique_constraints("model_configs")
    )


def _backfill() -> None:
    """Stamp org + deployment ownership on offers a vLLM deployment already published."""
    bind = op.get_bind()
    if "vllm_deployments" not in sa.inspect(bind).get_table_names():
        return
    deps = sa.table(
        "vllm_deployments",
        sa.column("id", sa.Integer),
        sa.column("public_id", sa.Uuid()),
        sa.column("org_id", sa.Integer),
        sa.column("served_model_name", sa.String),
    )
    offers = sa.table(
        "model_configs",
        sa.column("id", sa.Integer),
        sa.column("model_name", sa.String),
        sa.column("preferred_providers", sa.JSON),
        sa.column("owner_org_id", sa.Integer),
        sa.column("deployment_public_id", sa.Uuid()),
    )
    claimed: set[int] = set()
    for dep in bind.execute(sa.select(deps).order_by(deps.c.id)):
        candidates = bind.execute(
            sa.select(offers.c.id, offers.c.preferred_providers).where(
                (offers.c.model_name == dep.served_model_name) & offers.c.owner_org_id.is_(None)
            )
        )
        for offer_id, providers in candidates:
            if isinstance(providers, str):
                providers = json.loads(providers)
            if offer_id in claimed or list(providers or []) != ["vllm"]:
                continue
            bind.execute(
                offers.update()
                .where(offers.c.id == offer_id)
                .values(owner_org_id=dep.org_id, deployment_public_id=dep.public_id)
            )
            claimed.add(offer_id)


def upgrade() -> None:
    """Add the scope columns, backfill ownership, and swap in per-org uniqueness."""
    old_name = _old_unique_name()
    drop_old = _has_old_unique()
    # SQLite cannot drop a constraint in place and its inspector cannot see an inline
    # ``model_name ... UNIQUE``; a full table recreate (built from reflection) drops it.
    recreate: Literal["auto", "always"] = (
        "always" if op.get_bind().dialect.name == "sqlite" else "auto"
    )
    with op.batch_alter_table(
        "model_configs", naming_convention=_CONVENTION, recreate=recreate
    ) as batch:
        batch.add_column(sa.Column("owner_org_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("deployment_public_id", sa.Uuid(), nullable=True))
        batch.create_index(_INDEX, ["deployment_public_id"])
        if drop_old:
            batch.drop_constraint(old_name or _OLD_UQ, type_="unique")
    _backfill()
    where = sa.text("owner_org_id IS NULL")
    op.create_index(
        _GLOBAL_UQ, "model_configs", ["model_name"], unique=True,
        postgresql_where=where, sqlite_where=where,
    )  # fmt: skip
    org_where = sa.text("owner_org_id IS NOT NULL")
    op.create_index(
        _ORG_UQ, "model_configs", ["owner_org_id", "model_name"], unique=True,
        postgresql_where=org_where, sqlite_where=org_where,
    )  # fmt: skip


def downgrade() -> None:
    """Drop org-owned offers (the reconciler republishes them), then restore the old shape."""
    op.drop_index(_ORG_UQ, table_name="model_configs")
    op.drop_index(_GLOBAL_UQ, table_name="model_configs")
    op.execute(sa.text("DELETE FROM model_configs WHERE owner_org_id IS NOT NULL"))
    with op.batch_alter_table("model_configs") as batch:
        batch.drop_index(_INDEX)
        batch.drop_column("deployment_public_id")
        batch.drop_column("owner_org_id")
        batch.create_unique_constraint(_OLD_UQ, ["model_name"])
