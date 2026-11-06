"""vLLM fleet pricing helpers shared by the management API and the reconcile loop (C2/C3).

``tco_price`` turns a backend's snake_case pricing config and a placement ``Layout`` into a
``TcoResult``; ``price_deployment`` prices a ``vllm_deployments`` row (AUTO via TCO or MANUAL) and
upserts its per-deployment conversion-rate row through Plan B's ``apply_pricing``;
``comparison_rates`` finds the commercial baseline used for savings only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from decimal import Decimal
from typing import Any

from shared.fleet.enums import LocalDeploymentKind, PriceMode
from shared.fleet.pricing import apply_pricing, load_pricing_inputs
from shared.fleet.throughput import _count_dropped
from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.model_size import parse_params_b
from shared.fleet.vllm_ray.tco import _REFERENCE_PARAMS_B, TcoResult, compute_tco
from shared.routing.providers import LOCAL_PROVIDERS

logger = logging.getLogger(__name__)

# Expected failures of the registry/row lookups (bad/missing row fields or params); anything
# else (DB outage, programming error) must propagate rather than silently drop the bound.
_ESTIMATE_LOOKUP_ERRORS = (ValueError, TypeError, KeyError, AttributeError, ArithmeticError)

OBSERVED_WINDOW_S = 86400
NO_PARAMS_B = "Can't infer model size from the name; enter the parameter count in billions."


def tco_price(
    backend_cfg: dict[str, Any],
    layout: Layout,
    params_b: float,
    observed_tokens_per_s: float | None,
) -> TcoResult:
    """Compute the AUTO price for ``layout``; ``ValueError`` (unknown class, kwh) passes through."""
    inputs, costs = load_pricing_inputs(backend_cfg)
    return compute_tco(layout, costs, inputs, params_b, observed_tokens_per_s)


def layout_from_row(row: Any, backend_cfg: dict[str, Any]) -> Layout:
    """Pricing layout of a deployment row (committed replicas, at least one for pricing)."""
    _inputs, costs = load_pricing_inputs(backend_cfg)
    cost = costs.get(row.gpu_class)
    if cost is None:
        raise ValueError(f"no cost config for {row.gpu_class}")
    replicas = max(1, int(row.replicas_min))
    total = row.tensor_parallel * row.pipeline_parallel * replicas
    per_node = cost.node.gpus_per_node
    nodes = (
        math.ceil(total / per_node)
        if row.pipeline_parallel == 1
        else row.pipeline_parallel * replicas
    )
    return Layout(
        int(row.vendor),
        row.gpu_class,
        replicas,
        int(row.tensor_parallel),
        int(row.pipeline_parallel),
        1,
        nodes,
        0,
        0,
        True,
        "",
    )


def _comparison_row(db: Any, comparison_model: str | None) -> Any | None:
    """The enabled commercial rate row for ``provider/model`` (or the first one when None)."""
    table = db.token_conversion_rates
    if comparison_model:
        provider, _, model = comparison_model.partition("/")
        if not model or provider in LOCAL_PROVIDERS:
            return None
        return (
            db((table.provider == provider) & (table.model == model) & (table.enabled == True))  # noqa: E712
            .select()
            .first()
        )
    rows = db(table.enabled == True).select(orderby=table.id)  # noqa: E712
    return next((r for r in rows if r.provider not in LOCAL_PROVIDERS), None)


def comparison_rates(db: Any, comparison_model: str | None) -> dict[str, Any] | None:
    """Commercial baseline rates (savings only); None when no enabled commercial row matches."""
    row = _comparison_row(db, comparison_model)
    if row is None:
        return None
    return {
        "provider": row.provider,
        "model": row.model,
        "input_rate": float(row.input_rate),
        "output_rate": float(row.output_rate),
    }


def resolve_params_b(db: Any, row: Any) -> float | None:
    """Parameter count for ``row``: recorded with the last price, else the registry, else name."""
    stored = (row.price_breakdown or {}).get("params_b") if row.price_breakdown else None
    if stored:
        return float(stored)
    entry = db(db.model_registry.name == row.model_ref).select().first()
    if entry is not None and entry.params_b is not None:
        return float(entry.params_b)
    return parse_params_b(str(row.model_ref))


@dataclasses.dataclass(slots=True)
class _PricingView:
    """The row fields ``apply_pricing`` reads, with MANUAL pricing already resolved."""

    public_id: Any
    price_mode: int
    price_input_per_1m: Decimal
    price_output_per_1m: Decimal
    tensor_parallel: int
    pipeline_parallel: int
    replicas_min: int
    gpu_class: str
    model_ref: str
    price_breakdown: dict[str, Any] | None = None
    observed_tokens_per_s: float | None = None


@dataclasses.dataclass(slots=True)
class PricedDeployment:
    """Outcome of pricing a deployment: the stored prices and (AUTO only) breakdown/result."""

    price_input_per_1m: Decimal
    price_output_per_1m: Decimal
    price_breakdown: dict[str, Any] | None
    observed_tokens_per_s: float | None
    result: TcoResult | None


async def price_deployment(
    db: Any,
    row: Any,
    backend_cfg: dict[str, Any],
    observed_tokens_per_s: float | None,
    params_b: float | None,
) -> PricedDeployment:
    """Price ``row`` and upsert its rate row; the caller persists the returned values.

    AUTO computes via ``compute_tco`` (which clamps throughput to ``max(estimate, observed)`` so
    observed may only lower the price; so a model missing from the registry still prices from
    the supplied ``params_b``) and the result is written through ``apply_pricing`` as a fixed
    price so the rate-row upsert path is the single shared one. MANUAL prices are validated by
    ``apply_pricing`` (finite, > 0) before anything is written.
    """
    inputs, costs = load_pricing_inputs(backend_cfg)
    result: TcoResult | None = None
    in_price = Decimal(str(row.price_input_per_1m))
    out_price = Decimal(str(row.price_output_per_1m))
    breakdown: dict[str, Any] | None = None
    if row.price_mode == PriceMode.AUTO_TCO:
        if params_b is None:
            raise ValueError(NO_PARAMS_B)
        layout = layout_from_row(row, backend_cfg)
        result = await asyncio.to_thread(
            compute_tco, layout, costs, inputs, params_b, observed_tokens_per_s
        )
        in_price = Decimal(str(round(result.price_input_per_1m, 6)))
        out_price = Decimal(str(round(result.price_output_per_1m, 6)))
        breakdown = {**result.breakdown, "params_b": params_b}
    view = _PricingView(
        row.public_id,
        int(PriceMode.MANUAL),
        in_price,
        out_price,
        row.tensor_parallel,
        row.pipeline_parallel,
        row.replicas_min,
        row.gpu_class,
        row.model_ref,
    )
    await apply_pricing(db, int(LocalDeploymentKind.VLLM), view, inputs, costs, None)
    return PricedDeployment(
        in_price,
        out_price,
        breakdown,
        observed_tokens_per_s if result is not None and result.source == "observed" else None,
        result,
    )


def estimate_aggregate_tokens_per_s(
    db: Any, kind: int, row: Any, costs: dict[str, Any], inputs: Any = None
) -> float | None:
    """Deployment-TOTAL GPU-class throughput estimate (per-replica estimate x priced replicas).

    Same scaling ``compute_tco`` uses for its estimate path; the sanity bound for the observed
    AGGREGATE rate. ``None`` when the GPU class or parameter count cannot be resolved, in which
    case only the absolute bounds apply. Ollama/llama.cpp deployments are one replica.
    """
    from shared.fleet.pricing import _model_lookup_name, _params_b, gpu_set_for

    replicas = 1
    try:
        if kind == int(LocalDeploymentKind.VLLM):
            cls = str(row.gpu_class)
            params_b = resolve_params_b(db, row)
            replicas = max(1, int(row.replicas_min))
        else:
            gpu_set, _ = gpu_set_for(kind, row)
            classes = {c for c, _n in gpu_set}
            if len(classes) != 1:
                return None
            cls = next(iter(classes))
            params_b = _params_b(db, _model_lookup_name(kind, row))
    except _ESTIMATE_LOOKUP_ERRORS:
        # Owner rule: no estimate => the observed rate is IGNORED (never accepted unbounded).
        logger.warning(
            "throughput estimate unavailable for deployment %s; observed rate will be ignored",
            getattr(row, "public_id", None),
            exc_info=True,
        )
        _count_dropped("estimate_unavailable", 1)
        return None
    cost = costs.get(cls)
    if cost is None or not params_b or params_b <= 0:
        return None
    return float(cost.est_tokens_per_s * min(1.0, _REFERENCE_PARAMS_B / params_b) * replicas)
