"""Add content_filter_rules and content_filter_audit_log tables.

Revision ID: 005_add_content_filter_tables
Revises: 004_drop_provider_api_key
Create Date: 2026-06-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_add_content_filter_tables"
down_revision: Union[str, None] = "004_drop_provider_api_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create content_filter_rules table
    op.create_table(
        "content_filter_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("rule_type", sa.String(20), nullable=False),  # 'builtin_pii', 'custom_string', 'custom_regex'
        sa.Column("target", sa.String(10), nullable=False, server_default="both"),  # 'input', 'output', 'both'
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("action", sa.String(10), nullable=False, server_default="log"),  # 'block', 'redact', 'log'
        sa.Column("redact_with", sa.String(100), nullable=True, server_default="[REDACTED]"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    # Create indexes for content_filter_rules
    op.create_index("idx_cfr_org_enabled", "content_filter_rules", ["organization_id", "enabled"])
    op.create_index("idx_cfr_target", "content_filter_rules", ["target"])

    # Create content_filter_audit_log table
    op.create_table(
        "content_filter_audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("phase", sa.String(10), nullable=False),  # 'input', 'output'
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("api_key_id", sa.Integer, sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("action_taken", sa.String(10), nullable=False),  # 'allow', 'block', 'redact', 'log'
        sa.Column("violations_json", sa.JSON, nullable=True),
        sa.Column("text_sample", sa.Text, nullable=True),  # First 200 chars for audit
        sa.Column("auditor_used", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("auditor_decision", sa.String(10), nullable=True),  # 'block', 'allow', NULL if not invoked
        sa.Column("request_id", sa.String(64), nullable=True),  # For correlation with proxy logs
    )

    # Create indexes for content_filter_audit_log
    op.create_index("idx_cfal_timestamp", "content_filter_audit_log", ["timestamp"], postgresql_using="btree", postgresql_ops={"timestamp": "DESC"})
    op.create_index("idx_cfal_user", "content_filter_audit_log", ["user_id", "timestamp"], postgresql_using="btree", postgresql_ops={"timestamp": "DESC"})
    op.create_index("idx_cfal_org", "content_filter_audit_log", ["organization_id", "timestamp"], postgresql_using="btree", postgresql_ops={"timestamp": "DESC"})
    op.create_index("idx_cfal_action", "content_filter_audit_log", ["action_taken"])

    # Create content_filter_config table (key-value store for filter configuration)
    op.create_table(
        "content_filter_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    # Unique constraint: one value per key per org (NULL org = global)
    op.create_index("idx_cfc_key_org", "content_filter_config", ["key", "organization_id"], unique=True)


def downgrade() -> None:
    # Drop indexes
    op.drop_index("idx_cfc_key_org", table_name="content_filter_config")
    op.drop_index("idx_cfal_action", table_name="content_filter_audit_log")
    op.drop_index("idx_cfal_org", table_name="content_filter_audit_log")
    op.drop_index("idx_cfal_user", table_name="content_filter_audit_log")
    op.drop_index("idx_cfal_timestamp", table_name="content_filter_audit_log")
    op.drop_index("idx_cfr_target", table_name="content_filter_rules")
    op.drop_index("idx_cfr_org_enabled", table_name="content_filter_rules")

    # Drop tables
    op.drop_table("content_filter_config")
    op.drop_table("content_filter_audit_log")
    op.drop_table("content_filter_rules")
