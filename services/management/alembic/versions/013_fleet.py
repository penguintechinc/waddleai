"""Add fleet_backends registry + interface columns on deployment tables.

Introduces the ``fleet_backends`` table (§10.1/§13.1 of the platform spec):
one row per registered inference fleet backend (Ollama, llama.cpp, EXO,
Vertex AI, Bedrock), carrying its ``type``, ``management_scope``
(``register_and_route`` vs ``full_lifecycle``), backend-specific ``config``,
and an encrypted ``credentials_ref`` for cloud backends. Extends
``ollama_deployments`` and ``llamacpp_deployments`` with a nullable
``fleet_backend_id`` FK plus the columns the ``InferenceFleetBackend``
interface needs to describe a node (``node_uid``, ``pool_mode``) — existing
rows default to ``management_scope='full_lifecycle'`` so pre-fleet
deployments keep their current (WaddleAI-managed end to end) behavior.

# TODO(rebase): down_revision pinned to "012_knowledge" per the intended
# final chain 006 -> 007 -> 008 -> 009a -> 009b -> 010 -> 011 -> 012 -> 013
# -> 014. Migrations 007-012 are being authored on sibling branches and do
# not exist on this branch; the orchestrating agent reconciles the actual
# down_revision at merge time.

Revision ID: 013_fleet
Revises: 012_knowledge
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013_fleet"
down_revision: str | None = "012_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ensure_llamacpp_deployments() -> None:
    """Create ``llamacpp_deployments`` (pre-013 shape) when no earlier revision did.

    The table was added to the models on 2026-05-23 WITHOUT a migration, so
    databases built by ``create_all()`` already hold it (this is then a no-op),
    but a database walked through the chain from scratch (gh-215) does not --
    and the ALTERs below would die on ``UndefinedTable``. Columns here are the
    model's as of this revision, minus the four 013 adds right after.
    """
    if "llamacpp_deployments" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "llamacpp_deployments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("deployment_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), nullable=False),
        sa.Column("status_message", sa.Text, nullable=True),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("model_url", sa.String(512), nullable=True),
        sa.Column("model_filename", sa.String(255), nullable=True),
        sa.Column("n_ctx", sa.Integer, nullable=True),
        sa.Column("n_gpu_layers", sa.Integer, nullable=True),
        sa.Column("gpu_count", sa.Integer, nullable=True),
        sa.Column("endpoint_url", sa.String(512), nullable=True),
        sa.Column("k8s_namespace", sa.String(255), nullable=True),
        sa.Column("k8s_daemonset_name", sa.String(255), nullable=True),
        sa.Column("node_selector", sa.JSON, nullable=True),
        sa.Column("node_affinity", sa.JSON, nullable=True),
        sa.Column("model_cache_claim", sa.String(255), nullable=True),
        sa.Column("cpu_request", sa.String(50), nullable=True),
        sa.Column("cpu_limit", sa.String(50), nullable=True),
        sa.Column("memory_request", sa.String(50), nullable=True),
        sa.Column("memory_limit", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("modified_at", sa.DateTime, nullable=True),
    )


def upgrade() -> None:
    """Create ``fleet_backends`` and extend both deployment tables."""
    op.create_table(
        "fleet_backends",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "org_id",
            sa.Integer,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),  # ollama|llamacpp|exo|vertex_ai|bedrock
        sa.Column("mode", sa.String(50), nullable=True),
        sa.Column(
            "management_scope",
            sa.String(30),  # register_and_route|full_lifecycle
            nullable=False,
            server_default="full_lifecycle",
        ),
        sa.Column("config", sa.JSON, nullable=True),
        # Fernet-encrypted with enc: prefix via shared.security.credential_encryption
        sa.Column("credentials_ref", sa.String(512), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_fleet_backends_org", "fleet_backends", ["org_id"])
    op.create_index(
        "uq_fleet_backends_org_name",
        "fleet_backends",
        ["org_id", "name"],
        unique=True,
    )

    _ensure_llamacpp_deployments()

    for table in ("ollama_deployments", "llamacpp_deployments"):
        # batch_alter_table: SQLite cannot ALTER-add a column with an inline
        # FK constraint; batch mode does the copy-and-move needed on SQLite
        # and is a plain ALTER TABLE ADD COLUMN on Postgres.
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "fleet_backend_id",
                    sa.Integer,
                    sa.ForeignKey(
                        "fleet_backends.id",
                        ondelete="SET NULL",
                        name=f"fk_{table}_fleet_backend_id",
                    ),
                    nullable=True,
                )
            )
            batch_op.add_column(
                sa.Column(
                    "management_scope",
                    sa.String(30),  # register_and_route|full_lifecycle
                    nullable=False,
                    server_default="full_lifecycle",
                )
            )
            batch_op.add_column(sa.Column("node_uid", sa.String(255), nullable=True))
            batch_op.add_column(
                sa.Column("pool_mode", sa.Boolean, nullable=False, server_default=sa.false())
            )
        op.create_index(f"idx_{table}_fleet_backend", table, ["fleet_backend_id"])


def downgrade() -> None:
    """Drop the interface columns and the ``fleet_backends`` table."""
    for table in ("ollama_deployments", "llamacpp_deployments"):
        op.drop_index(f"idx_{table}_fleet_backend", table_name=table)
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("pool_mode")
            batch_op.drop_column("node_uid")
            batch_op.drop_column("management_scope")
            batch_op.drop_column("fleet_backend_id")

    op.drop_index("uq_fleet_backends_org_name", table_name="fleet_backends")
    op.drop_index("idx_fleet_backends_org", table_name="fleet_backends")
    op.drop_table("fleet_backends")
