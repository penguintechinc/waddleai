"""Virtual-price to conversion-rate bridge for locally hosted models (AM2, AM10).

A locally hosted deployment's price per 1M tokens is charged through the existing
``token_conversion_rates`` table, whose rates are DIVISORS (LLM tokens per WaddleAI credit).
This module converts a price into those divisors and upserts the (provider, model) row, so
``QuotaEngine``'s existing lookup charges the virtual price unchanged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from shared.utils.conversion_rate_store import upsert_conversion_rate

DEFAULT_BASE_COST_PER_WADDLEAI_TOKEN = Decimal("0.001")
_MILLION = Decimal(1_000_000)


def _check_price(name: str, value: Decimal) -> None:
    """Reject a price that cannot be inverted into a divisor rate."""
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} price must be finite and > 0, got {value}")


def virtual_rates(
    price_input: Decimal, price_output: Decimal, base_cost_per_waddleai_token: Decimal
) -> tuple[Decimal, Decimal]:
    """Return ``(input_rate, output_rate)`` = ``base * 1e6 / price`` (AM2 divisor convention)."""
    if not base_cost_per_waddleai_token.is_finite() or base_cost_per_waddleai_token <= 0:
        raise ValueError("base_cost_per_waddleai_token must be finite and > 0")
    _check_price("input", price_input)
    _check_price("output", price_output)
    scale = base_cost_per_waddleai_token * _MILLION
    return scale / price_input, scale / price_output


def upsert_virtual_rate(
    db: Any,
    provider: str,
    model: str,
    price_input_per_1m: Decimal,
    price_output_per_1m: Decimal,
    *,
    base_cost_per_waddleai_token: Decimal = DEFAULT_BASE_COST_PER_WADDLEAI_TOKEN,
) -> None:
    """Create or update the ``token_conversion_rates`` row charging this virtual price.

    Synchronous (penguin-dal); async callers wrap it in ``asyncio.to_thread``.
    """
    rate_in, rate_out = virtual_rates(
        price_input_per_1m, price_output_per_1m, base_cost_per_waddleai_token
    )
    upsert_conversion_rate(
        db,
        provider=provider,
        model=model,
        input_rate=float(rate_in),
        output_rate=float(rate_out),
        base_cost_per_waddleai_token=float(base_cost_per_waddleai_token),
    )
