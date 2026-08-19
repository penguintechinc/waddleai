"""Agent Hooks (§18): hook_rules, hook_denylist_entries, hook_configs, hook_telemetry_events.

Adds the server-side tables backing §18's developer-agent hook evaluation:

- ``hook_rules``: admin-authored declarative custom hooks (§18.3) -- a
  matcher (ecosystem/event/tool_name_pattern/match_pattern) plus a
  decision (allow|deny|ask), never shippable code. ``scope_type``/
  ``scope_ref`` follow the same global->org pattern migration 011 uses for
  ``security_policies``, but only two levels deep (no model/tool scope --
  the matcher fields themselves carry that granularity here).
- ``hook_denylist_entries``: admin-*added* Tier-1 canonical-denylist
  entries (§18.1/§18.4). The builtin seed list is a hardcoded constant in
  ``shared.security.hooks_denylist``, not a DB row -- this table can only
  ever add restrictions, never remove or weaken the builtin set, since
  there is no row representing a builtin pattern to delete.
- ``hook_configs``: per-scope opt-in config (§18.2/§18.5) -- Tier-2 remote
  policy evaluation enablement/timeout/fail-mode, and telemetry raw-payload
  capture opt-in. Same global->org nullable-column-means-inherit pattern as
  ``security_policies``.
- ``hook_telemetry_events``: fire-and-forget post_tool_use/session_start/
  notification events (§18.2). ``tool_input_hash`` is always populated;
  ``tool_input_raw`` is populated only when the resolved ``hook_configs``
  scope has ``capture_raw_payloads=True`` -- see the privacy constraint in
  the module docstring of ``shared.security.hooks_config``.

Revision ID: 015_hooks
Revises: 014_integrations
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_hooks"
# TODO(rebase): re-parent onto 015_local_vector_store at merge. That revision
# lands on feature/local-only-profile; naming it here would leave a dangling
# parent, and alembic resolves the ENTIRE graph on any command -- which breaks
# every migration test in the repo, not just this one.
down_revision: str | None = "014_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create hook_rules, hook_denylist_entries, hook_configs, hook_telemetry_events."""
    op.create_table(
        "hook_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # 'global' | 'org'
        sa.Column("scope_type", sa.String(10), nullable=False),
        # NULL for global; org id (as text) for org
        sa.Column("scope_ref", sa.String(255), nullable=True),
        # NULL = matches any ecosystem/event
        sa.Column("ecosystem", sa.String(20), nullable=True),
        sa.Column("event", sa.String(20), nullable=True),
        # Glob patterns (see shared.security.hooks_denylist.glob_search);
        # NULL = matches anything
        sa.Column("tool_name_pattern", sa.String(255), nullable=True),
        sa.Column("match_pattern", sa.Text(), nullable=True),
        # 'allow' | 'deny' | 'ask'
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        # Lower = higher priority; tie-break only among equal-severity
        # matches (see hooks_rules.combine_hook_rule_matches) -- never
        # changes which decision wins, only which rule is credited.
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_hookrules_scope", "hook_rules", ["scope_type", "scope_ref", "enabled"]
    )

    op.create_table(
        "hook_denylist_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(10), nullable=False),  # 'global' | 'org'
        sa.Column("scope_ref", sa.String(255), nullable=True),
        sa.Column("pattern", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_hookdenylist_scope", "hook_denylist_entries", ["scope_type", "scope_ref", "enabled"]
    )

    op.create_table(
        "hook_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(10), nullable=False),  # 'global' | 'org'
        sa.Column("scope_ref", sa.String(255), nullable=True),
        # NULL = inherit from the next-more-general scope (same convention
        # as security_policies, migration 011).
        sa.Column("remote_eval_enabled", sa.Boolean(), nullable=True),
        sa.Column("remote_eval_timeout_ms", sa.Integer(), nullable=True),
        sa.Column("remote_eval_fail_mode", sa.String(10), nullable=True),  # 'open' | 'closed'
        sa.Column("capture_raw_payloads", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_ref", name="uq_hookconfigs_scope"),
    )

    op.create_table(
        "hook_telemetry_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("ecosystem", sa.String(20), nullable=False),
        sa.Column("event", sa.String(20), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        # Always populated: sha256 hex of the JSON-serialized tool_input.
        sa.Column("tool_input_hash", sa.String(64), nullable=True),
        # Only populated when the resolved hook_configs scope has
        # capture_raw_payloads=True -- NULL by default (§18.5 privacy
        # constraint: never persist raw command lines/paths without an
        # explicit per-org opt-in).
        sa.Column("tool_input_raw", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_hooktelemetry_org_received",
        "hook_telemetry_events",
        ["organization_id", "received_at"],
    )


def downgrade() -> None:
    """Drop hook_telemetry_events, hook_configs, hook_denylist_entries, hook_rules."""
    op.drop_index("idx_hooktelemetry_org_received", table_name="hook_telemetry_events")
    op.drop_table("hook_telemetry_events")

    op.drop_table("hook_configs")

    op.drop_index("idx_hookdenylist_scope", table_name="hook_denylist_entries")
    op.drop_table("hook_denylist_entries")

    op.drop_index("idx_hookrules_scope", table_name="hook_rules")
    op.drop_table("hook_rules")
