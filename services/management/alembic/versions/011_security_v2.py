"""Security Layer v2: scoped policies, bypass grants, extended audit log.

Adds `security_policies` (§8.1 global->org->model->tool resolution chain) and
`security_bypass_grants` (§8.6 authorized bypass); extends
`content_filter_audit_log` with `policy_id`, `intent_categories`, `degraded`,
`bypass_grant_id`, `redaction_counts` (§8.9). Data-migrates existing per-org
`content_filter_config` rows into scoped `security_policies` rows, then drops
`content_filter_config`.

Column nullability encodes the resolution chain (§8.1's "most-specific field
wins, per-field merge"): every configurable column (tier{1..4}_enabled,
tier4_model, intent_classifier_enabled, intent_categories, block_action,
fail_mode, on_unclassifiable, auditor_timeout_ms, latency_budget_ms,
sample_rate, upstream_filters) is nullable. NULL means "not set at this
scope -- inherit from the next-more-general scope"; a non-NULL value
overrides. Only the seeded `global` row is fully populated (it is the
resolution floor -- nothing to inherit from), matching `_GLOBAL_DEFAULTS`
below. Migrated `org` rows are seeded with every configurable column NULL:
`content_filter_config`'s actual keys (`auditor_system_prompt`,
`disabled_builtins`, `disabled_ner_entities`) have no 1:1 mapping onto these
structured columns, and v1 never disabled a tier wholesale per-org -- so
"inherit everything from global" is the most faithful representation of
pre-migration per-org behavior, not a lossy default. `content_filter_rules`
(custom tier-2 rules) is untouched -- rules stay keyed by organization_id
and are matched against the resolved policy's `tier2_enabled` flag at
runtime, not referenced by FK from this table.

# TODO(rebase): down_revision is pinned to "010_routing_engine" per the
# intended final chain (006 -> 007 -> 008 -> 009a -> 009b -> 010 -> 011 ->
# 012 -> 013 -> 014). Migrations 007-010 are being authored on sibling
# branches and do not exist in this worktree; the human maintainer
# reconciles the actual down_revision at merge time.

Revision ID: 011_security_v2
Revises: 010_routing_engine
Create Date: 2026-07-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_security_v2"
down_revision: str | None = "010_routing_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fully-populated defaults for the seeded 'global' row -- the resolution
# floor, so every configurable field must be concrete here. Mirrors the
# §8.1/§8.2 resolved defaults: all four tiers on (matching v1's unconditional
# tier execution), no intent classifier (new feature, opt-in), fail_mode
# "degrade" (§8.2 resolved default), on_unclassifiable "reject" (§8.3a safe
# default), 5s auditor timeout (§8.2, down from v1's hardcoded 10s),
# sample_rate 100 (audit everything until an admin dials sampling down).
_GLOBAL_DEFAULTS = {
    "tier1_enabled": True,
    "tier2_enabled": True,
    "tier3_enabled": True,
    "tier4_enabled": True,
    "tier4_model": None,
    "intent_classifier_enabled": False,
    "intent_categories": None,
    "direction": "both",
    "block_action": "redact",
    "fail_mode": "degrade",
    "on_unclassifiable": "reject",
    "auditor_timeout_ms": 5000,
    "latency_budget_ms": None,
    "sample_rate": 100,
    "upstream_filters": None,
}

# Migrated 'org' rows: every configurable field left NULL (inherit
# everything from 'global' -- see module docstring for why this is the
# faithful migration, not a lossy shortcut). Only the structural
# scope/direction columns are set.
_ORG_ROW_DEFAULTS = {k: None for k in _GLOBAL_DEFAULTS}
_ORG_ROW_DEFAULTS["direction"] = "both"


def upgrade() -> None:
    """Create security_policies/security_bypass_grants, extend the audit log, fold config."""
    op.create_table(
        "security_policies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # 'global' | 'org' | 'model' | 'tool'
        sa.Column("scope_type", sa.String(10), nullable=False),
        # NULL for global; org id (as text) for org; model name for model;
        # tools[].function.name / namespaced MCP name (e.g. "elder.search") for tool
        sa.Column("scope_ref", sa.String(255), nullable=True),
        # All configurable columns below are nullable: NULL = "not set at
        # this scope, inherit from the next-more-general scope" (see module
        # docstring). Only the seeded 'global' row is fully populated.
        sa.Column("tier1_enabled", sa.Boolean, nullable=True),
        sa.Column("tier2_enabled", sa.Boolean, nullable=True),
        sa.Column("tier3_enabled", sa.Boolean, nullable=True),
        sa.Column("tier4_enabled", sa.Boolean, nullable=True),
        sa.Column("tier4_model", sa.String(100), nullable=True),
        sa.Column("intent_classifier_enabled", sa.Boolean, nullable=True),
        sa.Column("intent_categories", sa.JSON, nullable=True),
        # 'input' | 'output' | 'both' -- structural, not inheritable (a row is
        # scoped to one direction), so this one stays NOT NULL.
        sa.Column("direction", sa.String(10), nullable=False, server_default="both"),
        # 'block' | 'redact' | 'flag'
        sa.Column("block_action", sa.String(10), nullable=True),
        # 'open' | 'closed' | 'degrade'
        sa.Column("fail_mode", sa.String(10), nullable=True),
        # 'reject' | 'degrade' -- §8.3a per-modality-classifier-registry gap handling
        sa.Column("on_unclassifiable", sa.String(10), nullable=True),
        sa.Column("auditor_timeout_ms", sa.Integer, nullable=True),
        sa.Column("latency_budget_ms", sa.Integer, nullable=True),
        sa.Column("sample_rate", sa.Integer, nullable=True),
        # §8.7 upstream (pre-provider) filter config: category toggles, preset,
        # mode (redact|pseudonymize), applies_to (commercial|all)
        sa.Column("upstream_filters", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_secpol_scope",
        "security_policies",
        ["scope_type", "scope_ref", "direction"],
        unique=True,
    )

    op.create_table(
        "security_bypass_grants",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # 'user' | 'vkey'
        sa.Column("subject_type", sa.String(10), nullable=False),
        sa.Column("subject_ref", sa.String(255), nullable=False),
        # 'shadow' (default, run+log, don't enforce) | 'skip' (tiers don't run)
        sa.Column("mode", sa.String(10), nullable=False, server_default="shadow"),
        # List of policy-scope names this grant narrows to (e.g. ["intent_classifier"]);
        # NULL/empty = bypasses everything the grant's mode implies
        sa.Column("scope_narrow", sa.JSON, nullable=True),
        sa.Column("include_upstream", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "granted_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_secbypass_subject", "security_bypass_grants", ["subject_type", "subject_ref"]
    )
    op.create_index("idx_secbypass_expires", "security_bypass_grants", ["expires_at"])

    # Extend content_filter_audit_log (§8.9). batch_alter_table: SQLite has no
    # ALTER-of-constraint support, so FK-bearing ADD COLUMN needs the
    # copy-and-move batch strategy (matches migration 006's precedent).
    with op.batch_alter_table("content_filter_audit_log") as batch_op:
        policy_fk = sa.ForeignKey(
            "security_policies.id", ondelete="SET NULL", name="fk_cfal_policy_id"
        )
        batch_op.add_column(sa.Column("policy_id", sa.Integer, policy_fk, nullable=True))
        batch_op.add_column(sa.Column("intent_categories", sa.JSON, nullable=True))
        degraded_col = sa.Column("degraded", sa.Boolean, nullable=False, server_default="false")
        batch_op.add_column(degraded_col)
        bypass_fk = sa.ForeignKey(
            "security_bypass_grants.id", ondelete="SET NULL", name="fk_cfal_bypass_grant"
        )
        batch_op.add_column(sa.Column("bypass_grant_id", sa.Integer, bypass_fk, nullable=True))
        batch_op.add_column(sa.Column("redaction_counts", sa.JSON, nullable=True))

    _migrate_content_filter_config()

    op.drop_index("idx_cfc_key_org", table_name="content_filter_config")
    op.drop_table("content_filter_config")


def _migrate_content_filter_config() -> None:
    """Fold existing content_filter_config org rows into scoped security_policies.

    Inserts one fully-populated 'global' row (always -- even if
    content_filter_config was empty, per §8.1's "seed one global row"), and
    one 'org' row (everything NULL / inherited) per distinct organization_id
    found in content_filter_config. See module docstring for why org rows
    are seeded empty rather than attempting a field-for-field KV mapping.
    """
    conn = op.get_bind()

    select_org_ids = sa.text(
        "SELECT DISTINCT organization_id FROM content_filter_config "
        "WHERE organization_id IS NOT NULL"
    )
    org_ids = [row[0] for row in conn.execute(select_org_ids).fetchall()]

    # sa.table()/insert() (Core, not raw string-built SQL) -- columns come
    # from a hardcoded dict this migration owns, never external input, but
    # building the column list is expressed through the query builder
    # rather than string concatenation.
    security_policies = sa.table(
        "security_policies",
        sa.column("scope_type"),
        sa.column("scope_ref"),
        *(sa.column(name) for name in _GLOBAL_DEFAULTS),
    )

    def _row(scope_type: str, scope_ref: str | None, defaults: dict) -> dict:
        params = {"scope_type": scope_type, "scope_ref": scope_ref}
        params.update(defaults)
        return params

    conn.execute(security_policies.insert(), _row("global", None, _GLOBAL_DEFAULTS))
    for org_id in org_ids:
        conn.execute(security_policies.insert(), _row("org", str(org_id), _ORG_ROW_DEFAULTS))


def downgrade() -> None:
    """Restore the pre-011 content_filter_config shape; drop the v2 tables/columns.

    Migrated security_policies rows do not round-trip back into KV pairs
    (there is no lossless inverse of the fold -- see upgrade() docstring);
    downgrade restores the empty table shape only.
    """
    org_fk = sa.ForeignKey("organizations.id", ondelete="CASCADE")
    op.create_table(
        "content_filter_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("organization_id", sa.Integer, org_fk, nullable=True),
        sa.Column(
            "created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_cfc_key_org", "content_filter_config", ["key", "organization_id"], unique=True
    )

    with op.batch_alter_table("content_filter_audit_log") as batch_op:
        batch_op.drop_column("redaction_counts")
        batch_op.drop_column("bypass_grant_id")
        batch_op.drop_column("degraded")
        batch_op.drop_column("intent_categories")
        batch_op.drop_column("policy_id")

    op.drop_index("idx_secbypass_expires", table_name="security_bypass_grants")
    op.drop_index("idx_secbypass_subject", table_name="security_bypass_grants")
    op.drop_table("security_bypass_grants")

    op.drop_index("idx_secpol_scope", table_name="security_policies")
    op.drop_table("security_policies")
