"""Elastic pooled quota: conversion-rate cleanup + the five quota tables.

Rates part (spec §4, closes #297): collapse duplicate ``(provider, model)`` rows to the oldest
(the seed/admin row beats a later lazy default insert), add ``UNIQUE(provider, model)``, and
insert any missing canonical seed rows (never overwriting an existing/admin-edited rate).
Tables part (spec §5): create the five ``quota_*`` tables exactly as the SQLAlchemy authority
defines them and backfill one DISABLED ``quota_org_pool`` row per organization, so flag-off and
unconfigured is a strict no-op (expand/contract: nothing reads them until enabled).
Every step is guarded so it is a no-op where ``init_schema()``/``create_all`` already did it.

Revision ID: 026_pooled_quota
Revises: 025_fk_indexes
Create Date: 2026-10-16
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "026_pooled_quota"
down_revision: str | None = "025_fk_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- conversion-rate cleanup (Task 6) ---

_UQ_NAME = "uq_token_conversion_rates_provider_model"

# Frozen copy of shared.utils.default_conversion_rates.DEFAULT_RATE_ROWS (a historical migration
# must not import live app code); a unit test asserts the two stay equal.
_CANONICAL_RATES: tuple[tuple[str, str, float, float, float], ...] = (
    ("openai", "gpt-4o", 10.0, 20.0, 0.0025),
    ("openai", "gpt-4o-mini", 30.0, 40.0, 0.0001),
    ("openai", "gpt-4-turbo", 10.0, 20.0, 0.001),
    ("openai", "gpt-4", 10.0, 20.0, 0.003),
    ("openai", "gpt-3.5-turbo", 20.0, 30.0, 0.0005),
    ("anthropic", "claude-3-5-sonnet-latest", 12.0, 18.0, 0.003),
    ("anthropic", "claude-3-opus-20240229", 8.0, 15.0, 0.015),
    ("anthropic", "claude-3-sonnet-20240229", 12.0, 18.0, 0.003),
    ("anthropic", "claude-3-haiku-20240307", 25.0, 35.0, 0.00025),
    ("ollama", "llama3.2", 50.0, 50.0, 0.0),
    ("ollama", "llama3.1", 50.0, 50.0, 0.0),
    ("ollama", "mistral", 45.0, 45.0, 0.0),
    ("gemini", "gemini-1.5-pro", 12.0, 18.0, 0.00125),
    ("gemini", "gemini-1.5-flash", 30.0, 40.0, 0.000075),
    ("cohere", "command-r-plus", 15.0, 20.0, 0.003),
    ("cohere", "command-r", 25.0, 30.0, 0.0005),
)


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    """True if ``table`` exists (guard for create_all-bootstrapped or drifted databases)."""
    return table in set(sa.inspect(bind).get_table_names())


def _has_unique(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """True if a unique constraint or unique index called ``name`` exists on ``table``."""
    insp = sa.inspect(bind)
    names = {u["name"] for u in insp.get_unique_constraints(table)}
    names |= {i["name"] for i in insp.get_indexes(table) if i.get("unique")}
    return name in names


def _rates_upgrade(bind: sa.engine.Connection) -> None:
    """Dedup, add UNIQUE(provider, model), then seed any missing canonical rate rows."""
    if not _has_table(bind, "token_conversion_rates"):
        return
    bind.execute(
        sa.text(
            "DELETE FROM token_conversion_rates WHERE id NOT IN "
            "(SELECT MIN(id) FROM token_conversion_rates GROUP BY provider, model)"
        )
    )
    if not _has_unique(bind, "token_conversion_rates", _UQ_NAME):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.create_unique_constraint(_UQ_NAME, ["provider", "model"])
    now = datetime.utcnow()
    for provider, model, in_rate, out_rate, cost in _CANONICAL_RATES:
        exists = bind.execute(
            sa.text("SELECT 1 FROM token_conversion_rates WHERE provider = :p AND model = :m"),
            {"p": provider, "m": model},
        ).first()
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO token_conversion_rates (provider, model, input_rate, "
                    "output_rate, base_cost_per_waddleai_token, effective_date, enabled) "
                    "VALUES (:p, :m, :i, :o, :c, :now, :en)"
                ),
                {"p": provider, "m": model, "i": in_rate, "o": out_rate, "c": cost,
                 "now": now, "en": True},
            )  # fmt: skip


# --- quota tables (Task 10) ---

_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_quota_team_pool_org", "quota_team_pool", ["organization_id"]),
    ("ix_quota_user_res_team", "quota_user_reservation", ["team_id"]),
    ("ix_quota_override_org", "quota_override", ["organization_id"]),
    ("ix_quota_ledger_org_cycle", "quota_usage_ledger", ["organization_id", "cycle_index"]),
)
_QUOTA_TABLES_CHILD_FIRST: tuple[str, ...] = (
    "quota_usage_ledger",
    "quota_override",
    "quota_user_reservation",
    "quota_team_pool",
    "quota_org_pool",
)


def _has_index(bind: sa.engine.Connection, table: str, name: str) -> bool:
    """True when ``table`` already carries an index called ``name``."""
    return name in {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _pk() -> sa.Column:
    """Autoincrement integer primary key column shared by all five quota tables."""
    return sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True)


def _created(name: str = "created_at") -> sa.Column:
    """Server-defaulted timestamp column."""
    return sa.Column(name, sa.DateTime(), server_default=sa.func.now())


def _fk(col: str, target: str, *, cascade: bool = False, nullable: bool = False) -> sa.Column:
    """Integer foreign-key column, optionally ``ON DELETE CASCADE``."""
    fk = sa.ForeignKey(target, ondelete="CASCADE") if cascade else sa.ForeignKey(target)
    return sa.Column(col, sa.Integer(), fk, nullable=nullable)


def _credits(name: str, *, nullable: bool = False, zero: bool = True) -> sa.Column:
    """BigInteger credit column; ``zero`` adds a server default of 0."""
    return sa.Column(name, sa.BigInteger(), nullable=nullable, server_default="0" if zero else None)


def _create_quota_tables(bind: sa.engine.Connection) -> None:
    """Create each missing quota table with the authority's constraint names."""
    specs: dict[str, tuple] = {
        "quota_org_pool": (
            _pk(),
            _fk("org_id", "organizations.id", cascade=True),
            sa.Column("cycle_anchor_date", sa.Date(), nullable=True),
            _credits("pool_credits"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            _created(),
            sa.UniqueConstraint("org_id", name="uq_quota_org_pool_org"),
        ),
        "quota_team_pool": (
            _pk(),
            _fk("team_id", "teams.id", cascade=True),
            _fk("organization_id", "organizations.id"),
            _credits("pool_credits"),
            _credits("shared_buffer_credits"),
            _created(),
            sa.UniqueConstraint("team_id", name="uq_quota_team_pool_team"),
        ),
        "quota_user_reservation": (
            _pk(),
            _fk("team_id", "teams.id", cascade=True),
            _fk("user_id", "users.id", cascade=True),
            _fk("organization_id", "organizations.id"),
            _credits("guaranteed_credits"),
            _credits("daily_subcap", nullable=True, zero=False),
            sa.UniqueConstraint("user_id", "organization_id", name="uq_quota_user_res_user_org"),
        ),
        "quota_override": (
            _pk(),
            _fk("organization_id", "organizations.id"),
            _fk("user_id", "users.id", cascade=True, nullable=True),
            sa.Column("model", sa.String(255), nullable=True),
            sa.Column("provider", sa.String(100), nullable=True),
            _credits("cap_credits", zero=False),
            _created(),
            sa.CheckConstraint(
                "user_id IS NOT NULL OR model IS NOT NULL OR provider IS NOT NULL",
                name="ck_quota_override_has_dimension",
            ),
        ),
        "quota_usage_ledger": (
            _pk(),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=False),
            _fk("organization_id", "organizations.id"),
            sa.Column("cycle_index", sa.Integer(), nullable=False),
            _credits("credits_used"),
            _created("updated_at"),
            sa.UniqueConstraint(
                "scope_type",
                "scope_id",
                "organization_id",
                "cycle_index",
                name="uq_quota_ledger_scope_cycle",
            ),  # fmt: skip
        ),
    }
    for name, items in specs.items():
        if not _has_table(bind, name):
            op.create_table(name, *items)


def _tables_upgrade(bind: sa.engine.Connection) -> None:
    """Create the quota tables + indexes (guarded) and backfill one disabled pool per org."""
    _create_quota_tables(bind)
    for name, table, cols in _INDEXES:
        if not _has_index(bind, table, name):
            op.create_index(name, table, cols)
    bind.execute(
        sa.text(
            "INSERT INTO quota_org_pool (org_id, pool_credits, enabled, created_at) "
            "SELECT o.id, 0, :enabled, :now FROM organizations o "
            "WHERE NOT EXISTS (SELECT 1 FROM quota_org_pool p WHERE p.org_id = o.id)"
        ),
        {"enabled": False, "now": datetime.utcnow()},
    )


def _tables_downgrade(bind: sa.engine.Connection) -> None:
    """Drop the five quota tables, children before parents; their indexes go with them."""
    for name in _QUOTA_TABLES_CHILD_FIRST:
        if _has_table(bind, name):
            op.drop_table(name)


def upgrade() -> None:
    """Apply the conversion-rate cleanup, then create the quota tables."""
    bind = op.get_bind()
    _rates_upgrade(bind)
    _tables_upgrade(bind)


def downgrade() -> None:
    """Drop the quota tables, then the rates UNIQUE; deduped/seeded rate rows are kept."""
    bind = op.get_bind()
    _tables_downgrade(bind)
    if _has_table(bind, "token_conversion_rates") and _has_unique(
        bind, "token_conversion_rates", _UQ_NAME
    ):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.drop_constraint(_UQ_NAME, type_="unique")
