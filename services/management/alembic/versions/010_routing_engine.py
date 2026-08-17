"""Unified Smart Routing engine tables (spec §7, §13.1).

Adds ``model_configs`` (seeded from the hardcoded request_router dict),
``model_aliases``, ``routing_rules_v2``, ``routing_policies``,
``routing_decision_traces`` (§7.4 first-class trace corpus); evolves
``routing_matrix`` -> ``model_assignments`` (renamed, plus ``escalation_model``,
``fallback_models``, ``scope``, ``scope_ref``; complexity/region loosened to
nullable since new rows are keyed by tool_type+scope, not the old
tool_type+complexity+region composite). Seeds WaddleAI's internal-function
assignment rows (security-audit, routing-classifier, embeddings, docs-fetch,
summarize) per the §2.3 dual-default pattern.

# TODO(rebase): down_revision is pinned to "006_add_memory_scope" -- the last
# revision that exists in this worktree. The intended final chain per spec
# §13.1 is 006 -> 007 -> 008 -> 009a -> 009b -> 010 (proxy-migration,
# response-cache and proxy-memory branches land 007/008/009a/009b in
# parallel and do not exist here yet). Re-point down_revision at
# "009b_proxy_memory" when reconciling this branch into release/v0.2.X.
# Internal-function rows deliberately store model_name as a plain string
# rather than an FK into model_registry (migration 008, not present yet) --
# add that FK in a follow-up once 008 lands, matching the existing
# model_assignments.model_name / model_configs.model_name convention.

Revision ID: 010_routing_engine
Revises: 006_add_memory_scope
Create Date: 2026-08-14
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "010_routing_engine"
down_revision: str | None = "009b_proxy_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Extracted from shared.utils.request_router.LLMRequestRouter._load_model_configs
# (the hardcoded dict this migration retires per §7.6).
_LEGACY_MODEL_CONFIGS = [
    {
        "model_name": "gpt-4",
        "preferred_providers": ["openai"],
        "cost_per_token": {"openai": 0.00003},
        "max_tokens": 8192,
        "context_length": 8192,
        "capabilities": ["chat", "completion", "reasoning"],
    },
    {
        "model_name": "gpt-3.5-turbo",
        "preferred_providers": ["openai"],
        "cost_per_token": {"openai": 0.0000015},
        "max_tokens": 4096,
        "context_length": 4096,
        "capabilities": ["chat", "completion"],
    },
    {
        "model_name": "claude-3-opus-20240229",
        "preferred_providers": ["anthropic"],
        "cost_per_token": {"anthropic": 0.000015},
        "max_tokens": 200000,
        "context_length": 200000,
        "capabilities": ["chat", "reasoning", "analysis"],
    },
    {
        "model_name": "claude-3-sonnet-20240229",
        "preferred_providers": ["anthropic"],
        "cost_per_token": {"anthropic": 0.000003},
        "max_tokens": 200000,
        "context_length": 200000,
        "capabilities": ["chat", "reasoning"],
    },
    {
        "model_name": "llama3",
        "preferred_providers": ["ollama"],
        "cost_per_token": {"ollama": 0.0},
        "max_tokens": 4096,
        "context_length": 4096,
        "capabilities": ["chat", "completion"],
    },
]

# WaddleAI's own internal functions (§2.3 dual-default pattern, §8.3a for the
# security auditor). security-audit's Apache-licensed alternative is not
# seeded as a second row here -- the dual-default *alternative* concept is a
# deployment-config choice (§2.3), not a second assignments row; the primary
# pick is Apache-2.0-safe already.
_INTERNAL_FUNCTION_ASSIGNMENTS = [
    {"tool_type": "security-audit", "model_name": "shieldgemma:2b"},
    {"tool_type": "routing-classifier", "model_name": "gemma4:e2b"},
    {"tool_type": "embeddings", "model_name": "nomic-embed-text"},
    {"tool_type": "docs-fetch", "model_name": "gemma4:e2b"},
    {"tool_type": "summarize", "model_name": "gemma4:e2b"},
]


def upgrade() -> None:
    """Evolve routing_matrix into model_assignments and add the routing-engine tables."""
    # --- routing_matrix -> model_assignments -----------------------------
    op.rename_table("routing_matrix", "model_assignments")

    with op.batch_alter_table("model_assignments") as batch_op:
        batch_op.add_column(sa.Column("escalation_model", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("fallback_models", sa.JSON, nullable=True))
        batch_op.add_column(
            sa.Column("scope", sa.String(10), nullable=False, server_default="global")
        )
        batch_op.add_column(sa.Column("scope_ref", sa.Integer, nullable=True))
        batch_op.alter_column("complexity", existing_type=sa.String(10), nullable=True)
        batch_op.alter_column("region", existing_type=sa.String(5), nullable=True)
        batch_op.drop_constraint("uq_routing_matrix_lookup", type_="unique")
        batch_op.create_unique_constraint(
            "uq_model_assignments_lookup", ["tool_type", "scope", "scope_ref"]
        )

    # --- new tables --------------------------------------------------------
    op.create_table(
        "model_configs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("model_name", sa.String(255), nullable=False, unique=True),
        sa.Column("preferred_providers", sa.JSON, nullable=False),
        sa.Column("cost_per_token", sa.JSON, nullable=False),
        sa.Column("max_tokens", sa.Integer, nullable=False),
        sa.Column("context_length", sa.Integer, nullable=False),
        sa.Column("capabilities", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "model_aliases",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("source_model", sa.String(255), nullable=False),
        sa.Column("target_model", sa.String(255), nullable=False),
        sa.Column("target_provider", sa.String(100), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("organization_id", "source_model", name="uq_model_aliases_org_source"),
    )
    op.create_index("idx_model_aliases_org", "model_aliases", ["organization_id"])

    op.create_table(
        "routing_rules_v2",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("match", sa.JSON, nullable=False),
        sa.Column("action", sa.JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("idx_routing_rules_v2_org", "routing_rules_v2", ["organization_id"])
    op.create_index("idx_routing_rules_v2_priority", "routing_rules_v2", ["priority"])

    op.create_table(
        "routing_policies",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("mode", sa.String(20), nullable=False, server_default="local_first"),
        sa.Column("escalation_threshold", sa.Integer, nullable=False, server_default="3"),
        sa.Column("escalation_target", sa.String(255), nullable=True),
        sa.Column("classifier_prompt", sa.Text, nullable=True),
        sa.Column("de_escalation", sa.String(20), nullable=False, server_default="idle_reset"),
        sa.Column("idle_reset_minutes", sa.Integer, nullable=False, server_default="10"),
        sa.Column(
            "sensitivity_routing", sa.String(20), nullable=False, server_default="local_only"
        ),
        sa.Column("budget_pressure_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("provider_failover", sa.String(20), nullable=False, server_default="off"),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "routing_decision_traces",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime, nullable=True),
        sa.Column("requirements", sa.JSON, nullable=True),
        sa.Column("tool_type", sa.String(50), nullable=True),
        sa.Column("tool_type_source", sa.String(20), nullable=True),
        sa.Column("rules_fired", sa.JSON, nullable=True),
        sa.Column("classifier_output", sa.JSON, nullable=True),
        sa.Column("assignment_model", sa.String(255), nullable=True),
        sa.Column("capability_veto", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("veto_reason", sa.String(255), nullable=True),
        sa.Column("qualified_candidates", sa.JSON, nullable=True),
        sa.Column("pressure_signals", sa.JSON, nullable=True),
        sa.Column("final_model", sa.String(255), nullable=True),
        sa.Column("routed_from", sa.JSON, nullable=True),
        sa.Column("escalated", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("idx_rdt_request_id", "routing_decision_traces", ["request_id"])
    op.create_index(
        "idx_rdt_org_timestamp", "routing_decision_traces", ["organization_id", "timestamp"]
    )

    # --- data migration: seed model_configs from the hardcoded dict --------
    model_configs_table = sa.table(
        "model_configs",
        sa.column("model_name", sa.String),
        sa.column("preferred_providers", sa.JSON),
        sa.column("cost_per_token", sa.JSON),
        sa.column("max_tokens", sa.Integer),
        sa.column("context_length", sa.Integer),
        sa.column("capabilities", sa.JSON),
        sa.column("enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        model_configs_table,
        [
            {
                "model_name": cfg["model_name"],
                "preferred_providers": cfg["preferred_providers"],
                "cost_per_token": cfg["cost_per_token"],
                "max_tokens": cfg["max_tokens"],
                "context_length": cfg["context_length"],
                "capabilities": cfg["capabilities"],
                "enabled": True,
                "created_at": now,
            }
            for cfg in _LEGACY_MODEL_CONFIGS
        ],
    )

    # --- seed internal-function assignment rows (global scope) -------------
    model_assignments_table = sa.table(
        "model_assignments",
        sa.column("tool_type", sa.String),
        sa.column("model_name", sa.String),
        sa.column("scope", sa.String),
        sa.column("scope_ref", sa.Integer),
        sa.column("enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(
        model_assignments_table,
        [
            {
                "tool_type": row["tool_type"],
                "model_name": row["model_name"],
                "scope": "global",
                "scope_ref": None,
                "enabled": True,
                "created_at": now,
            }
            for row in _INTERNAL_FUNCTION_ASSIGNMENTS
        ],
    )


def downgrade() -> None:
    """Fold model_assignments back into routing_matrix and drop the routing-engine tables."""
    op.drop_index("idx_rdt_org_timestamp", table_name="routing_decision_traces")
    op.drop_index("idx_rdt_request_id", table_name="routing_decision_traces")
    op.drop_table("routing_decision_traces")

    op.drop_table("routing_policies")

    op.drop_index("idx_routing_rules_v2_priority", table_name="routing_rules_v2")
    op.drop_index("idx_routing_rules_v2_org", table_name="routing_rules_v2")
    op.drop_table("routing_rules_v2")

    op.drop_index("idx_model_aliases_org", table_name="model_aliases")
    op.drop_table("model_aliases")

    op.drop_table("model_configs")

    # Remove the seeded internal-function rows before tightening
    # complexity/region back to NOT NULL -- they were seeded with both NULL.
    op.execute(
        "DELETE FROM model_assignments WHERE tool_type IN "
        "('security-audit', 'routing-classifier', 'embeddings', 'docs-fetch', 'summarize') "
        "AND scope = 'global' AND scope_ref IS NULL"
    )

    with op.batch_alter_table("model_assignments") as batch_op:
        batch_op.drop_constraint("uq_model_assignments_lookup", type_="unique")
        batch_op.create_unique_constraint(
            "uq_routing_matrix_lookup", ["tool_type", "complexity", "region"]
        )
        batch_op.alter_column("region", existing_type=sa.String(5), nullable=False)
        batch_op.alter_column("complexity", existing_type=sa.String(10), nullable=False)
        batch_op.drop_column("scope_ref")
        batch_op.drop_column("scope")
        batch_op.drop_column("fallback_models")
        batch_op.drop_column("escalation_model")

    op.rename_table("model_assignments", "routing_matrix")
