"""Add mcp_endpoints and mcp_user_links (external MCP gateway registry).

Adds the registry of admin-registered external MCP endpoints (§11.4) and
per-user OAuth tokens for `identity_mode='per_user'` endpoints (§11.4/
§11.5). `mcp_user_links.user_uuid` references the caller by opaque id only
(no PII, per the identity-table rule); `access_token_enc`/
`refresh_token_enc` are Fernet-encrypted at rest via
`shared.security.credential_encryption`, reusing the same encryption
helper `provider_credentials.api_key` already uses -- never stored
plaintext, never returned in an API response, never logged.

Revision ID: 014_integrations
Revises: 013_fleet
Create Date: 2026-08-14

TODO(rebase): down_revision is a placeholder. The intended chain is
006 -> 007 -> 008 -> 009a -> 009b -> 010 -> 011 -> 012 -> 013 -> 014;
migrations 007-013 are being authored on parallel branches and do not
exist in this worktree yet (§13.1). Repoint down_revision to the real
013_fleet revision id at merge/reconciliation time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014_integrations"
down_revision: str | None = "013_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create mcp_endpoints and mcp_user_links."""
    op.create_table(
        "mcp_endpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("transport", sa.String(20), nullable=False),  # 'streamable_http', 'stdio'
        # 'none', 'header', 'oauth2_client_credentials', 'oauth2_auth_code'
        sa.Column("auth_type", sa.String(40), nullable=False),
        sa.Column("auth_config", sa.JSON(), nullable=True),
        # 'shared' (one org-wide credential) or 'per_user'
        sa.Column("identity_mode", sa.String(20), nullable=False, server_default="shared"),
        sa.Column("namespace", sa.String(100), nullable=False),
        # Opaque pointer into the credential store for shared-mode static
        # secrets (provider-credential pattern) -- the secret itself never
        # lives in this table.
        sa.Column("credentials_ref", sa.String(255), nullable=True),
        # 'active', 'disabled', 'error'
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Namespace unique per org: two endpoints in the same org can never
        # collide on `elder.*`-style tool namespacing (§11.4) -- caught at
        # registration, not at aggregation time. Declared inline (not via a
        # separate op.create_unique_constraint ALTER) for SQLite
        # compatibility -- SQLite has no ALTER-add-constraint support.
        sa.UniqueConstraint("org_id", "namespace", name="uq_mcp_endpoints_org_namespace"),
    )
    op.create_index("ix_mcp_endpoints_org_id", "mcp_endpoints", ["org_id"])

    op.create_table(
        "mcp_user_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        # Opaque caller id, no PII -- see module docstring.
        sa.Column("user_uuid", sa.String(36), nullable=False),
        # Fernet-encrypted ("enc:" prefix) via shared.security.credential_encryption.
        sa.Column("access_token_enc", sa.String(2048), nullable=True),
        sa.Column("refresh_token_enc", sa.String(2048), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        # 'linked', 'expired', 'revoked'
        sa.Column("status", sa.String(20), nullable=False, server_default="linked"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["endpoint_id"], ["mcp_endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint_id", "user_uuid", name="uq_mcp_user_links_endpoint_user"),
    )
    op.create_index("ix_mcp_user_links_endpoint_id", "mcp_user_links", ["endpoint_id"])


def downgrade() -> None:
    """Drop mcp_user_links and mcp_endpoints."""
    op.drop_table("mcp_user_links")
    op.drop_table("mcp_endpoints")
