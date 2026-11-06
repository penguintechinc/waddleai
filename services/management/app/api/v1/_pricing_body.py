"""Shared parsing of the pricing fields accepted by vLLM, Ollama and llama.cpp deployment APIs.

One set of rules for every local-deployment kind (AM10): ``price_mode`` 1 (automatic TCO) or 2
(manual); manual prices must be finite decimals greater than zero; automatic mode ignores any
client-supplied price so a caller can never set an automatic price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from shared.fleet.enums import PriceMode

MODE_ERROR = "price_mode must be 1 (automatic) or 2 (manual)"
MANUAL_PRICE_ERROR = (
    "manual pricing needs price_input_per_1m and price_output_per_1m "
    "as decimal numbers greater than zero"
)


class PriceFieldError(ValueError):
    """The request's pricing fields are invalid; the message is safe to return to the caller."""


@dataclass(slots=True, frozen=True)
class PriceFields:
    """Validated pricing request: the mode and (manual only) the two per-1M-token prices."""

    mode: PriceMode
    price_input_per_1m: Decimal | None
    price_output_per_1m: Decimal | None


def _positive_decimal(value: Any) -> Decimal:
    """Parse ``value`` as a finite Decimal > 0, or raise ``PriceFieldError``."""
    if isinstance(value, bool) or value is None:
        raise PriceFieldError(MANUAL_PRICE_ERROR)
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise PriceFieldError(MANUAL_PRICE_ERROR) from None
    if not parsed.is_finite() or parsed <= 0:
        raise PriceFieldError(MANUAL_PRICE_ERROR)
    return parsed


def parse_price_fields(body: dict[str, Any], *, default_mode: PriceMode) -> PriceFields:
    """Validate ``price_mode`` / ``price_*_per_1m`` from a request body.

    ``default_mode`` applies when ``price_mode`` is absent (create defaults to automatic).
    """
    raw = body.get("price_mode", int(default_mode))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in (1, 2):
        raise PriceFieldError(MODE_ERROR)
    mode = PriceMode(raw)
    if mode == PriceMode.AUTO_TCO:
        return PriceFields(mode, None, None)
    return PriceFields(
        mode,
        _positive_decimal(body.get("price_input_per_1m")),
        _positive_decimal(body.get("price_output_per_1m")),
    )


class PricingRefusedError(Exception):
    """Pricing cannot proceed; the message is a plain-language 422 body for the caller."""


def pricing_requested(body: dict[str, Any]) -> bool:
    """True when the body carries any pricing field (existing calls without them are unchanged)."""
    keys = ("price_mode", "price_input_per_1m", "price_output_per_1m", "comparison_model")
    return any(body.get(k) is not None for k in keys)


def _pricing_config(database: Any, org_id: int) -> dict[str, Any]:
    """Pricing config of the org's first vllm_ray backend (TCO inputs and per-class costs)."""
    table = database.fleet_backends
    backend = (
        database((table.org_id == org_id) & (table.type == "vllm_ray"))
        .select(orderby=table.id, limitby=(0, 1))
        .first()
    )
    if backend is None:
        raise PricingRefusedError(
            "No vLLM Ray backend is configured for this organization; "
            "pricing inputs come from its config."
        )
    config: dict[str, Any] = backend.config or {}
    return config


async def price_local_deployment(
    database: Any,
    kind: int,
    org_id: int,
    row: Any,
    fields: PriceFields,
    comparison_model: str | None,
) -> dict[str, Any]:
    """Price an Ollama/llama.cpp deployment (AM10) and return the columns to persist.

    Upserts the per-deployment rate row through ``apply_pricing``; refusals raise
    ``PricingRefusedError`` before anything is persisted on the deployment.
    """
    import asyncio
    from types import SimpleNamespace

    from shared.fleet.pricing import PricingInputsUnavailable, apply_pricing, load_pricing_inputs
    from shared.fleet.vllm_pricing import comparison_rates

    if not comparison_model or not str(comparison_model).strip():
        raise PricingRefusedError("comparison_model is required (provider/model, for savings)")
    baseline = await asyncio.to_thread(comparison_rates, database, str(comparison_model).strip())
    if baseline is None:
        raise PricingRefusedError(
            f"No commercial rate is configured for '{comparison_model}'; "
            "pick a comparison model that has a rate."
        )
    config = await asyncio.to_thread(_pricing_config, database, org_id)
    try:
        inputs, costs = load_pricing_inputs(config)
    except ValueError as exc:
        raise PricingRefusedError(str(exc)) from exc
    view = SimpleNamespace(**{key: row[key] for key in row.keys()})
    view.price_mode = int(fields.mode)
    if fields.mode == PriceMode.MANUAL:
        view.price_input_per_1m = fields.price_input_per_1m
        view.price_output_per_1m = fields.price_output_per_1m
    view.price_breakdown = None
    view.observed_tokens_per_s = None
    try:
        await apply_pricing(database, kind, view, inputs, costs, None)
    except (ValueError, PricingInputsUnavailable) as exc:
        raise PricingRefusedError(str(exc)) from exc
    return {
        "price_mode": int(fields.mode),
        "price_input_per_1m": view.price_input_per_1m,
        "price_output_per_1m": view.price_output_per_1m,
        "price_breakdown": view.price_breakdown,
        "observed_tokens_per_s": view.observed_tokens_per_s,
        "comparison_model": str(comparison_model).strip(),
    }
