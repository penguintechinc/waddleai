"""Tests for shared.quota.virtual_price (Plan B task B4d, AM2 divisor-rate formula)."""

from decimal import Decimal

import pytest

from shared.quota.virtual_price import upsert_virtual_rate, virtual_rates
from tests.conformance._fake_dal import FakeDAL


def _rows(db: FakeDAL, provider: str, model: str) -> list[object]:
    """All token_conversion_rates rows for one (provider, model)."""
    t = db.token_conversion_rates
    return list(db((t.provider == provider) & (t.model == model)).select())


def test_virtual_rates_are_divisors_base_times_1e6_over_price() -> None:
    """Rate = base * 1e6 / price per 1M tokens (LLM tokens per credit)."""
    rin, rout = virtual_rates(Decimal("0.9"), Decimal("4.5"), Decimal("0.001"))
    assert rin == Decimal("0.001") * 1000000 / Decimal("0.9")
    assert rout == Decimal("0.001") * 1000000 / Decimal("4.5")


@pytest.mark.parametrize("bad", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_virtual_rates_reject_non_positive_or_non_finite(bad: Decimal) -> None:
    """A zero, negative, NaN or infinite price cannot be inverted into a rate."""
    with pytest.raises(ValueError, match="price"):
        virtual_rates(bad, Decimal(1), Decimal("0.001"))
    with pytest.raises(ValueError, match="price"):
        virtual_rates(Decimal(1), bad, Decimal("0.001"))


def test_virtual_rates_reject_non_positive_base() -> None:
    """The base cost per credit must be positive."""
    with pytest.raises(ValueError, match="base_cost"):
        virtual_rates(Decimal(1), Decimal(1), Decimal(0))


def test_upsert_inserts_row_with_divisor_rates_and_default_base() -> None:
    """First call inserts (provider, model) with rates from the formula and base 0.001."""
    db = FakeDAL()
    upsert_virtual_rate(db, "vllm", "llama-70b", Decimal("0.5"), Decimal("2.0"))
    (row,) = _rows(db, "vllm", "llama-70b")
    assert row.input_rate == pytest.approx(2000.0)  # type: ignore[attr-defined]
    assert row.output_rate == pytest.approx(500.0)  # type: ignore[attr-defined]
    assert row.base_cost_per_waddleai_token == 0.001  # type: ignore[attr-defined]
    assert row.enabled is True  # type: ignore[attr-defined]


def test_upsert_is_idempotent_and_updates_in_place() -> None:
    """A second call with new prices updates the same row, never duplicates it."""
    db = FakeDAL()
    upsert_virtual_rate(db, "ollama", "gemma", Decimal(1), Decimal(1))
    upsert_virtual_rate(db, "ollama", "gemma", Decimal(2), Decimal(4))
    (row,) = _rows(db, "ollama", "gemma")
    assert row.input_rate == pytest.approx(500.0)  # type: ignore[attr-defined]
    assert row.output_rate == pytest.approx(250.0)  # type: ignore[attr-defined]


def test_upsert_honours_custom_base_cost() -> None:
    """The base cost is a keyword override of the 0.001 default."""
    db = FakeDAL()
    upsert_virtual_rate(
        db, "llamacpp", "m", Decimal(1), Decimal(1), base_cost_per_waddleai_token=Decimal("0.01")
    )
    (row,) = _rows(db, "llamacpp", "m")
    assert row.input_rate == pytest.approx(10000.0)  # type: ignore[attr-defined]
    assert row.base_cost_per_waddleai_token == 0.01  # type: ignore[attr-defined]


def test_upsert_rejects_bad_price_without_writing() -> None:
    """An invalid price raises before any row is written."""
    db = FakeDAL()
    with pytest.raises(ValueError, match="price"):
        upsert_virtual_rate(db, "vllm", "m", Decimal(0), Decimal(1))
    assert _rows(db, "vllm", "m") == []
