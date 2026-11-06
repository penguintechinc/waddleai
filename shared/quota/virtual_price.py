"""Virtual-price to conversion-rate bridge for locally hosted models (AM2, AM10).

A locally hosted deployment's price per 1M tokens is charged through the existing
``token_conversion_rates`` table, whose rates are DIVISORS (LLM tokens per WaddleAI credit).
This module converts a price into those divisors and upserts the (provider, model) row, so
``QuotaEngine``'s existing lookup charges the virtual price unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


# LocalDeploymentKind value -> rate-row provider and deployment table (AM10).
LOCAL_RATE_PROVIDERS: dict[int, str] = {1: "vllm", 2: "ollama", 3: "llamacpp"}
LOCAL_DEPLOYMENT_TABLES: dict[int, str] = {
    1: "vllm_deployments",
    2: "ollama_deployments",
    3: "llamacpp_deployments",
}
_PRICE_QUANTUM = Decimal("0.000001")  # NUMERIC(12, 6) price columns


class LocalRateMissingError(Exception):
    """A local deployment has no usable virtual rate; the request is refused, never defaulted."""


@dataclass(slots=True, frozen=True)
class LocalDeployment:
    """A resolved local deployment: its rate-row coordinates and the price snapshot in force."""

    kind: int
    deployment_id: int
    provider: str
    model_key: str
    price_input: Decimal
    price_output: Decimal


def snapshot_price(value: Any) -> Decimal | None:
    """A unit price quantized to the column's 6 places (so snapshot equality is exact)."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(_PRICE_QUANTUM)


def usage_row_key(
    kind: int | None, deployment_id: int | None, price_in: Any, price_out: Any
) -> tuple[int | None, int | None, Decimal | None, Decimal | None]:
    """Key identifying one ``token_usage`` rollup row: (kind, deployment, input, output price).

    A price or deployment change yields a different key, so usage after the change opens a new
    row and earlier rows keep the snapshot they were charged at (Plan C review focus 5).
    """
    return (kind, deployment_id, snapshot_price(price_in), snapshot_price(price_out))


def resolve_local_deployment(
    db: Any, kind: int, deployment_id: int | None
) -> LocalDeployment | None:
    """Look up the deployment row behind a usage row; ``None`` when it cannot be resolved."""
    table_name = LOCAL_DEPLOYMENT_TABLES.get(kind)
    if table_name is None or deployment_id is None or not hasattr(db, table_name):
        return None
    table = getattr(db, table_name)
    row = db(table.id == deployment_id).select().first()
    if row is None or getattr(row, "public_id", None) is None:
        return None
    price_in = snapshot_price(getattr(row, "price_input_per_1m", None))
    price_out = snapshot_price(getattr(row, "price_output_per_1m", None))
    if price_in is None or price_out is None:
        return None
    return LocalDeployment(
        kind=kind,
        deployment_id=deployment_id,
        provider=LOCAL_RATE_PROVIDERS[kind],
        model_key=rate_model_key(row),
        price_input=price_in,
        price_output=price_out,
    )
