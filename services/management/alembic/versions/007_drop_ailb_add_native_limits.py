"""Drop AILB tables, fold their usage history into token_usage, add native limits.

Removes the MarchProxy AILB coupling (§5/§13 platform spec): the
`marchproxy_ailb_sync` table is dropped outright (config-only, no history
worth keeping); `ailb_usage_events` and `ailb_usage_records` are first
**folded** into `token_usage` with `source='ailb_import'` for billing/
dashboard continuity (spec Q#1), then dropped. The fold is a plain
SELECT-then-INSERT over each source table -- an empty source table simply
yields zero rows and issues no INSERT, so it is safe/idempotent to run
against a dev database that never had AILB usage data.

Also: drops `virtual_keys.ailb_key_id`/`ailb_sync_status`; guard-adds
`virtual_keys.budget_monthly_tokens`/`budget_monthly_usd` (`rpm_limit`/
`tpm_limit` already exist as of baseline -- not touched here); guard-adds
`token_usage.source`/`estimated` (both already declared on the ORM model
by earlier commits on this line but never landed by a migration until now);
adds `organizations.rpm_limit` (§12.1 per-org Cilium edge RPM -- the
reconciler already reads this via getattr against live reflection); and
seeds `token_conversion_rates` from `shared.utils.token_manager
.TokenManager.DEFAULT_CONVERSION_RATES`.

The conversion-rate seed carries real sub-cent decimals (e.g. 0.00015), but
`token_conversion_rates.input_rate`/`output_rate`/`base_cost_per_waddleai_token`
were declared `Integer` in the pre-existing baseline schema -- inserting
these seed values as-is would silently truncate every rate to a whole
number on strict-typed backends (PostgreSQL/MySQL). Since this migration is
what first writes real data into those columns, it also widens them to
`Float` (batch-mode, SQLite-safe) immediately before seeding.

Revision ID: 007_drop_ailb_add_native_limits
Revises: 006_add_memory_scope
Create Date: 2026-08-14
"""

import json
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "007_drop_ailb_add_native_limits"
down_revision: str | None = "006_add_memory_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Mirrors shared.utils.token_manager.TokenManager.DEFAULT_CONVERSION_RATES.
# Duplicated (not imported) deliberately -- migrations must reproduce a fixed
# historical seed, not track whatever the current application module says.
def _rate(
    provider: str, model: str, input_rate: float, output_rate: float, base_cost: float
) -> dict:
    """Build one DEFAULT_CONVERSION_RATES seed row (kept off one line for E501)."""
    return {
        "provider": provider,
        "model": model,
        "input_rate": input_rate,
        "output_rate": output_rate,
        "base_cost_per_waddleai_token": base_cost,
    }


_DEFAULT_CONVERSION_RATES = [
    _rate("openai", "gpt-4", 10.0, 20.0, 0.003),
    _rate("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    _rate("openai", "gpt-4o", 10.0, 20.0, 0.0005),
    _rate("openai", "gpt-4o-mini", 10.0, 10.0, 0.00015),
    _rate("openai", "gpt-3.5-turbo", 10.0, 10.0, 0.0002),
    _rate("anthropic", "claude-3-opus", 10.0, 20.0, 0.0075),
    _rate("anthropic", "claude-3-sonnet", 10.0, 20.0, 0.0015),
    _rate("anthropic", "claude-3-haiku", 10.0, 10.0, 0.00025),
    _rate("anthropic", "claude-3.5-sonnet", 10.0, 20.0, 0.003),
    _rate("ollama", "llama2", 10.0, 10.0, 0.0),
    _rate("ollama", "mistral", 10.0, 10.0, 0.0),
    _rate("ollama", "codellama", 10.0, 10.0, 0.0),
]

_TOKEN_USAGE_INSERT = sa.text(
    "INSERT INTO token_usage "
    "(virtual_key_id, user_id, organization_id, date, waddleai_tokens, "
    " llm_tokens, tokens_input_total, tokens_output_total, request_count, "
    " cost_usd_total, last_updated, source, estimated) "
    "VALUES (:virtual_key_id, :user_id, :organization_id, :date, 0, "
    " :llm_tokens, :tokens_input_total, :tokens_output_total, 1, "
    " :cost_usd_total, :last_updated, :source, :estimated)"
)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True if `column` already exists on `table` (guard for pre-migration ORM drift)."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _fold_ailb_usage(connection: sa.engine.Connection) -> int:
    """Fold ailb_usage_events + ailb_usage_records into token_usage.

    Returns the number of rows folded. A source table with zero rows simply
    contributes zero INSERTs -- safe on a fresh/empty database.
    """
    now = datetime.utcnow()
    folded = 0

    for row in connection.execute(
        sa.text(
            "SELECT virtual_key_id, model, provider, input_tokens, "
            "output_tokens, cost_usd, timestamp, created_at FROM ailb_usage_events"
        )
    ).mappings():
        input_tokens = row["input_tokens"] or 0
        output_tokens = row["output_tokens"] or 0
        llm_tokens = None
        if row["model"]:
            llm_tokens = json.dumps(
                {
                    f"{row['provider']}:{row['model']}": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                }
            )
        connection.execute(
            _TOKEN_USAGE_INSERT,
            {
                "virtual_key_id": row["virtual_key_id"],
                "user_id": None,
                "organization_id": None,
                "date": row["timestamp"] or row["created_at"] or now,
                "llm_tokens": llm_tokens,
                "tokens_input_total": input_tokens,
                "tokens_output_total": output_tokens,
                "cost_usd_total": row["cost_usd"] or 0,
                "last_updated": now,
                "source": "ailb_import",
                "estimated": False,
            },
        )
        folded += 1

    for row in connection.execute(
        sa.text(
            "SELECT user_id, model, provider, input_tokens, output_tokens, "
            "timestamp, created_at FROM ailb_usage_records"
        )
    ).mappings():
        input_tokens = row["input_tokens"] or 0
        output_tokens = row["output_tokens"] or 0
        try:
            user_id = int(row["user_id"]) if row["user_id"] is not None else None
        except (TypeError, ValueError):
            # AILBUsageRecord.user_id was a free-form string (often an external
            # identity, not a WaddleAI users.id) -- keep the usage row for
            # billing continuity, just without a resolvable owner.
            user_id = None
        llm_tokens = None
        if row["model"]:
            llm_tokens = json.dumps(
                {
                    f"{row['provider']}:{row['model']}": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                }
            )
        connection.execute(
            _TOKEN_USAGE_INSERT,
            {
                "virtual_key_id": None,
                "user_id": user_id,
                "organization_id": None,
                "date": row["timestamp"] or row["created_at"] or now,
                "llm_tokens": llm_tokens,
                "tokens_input_total": input_tokens,
                "tokens_output_total": output_tokens,
                "cost_usd_total": 0,
                "last_updated": now,
                "source": "ailb_import",
                "estimated": False,
            },
        )
        folded += 1

    return folded


def upgrade() -> None:
    """Drop AILB tables, fold their usage history into token_usage, and add native rate limits."""
    bind = op.get_bind()

    # Guard-add: budget_monthly_tokens/budget_monthly_usd may already exist
    # on databases provisioned via create_all() after the ORM model picked
    # them up (rpm_limit/tpm_limit are NOT touched here -- already present
    # since baseline).
    if not _has_column(bind, "virtual_keys", "budget_monthly_tokens"):
        op.add_column(
            "virtual_keys", sa.Column("budget_monthly_tokens", sa.Integer(), nullable=True)
        )
    if not _has_column(bind, "virtual_keys", "budget_monthly_usd"):
        op.add_column("virtual_keys", sa.Column("budget_monthly_usd", sa.Integer(), nullable=True))

    # Guard-add token_usage.source/estimated (same drift as above). NOT NULL
    # with server_default is safe as a single step (existing rows backfill).
    if not _has_column(bind, "token_usage", "source"):
        op.add_column(
            "token_usage",
            sa.Column("source", sa.String(50), nullable=False, server_default="aiproxy"),
        )
    if not _has_column(bind, "token_usage", "estimated"):
        op.add_column(
            "token_usage",
            sa.Column("estimated", sa.Boolean(), nullable=False, server_default="false"),
        )

    # Fold AILB usage history into token_usage BEFORE dropping the AILB
    # tables -- this must run after the guard-adds above so `source`/
    # `estimated` are available columns to insert into.
    _fold_ailb_usage(bind)

    op.drop_table("ailb_usage_events")
    op.drop_table("ailb_usage_records")
    op.drop_table("marchproxy_ailb_sync")

    with op.batch_alter_table("virtual_keys") as batch_op:
        batch_op.drop_column("ailb_key_id")
        batch_op.drop_column("ailb_sync_status")

    op.add_column("organizations", sa.Column("rpm_limit", sa.Integer(), nullable=True))

    # Widen token_conversion_rates rate columns to Float before seeding --
    # see module docstring. batch mode keeps this SQLite-safe.
    with op.batch_alter_table("token_conversion_rates") as batch_op:
        batch_op.alter_column(
            "input_rate", existing_type=sa.Integer(), type_=sa.Float(), nullable=False
        )
        batch_op.alter_column(
            "output_rate", existing_type=sa.Integer(), type_=sa.Float(), nullable=False
        )
        batch_op.alter_column(
            "base_cost_per_waddleai_token",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            nullable=True,
        )

    conversion_rates_table = sa.table(
        "token_conversion_rates",
        sa.column("provider", sa.String),
        sa.column("model", sa.String),
        sa.column("input_rate", sa.Float),
        sa.column("output_rate", sa.Float),
        sa.column("base_cost_per_waddleai_token", sa.Float),
        sa.column("effective_date", sa.DateTime),
        sa.column("enabled", sa.Boolean),
    )
    now = datetime.utcnow()
    op.bulk_insert(
        conversion_rates_table,
        [{**rate, "effective_date": now, "enabled": True} for rate in _DEFAULT_CONVERSION_RATES],
    )


def downgrade() -> None:
    """Recreate the dropped AILB tables/columns and revert rate columns to Integer."""
    bind = op.get_bind()

    # Remove exactly the rows this migration seeded (matched by
    # provider+model); any organically-added rates are left untouched.
    delete_seed = sa.text(
        "DELETE FROM token_conversion_rates WHERE provider = :provider AND model = :model"
    )
    for rate in _DEFAULT_CONVERSION_RATES:
        bind.execute(delete_seed, {"provider": rate["provider"], "model": rate["model"]})

    with op.batch_alter_table("token_conversion_rates") as batch_op:
        batch_op.alter_column(
            "base_cost_per_waddleai_token",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            nullable=True,
        )
        batch_op.alter_column(
            "output_rate", existing_type=sa.Float(), type_=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "input_rate", existing_type=sa.Float(), type_=sa.Integer(), nullable=False
        )

    op.drop_column("organizations", "rpm_limit")

    op.add_column("virtual_keys", sa.Column("ailb_key_id", sa.String(255), nullable=True))
    op.add_column("virtual_keys", sa.Column("ailb_sync_status", sa.String(50), nullable=True))

    op.create_table(
        "marchproxy_ailb_sync",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id"), nullable=True),
        sa.Column("ailb_instance_id", sa.String(255), nullable=True),
        sa.Column("ailb_route_id", sa.String(255), nullable=True),
        sa.Column("sync_status", sa.String(50), nullable=True),
        sa.Column("last_synced", sa.DateTime(), nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "ailb_usage_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(255), unique=True, nullable=True),
        sa.Column("virtual_key_id", sa.Integer(), sa.ForeignKey("virtual_keys.id"), nullable=True),
        sa.Column("ailb_key_id", sa.String(255), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("processed", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "ailb_usage_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("api_key_id", sa.String(255), nullable=True),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_ailb_usage_records_user_id", "ailb_usage_records", ["user_id"])
    op.create_index("ix_ailb_usage_records_api_key_id", "ailb_usage_records", ["api_key_id"])

    # NOTE: rows folded into token_usage (source='ailb_import') by upgrade()
    # are NOT moved back / deleted here -- they are now indistinguishable
    # from any other billing history and unfolding would itself be a data
    # loss risk. Documented behavior (spec §13, Q#1 continuity intent).
    if _has_column(bind, "token_usage", "estimated"):
        op.drop_column("token_usage", "estimated")
    if _has_column(bind, "token_usage", "source"):
        op.drop_column("token_usage", "source")

    if _has_column(bind, "virtual_keys", "budget_monthly_usd"):
        op.drop_column("virtual_keys", "budget_monthly_usd")
    if _has_column(bind, "virtual_keys", "budget_monthly_tokens"):
        op.drop_column("virtual_keys", "budget_monthly_tokens")
