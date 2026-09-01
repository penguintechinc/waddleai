"""Webhook secret for CodeRAG repo registration (§9.1 core-completion).

Adds ``code_repos.webhook_secret`` -- a Fernet-encrypted (``enc:`` prefix via
``shared.security.credential_encryption``) shared secret generated at
repo-registration time and returned to the caller exactly once. The push
webhook route (``services/management/app/api/v1/code_repos.py``) verifies
the inbound GitHub/Gitea ``X-Hub-Signature-256`` HMAC against the decrypted
value before trusting the payload -- same encrypt-before-store pattern
already used for ``provider_credentials.api_key``, never a raw plaintext
secret column.

Revision ID: 019_code_repos_webhook_secret
Revises: 018_model_access_policies
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019_code_repos_webhook_secret"
down_revision: str | None = "018_model_access_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``code_repos.webhook_secret`` (nullable, Fernet-encrypted at the app layer)."""
    op.add_column("code_repos", sa.Column("webhook_secret", sa.String(512), nullable=True))


def downgrade() -> None:
    """Drop ``code_repos.webhook_secret``."""
    with op.batch_alter_table("code_repos") as batch_op:
        batch_op.drop_column("webhook_secret")
