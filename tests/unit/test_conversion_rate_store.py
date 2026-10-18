"""UNIQUE(provider, model) + the ensure/upsert helpers (spec §4, issue #297)."""

from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from shared.utils.conversion_rate_store import ensure_conversion_rate, upsert_conversion_rate
from shared.utils.default_conversion_rates import RateRow

_ROW = RateRow("openai", "gpt-4o", 10.0, 20.0, 0.0025)


def _rows(db, provider, model):
    """All token_conversion_rates rows for one (provider, model)."""
    t = db.token_conversion_rates
    return list(db((t.provider == provider) & (t.model == model)).select())


def test_database_rejects_a_duplicate_provider_model(real_schema_db):
    """regression: gh-297 -- the table had NO unique constraint, so lazy inserts duplicated rows."""
    db = real_schema_db
    db.token_conversion_rates.insert(
        provider="openai", model="gpt-4o", input_rate=1.0, output_rate=1.0
    )
    with pytest.raises(IntegrityError):
        db.token_conversion_rates.insert(
            provider="openai", model="gpt-4o", input_rate=2.0, output_rate=2.0
        )


def test_ensure_inserts_once_and_never_overwrites(real_schema_db):
    """Seeding twice keeps one row, and an admin-edited rate survives a re-seed."""
    db = real_schema_db
    assert ensure_conversion_rate(db, _ROW) is True
    upsert_conversion_rate(db, provider="openai", model="gpt-4o", input_rate=99.0, output_rate=99.0)
    assert ensure_conversion_rate(db, _ROW) is False
    (row,) = _rows(db, "openai", "gpt-4o")
    assert row.input_rate == 99.0


def test_upsert_updates_in_place_and_returns_the_same_id(real_schema_db):
    """Upsert is idempotent per (provider, model): same id, latest values, enabled flag honoured."""
    db = real_schema_db
    first = upsert_conversion_rate(
        db, provider="gemini", model="m", input_rate=5.0, output_rate=6.0
    )
    second = upsert_conversion_rate(
        db, provider="gemini", model="m", input_rate=7.0, output_rate=8.0, enabled=False
    )
    assert first == second
    (row,) = _rows(db, "gemini", "m")
    assert (row.input_rate, row.output_rate, row.enabled) == (7.0, 8.0, False)


class _RacyTable:
    """Table stub whose insert always loses the unique race."""

    def insert(self, **_kw):
        """Raise the unique violation a concurrent writer would cause."""
        raise IntegrityError("insert", {}, Exception("unique"))


class _RacyDb:
    """penguin-dal stand-in: the row never exists on select/update, and every insert collides."""

    token_conversion_rates = _RacyTable()

    def __call__(self, _query):
        """Return a query whose select is empty and update touches nothing."""
        return self

    def select(self):
        """Empty result set with a ``.first()``."""
        return self

    def first(self):
        """No row."""
        return None

    def update(self, **_kw):
        """Zero rows updated."""
        return 0


class _Col:
    """Column stub supporting ``==`` and ``&`` so the query expression builds."""

    def __eq__(self, _o):
        return self

    def __and__(self, _o):
        return self


_racy_table: Any = _RacyDb.token_conversion_rates
_racy_table.provider = _racy_table.model = _Col()


def test_ensure_returns_false_when_a_concurrent_seeder_wins():
    """A unique violation on insert is a lost race, not an error."""
    assert ensure_conversion_rate(_RacyDb(), _ROW) is False


def test_upsert_raises_when_it_never_converges():
    """Persistent insert collisions with no updatable row fail loudly after bounded retries."""
    with pytest.raises(RuntimeError, match="did not converge"):
        upsert_conversion_rate(_RacyDb(), provider="p", model="m", input_rate=1.0, output_rate=1.0)
