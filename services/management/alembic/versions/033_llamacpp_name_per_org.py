"""Scope ``llamacpp_deployments.name`` uniqueness to the owning org.

The global ``UNIQUE(name)`` let one org squat names and learn another org's names from the
duplicate-key error. Replace it with a unique index on ``(COALESCE(owner_org_id, 0), name)`` so
the same name is allowed across orgs (legacy unowned rows share the 0 bucket). Kubernetes object
names derive from ``public_id``, so cross-org duplicates never collide in the cluster.

Revision ID: 033_llamacpp_name_per_org
Revises: 032_llamacpp_rpc_head
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "033_llamacpp_name_per_org"
down_revision: str | None = "032_llamacpp_rpc_head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llamacpp_deployments"
_INDEX = "uq_llamacpp_owner_name"
_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _present() -> bool:
    """True when the table exists (fresh DBs built from the chain have it by 013)."""
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def _has_index() -> bool:
    """True when the per-org unique index already exists (re-run safe)."""
    inspector = sa.inspect(op.get_bind())
    return _INDEX in {i["name"] for i in inspector.get_indexes(_TABLE)}


def _global_name_uniques() -> list[str | None]:
    """Names (None when unnamed) of unique constraints covering ONLY the ``name`` column."""
    bind = op.get_bind()
    found: list[str | None] = [
        u["name"]
        for u in sa.inspect(bind).get_unique_constraints(_TABLE)
        if list(u["column_names"]) == ["name"]
    ]
    if not found and bind.dialect.name == "sqlite":
        # an inline column UNIQUE is an implicit autoindex SQLAlchemy does not reflect
        for row in bind.execute(sa.text(f"PRAGMA index_list('{_TABLE}')")).mappings():
            if row["unique"] and row["origin"] == "u":
                cols = [
                    r["name"]
                    for r in bind.execute(sa.text(f"PRAGMA index_info('{row['name']}')")).mappings()
                ]
                if cols == ["name"]:
                    found.append(None)
    return found


def upgrade() -> None:
    """Drop the global name unique constraint and add the per-org unique index."""
    if not _present():
        return
    for name in _global_name_uniques():
        if name is None:
            # unreflected inline UNIQUE (sqlite): recreating from the reflected table drops it
            with op.batch_alter_table(_TABLE, recreate="always"):
                pass
            continue
        with op.batch_alter_table(_TABLE, naming_convention=_CONVENTION) as batch:
            batch.drop_constraint(name, type_="unique")
    if not _has_index():
        op.execute(f"CREATE UNIQUE INDEX {_INDEX} ON {_TABLE} (COALESCE(owner_org_id, 0), name)")


def downgrade() -> None:
    """Restore the global unique name (fails loudly if two orgs now share a name)."""
    if not _present():
        return
    if _has_index():
        op.drop_index(_INDEX, table_name=_TABLE)
    if not _global_name_uniques():
        with op.batch_alter_table(_TABLE, naming_convention=_CONVENTION) as batch:
            batch.create_unique_constraint("uq_llamacpp_deployments_name", ["name"])
