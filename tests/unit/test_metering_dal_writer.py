"""Regression tests for PenguinDALUsageWriter against a real penguin_dal DB.

penguin_dal's Row has no update_record() (that's classic PyDAL API) -- see
shared/utils/metering.py. A MagicMock-based test wouldn't catch a regression
back to that call (MagicMock happily accepts .update_record() and returns
another mock), which is exactly how this bug shipped undetected. These tests
run write_aggregated_row against a genuine sqlite-backed penguin_dal DAL so a
revert to the old API fails with the same AttributeError production hits,
and assert the persisted row values directly rather than just that a
function was called.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from penguin_dal import DAL, Field
from penguin_dal.query import QuerySet

from shared.utils.metering import AggregatedMetrics, PenguinDALUsageWriter


def _make_db(tmp_path: Path) -> DAL:
    """A standalone sqlite-backed DAL with a minimal token_usage table.

    Column set matches exactly what PenguinDALUsageWriter reads/writes --
    intentionally smaller than the full production schema (which is defined
    via SQLAlchemy/Alembic and auto-reflected, not via this module).
    """
    db = DAL(f"sqlite:///{tmp_path / 'metering.db'}")
    db.define_table(
        "token_usage",
        Field("virtual_key_id", "integer"),
        Field("user_id", "integer"),
        Field("organization_id", "integer"),
        Field("date", "date"),
        Field("waddleai_tokens", "integer", default=0),
        Field("llm_tokens", "text"),
        Field("tokens_input_total", "integer", default=0),
        Field("tokens_output_total", "integer", default=0),
        Field("request_count", "integer", default=0),
        Field("cost_usd_total", "double", default=0),
        Field("source", "string"),
        Field("estimated", "boolean", default=False),
        Field("tokens_saved", "integer", default=0),
        Field("cache_status", "string"),
        Field("last_updated", "datetime"),
    )
    return db


def _agg(**overrides: object) -> AggregatedMetrics:
    defaults: dict[str, object] = dict(
        virtual_key_id=1,
        model="gpt-4",
        provider="openai",
        minute_bucket=datetime(2026, 1, 1, 12, 0),
        total_input_tokens=10,
        total_output_tokens=5,
        request_count=1,
    )
    defaults.update(overrides)
    return AggregatedMetrics(**defaults)  # type: ignore[arg-type]


def test_write_aggregated_row_updates_existing_row_in_place(tmp_path: Path) -> None:
    """Verify the write path actually accumulates across flushes.

    The second flush for the same (vkey, day) must accumulate onto the
    existing row via db(condition).update() -- not silently no-op like the
    old Row.update_record() call did.
    """
    db = _make_db(tmp_path)
    writer = PenguinDALUsageWriter(db=db)

    writer.write_aggregated_row(_agg(total_input_tokens=10, total_output_tokens=5, request_count=1))
    writer.write_aggregated_row(_agg(total_input_tokens=7, total_output_tokens=3, request_count=2))

    rows = db(db.token_usage.virtual_key_id == 1).select()
    assert len(rows) == 1  # accumulated onto one row, not a second insert
    row = rows.first()
    assert row.tokens_input_total == 17  # 10 + 7 -- stuck at 10 if the update silently no-opped
    assert row.tokens_output_total == 8
    assert row.request_count == 3


def test_write_aggregated_row_merges_llm_breakdown_across_flushes(tmp_path: Path) -> None:
    """The per-model JSON breakdown must also reflect the second flush."""
    import json

    db = _make_db(tmp_path)
    writer = PenguinDALUsageWriter(db=db)

    writer.write_aggregated_row(_agg(total_input_tokens=10, total_output_tokens=5))
    writer.write_aggregated_row(_agg(total_input_tokens=7, total_output_tokens=3))

    row = db(db.token_usage.virtual_key_id == 1).select().first()
    breakdown = json.loads(row.llm_tokens)
    assert breakdown["openai_gpt_4"]["input"] == 17
    assert breakdown["openai_gpt_4"]["output"] == 8


def test_write_aggregated_row_does_not_swallow_db_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a DB failure propagates instead of being swallowed.

    UsageWriter.write_aggregated_row documents 'Raises: Any exception ...
    (caller handles)' -- MeteringBuffer.flush()'s retry queue depends on
    that. A broad `except Exception: logger.error(...)` around the whole
    method (as it used to have) would silently defeat the retry path by
    turning every DB failure into a no-op instead of a re-queued aggregate.
    """
    db = _make_db(tmp_path)
    writer = PenguinDALUsageWriter(db=db)
    writer.write_aggregated_row(_agg())  # creates the row so the next call hits the update branch

    def _boom(self: QuerySet, **kwargs: object) -> int:
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(QuerySet, "update", _boom)

    with pytest.raises(RuntimeError, match="simulated db failure"):
        writer.write_aggregated_row(_agg())
