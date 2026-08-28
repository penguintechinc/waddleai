"""Model Access Policies -- per-tenant deny lists (design spec §3.3, 2026-08-28).

Adds ``model_access_policies``: the management-side surface letting an org
(or a narrower user/key scope inside it) block a client-supplied model
pattern from being routed to, e.g. "block claude-opus-5*, keep opus-4.8
workers unaffected". Every row is a block rule -- ``action`` governs what
happens when it matches a request (``reject`` outright, the default, or
``reroute`` to ``fallback_model``). Enforcement (wiring this table into
``RoutingEngine``/`` /v1/models`` filtering) is a separate, proxy-side
follow-up; this migration only lands the storage.

``scope_type``/``scope_ref`` follows the same ``global``/``org``/``key``
polymorphic-scope convention as ``cache_configs`` (migration 009a) and
``hook_rules`` (migration 016), extended with a ``user`` level between
``org`` and ``key`` to match the resolver's four-level precedence
(``shared/security/model_access.py``). ``model_pattern`` uses a single
field with glob semantics (``fnmatch``) rather than a separate
``match_type`` column -- an exact id and a no-wildcard glob pattern match
identically, so a second column would only be a state-sync hazard (e.g.
``match_type='exact'`` next to a pattern that contains ``*``).

Revision ID: 018_model_access_policies
Revises: 017_ollama_deployment_namespace
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018_model_access_policies"
down_revision: str | None = "017_ollama_deployment_namespace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``model_access_policies``."""
    op.create_table(
        "model_access_policies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # 'global' | 'org' | 'user' | 'key'
        sa.Column("scope_type", sa.String(10), nullable=False),
        # NULL only for scope_type='global'; org_id/user_id/api_key_id as text otherwise.
        sa.Column("scope_ref", sa.String(255), nullable=True),
        # Exact model id ("claude-opus-5-20260501") or glob ("claude-opus-5*").
        sa.Column("model_pattern", sa.String(255), nullable=False),
        # 'reject' (default -- block outright) | 'reroute' (serve fallback_model instead).
        sa.Column("action", sa.String(10), nullable=False, server_default="reject"),
        # Required when action='reroute'; ignored/NULL for 'reject'.
        sa.Column("fallback_model", sa.String(255), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_map_scope",
        "model_access_policies",
        ["scope_type", "scope_ref", "enabled"],
    )


def downgrade() -> None:
    """Drop ``model_access_policies``."""
    op.drop_index("idx_map_scope", table_name="model_access_policies")
    op.drop_table("model_access_policies")
