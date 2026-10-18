"""Elastic pooled quota: conversion-rate cleanup (+ quota tables, added by a later task).

Rates part (spec §4, closes #297): collapse duplicate ``(provider, model)`` rows to the oldest
(the seed/admin row beats a later lazy default insert), add ``UNIQUE(provider, model)``, and
insert any missing canonical seed rows (never overwriting an existing/admin-edited rate).
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


# --- quota tables (Task 10 appends here: _tables_upgrade / _tables_downgrade) ---


def upgrade() -> None:
    """Apply the conversion-rate cleanup."""
    bind = op.get_bind()
    _rates_upgrade(bind)


def downgrade() -> None:
    """Drop the UNIQUE constraint; deduped rows and seeded rates are intentionally kept."""
    bind = op.get_bind()
    if _has_table(bind, "token_conversion_rates") and _has_unique(
        bind, "token_conversion_rates", _UQ_NAME
    ):
        with op.batch_alter_table("token_conversion_rates") as batch:
            batch.drop_constraint(_UQ_NAME, type_="unique")
