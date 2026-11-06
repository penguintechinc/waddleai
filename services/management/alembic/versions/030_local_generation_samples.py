"""Per-request generation samples for locally hosted models (observed throughput, spec §4.7).

The AUTO price is recomputed daily from the p50 output tokens/s a deployment actually delivers.
``token_usage`` is a per-key/day rollup with no timing, so each timed local request also
records one small sample row (kind, deployment, output tokens, generation milliseconds); the
reconciler reads the p50 over a rolling window and prunes old samples.

Revision ID: 030_local_generation_samples
Revises: 029_model_config_offer_scope
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030_local_generation_samples"
down_revision: str | None = "029_model_config_offer_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "local_generation_samples"
_INDEX = "ix_local_generation_samples_dep_created"


def upgrade() -> None:
    """Create the samples table and its (deployment, time) index."""
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("local_deployment_kind", sa.Integer(), nullable=False),
        sa.Column("local_deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("generation_ms", sa.Integer(), nullable=False),
        # 1 = engine-reported decode time, 2 = proxy-measured (consumer stalls excluded)
        sa.Column("timing_source", sa.SmallInteger(), nullable=False, server_default="2"),
        # the user whose request produced the sample: caps any one principal's influence
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("output_tokens > 0", name="ck_local_generation_samples_tokens"),
        sa.CheckConstraint("generation_ms > 0", name="ck_local_generation_samples_ms"),
        sa.CheckConstraint("timing_source IN (1, 2)", name="ck_local_generation_samples_source"),
    )
    op.create_index(_INDEX, _TABLE, ["local_deployment_kind", "local_deployment_id", "created_at"])


def downgrade() -> None:
    """Drop the samples table (observed throughput falls back to the estimate)."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
