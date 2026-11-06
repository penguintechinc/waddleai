"""Virtual-price to conversion-rate bridge for locally hosted models (AM2, AM10).

A locally hosted deployment's price per 1M tokens is charged through the existing
``token_conversion_rates`` table, whose rates are DIVISORS (LLM tokens per WaddleAI credit).
This module converts a price into those divisors and upserts the (provider, model) row, so
``QuotaEngine``'s existing lookup charges the virtual price unchanged.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError

DEFAULT_BASE_COST_PER_WADDLEAI_TOKEN = Decimal("0.001")
_MILLION = Decimal(1_000_000)
_MODEL_KEY = re.compile(
    r"^deployment/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def rate_model_key(row: Any) -> str:
    """Rate-row model key for a deployment: ``deployment/<public_id>`` (AM26), never its name."""
    return f"deployment/{row.public_id}"


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
    """Create or update a deployment's ``token_conversion_rates`` row for this virtual price.

    Synchronous (penguin-dal); async callers wrap it in ``asyncio.to_thread``.
    """
    if not _MODEL_KEY.match(model):
        raise ValueError(f"model key must be deployment/<uuid>, got {model!r}")
    rate_in, rate_out = virtual_rates(
        price_input_per_1m, price_output_per_1m, base_cost_per_waddleai_token
    )
    values: dict[str, Any] = {
        "input_rate": float(rate_in),
        "output_rate": float(rate_out),
        "base_cost_per_waddleai_token": float(base_cost_per_waddleai_token),
        "effective_date": datetime.utcnow(),
    }
    table = db.token_conversion_rates

    def match() -> Any:
        return db((table.provider == provider) & (table.model == model))

    # An existing row keeps its `enabled` flag: an admin-disabled rate must stay disabled.
    if match().update(**values):
        return
    try:
        table.insert(provider=provider, model=model, enabled=True, **values)
    except IntegrityError as exc:  # lost an insert race: update the winner's row, keep its flag
        if not match().update(**values):
            raise RuntimeError(
                f"virtual rate upsert for {provider}/{model} did not converge: "
                "insert collided but no existing row was updated"
            ) from exc
