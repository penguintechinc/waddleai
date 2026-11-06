"""Tests for shared.quota.virtual_price (Plan B task B4d, AM2 divisor-rate formula)."""

from decimal import Decimal

import pytest

from shared.quota.virtual_price import upsert_virtual_rate, virtual_rates
from tests.conformance._fake_dal import FakeDAL

KEY = "deployment/123e4567-e89b-12d3-a456-426614174000"


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
    upsert_virtual_rate(db, "vllm", KEY, Decimal("0.5"), Decimal("2.0"))
    (row,) = _rows(db, "vllm", KEY)
    assert row.input_rate == pytest.approx(2000.0)  # type: ignore[attr-defined]
    assert row.output_rate == pytest.approx(500.0)  # type: ignore[attr-defined]
    assert row.base_cost_per_waddleai_token == 0.001  # type: ignore[attr-defined]
    assert row.enabled is True  # type: ignore[attr-defined]


def test_upsert_is_idempotent_and_updates_in_place() -> None:
    """A second call with new prices updates the same row, never duplicates it."""
    db = FakeDAL()
    upsert_virtual_rate(db, "ollama", KEY, Decimal(1), Decimal(1))
    upsert_virtual_rate(db, "ollama", KEY, Decimal(2), Decimal(4))
    (row,) = _rows(db, "ollama", KEY)
    assert row.input_rate == pytest.approx(500.0)  # type: ignore[attr-defined]
    assert row.output_rate == pytest.approx(250.0)  # type: ignore[attr-defined]


def test_upsert_honours_custom_base_cost() -> None:
    """The base cost is a keyword override of the 0.001 default."""
    db = FakeDAL()
    upsert_virtual_rate(
        db, "llamacpp", KEY, Decimal(1), Decimal(1), base_cost_per_waddleai_token=Decimal("0.01")
    )
    (row,) = _rows(db, "llamacpp", KEY)
    assert row.input_rate == pytest.approx(10000.0)  # type: ignore[attr-defined]
    assert row.base_cost_per_waddleai_token == 0.01  # type: ignore[attr-defined]


def test_upsert_rejects_bad_price_without_writing() -> None:
    """An invalid price raises before any row is written."""
    db = FakeDAL()
    with pytest.raises(ValueError, match="price"):
        upsert_virtual_rate(db, "vllm", KEY, Decimal(0), Decimal(1))
    assert _rows(db, "vllm", KEY) == []


def test_existing_disabled_row_stays_disabled_but_gets_new_rates() -> None:
    """AM26: an admin-disabled rate is updated in place without being re-enabled."""
    db = FakeDAL()
    upsert_virtual_rate(db, "vllm", KEY, Decimal(1), Decimal(1))
    (row,) = _rows(db, "vllm", KEY)
    row.enabled = False  # type: ignore[attr-defined]
    upsert_virtual_rate(db, "vllm", KEY, Decimal(2), Decimal(2))
    (row,) = _rows(db, "vllm", KEY)
    assert row.enabled is False  # type: ignore[attr-defined]
    assert row.input_rate == pytest.approx(500.0)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "bad", ["served-name", "deployment/not-a-uuid", "deployment/", "llama-70b"]
)
def test_non_deployment_key_rejected(bad: str) -> None:
    """AM26: only deployment/<uuid> keys may be written; nothing is stored."""
    db = FakeDAL()
    with pytest.raises(ValueError, match="deployment/<uuid>"):
        upsert_virtual_rate(db, "vllm", bad, Decimal(1), Decimal(1))
    assert _rows(db, "vllm", bad) == []
