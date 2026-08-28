"""Add ``namespace`` column to ``ollama_deployments``.

``ollama_manager.py``'s ``create_deployment``/``update_deployment`` build the
row from ``OllamaDeploymentConfig``, which already carries a ``namespace``
field (defaulting to ``"waddleai"``) consumed by ``generate_daemonset_manifest``/
``generate_pool_manifest`` via ``getattr(deployment, "namespace", "waddleai")``
-- but the insert/update never wrote it, so every deployment silently fell
back to the default regardless of what was requested. This adds the missing
column so it round-trips; existing rows default to ``"waddleai"``, matching
the fallback the manifest generators already used.

Revision ID: 017_ollama_deployment_namespace
Revises: 016_hooks
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "017_ollama_deployment_namespace"
down_revision: str | None = "016_hooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``namespace`` (default ``"waddleai"``) to ``ollama_deployments``."""
    with op.batch_alter_table("ollama_deployments") as batch_op:
        batch_op.add_column(
            sa.Column("namespace", sa.String(255), nullable=False, server_default="waddleai")
        )


def downgrade() -> None:
    """Drop the ``namespace`` column."""
    with op.batch_alter_table("ollama_deployments") as batch_op:
        batch_op.drop_column("namespace")
