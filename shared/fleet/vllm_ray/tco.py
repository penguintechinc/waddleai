"""Amortized true-cost (TCO) pricing for locally hosted GPU deployments (spec 4.7, AM6/AM8).

Turns a layout plus hardware cost configuration into an hourly cost and a per-million-token
price, so the default virtual price reflects what the deployment actually costs to run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.gpu_price_reference import GPU_PRICE_REFERENCE

_HOURS_PER_YEAR = 365 * 24
_REFERENCE_PARAMS_B = 12.0


@dataclass(slots=True)
class NodeClassCost:
    """Cost of the host node a GPU class lives in; ``node_base_usd`` overrides the build-up."""

    cpu_usd: float
    ram_gb: int
    nvme_gb: int
    nvme_pro: bool
    network_devices: int
    node_base_watts: int
    gpus_per_node: int
    node_base_usd: float | None = None


@dataclass(slots=True)
class GpuClassCost:
    """Per-class GPU cost; ``None`` card/TDP resolve from the static price reference."""

    gpu_class: str
    card_usd: float | None
    tdp_watts: int | None
    est_tokens_per_s: float
    node: NodeClassCost


@dataclass(slots=True)
class TcoInputs:
    """Operator-tunable TCO assumptions (electricity, lifecycle, utilization, rates)."""

    kwh_usd: float = 0.18
    lifecycle_years: int = 10
    utilization_pct: int = 50
    input_ratio: float = 0.2
    ram_usd_per_gb: float = 1.0
    nvme_usd_per_gb: float = 1.0
    nvme_pro_usd_per_gb: float = 2.0
    network_device_usd: float = 400.0


@dataclass(slots=True)
class TcoResult:
    """Computed hourly cost, per-1M-token prices, and the terms that produced them."""

    hourly_cost_usd: float
    gpu_hourly_usd: float
    node_hourly_usd: float
    node_base_usd: float
    tokens_per_s_used: float
    source: str  # "observed" | "estimate"
    price_input_per_1m: float
    price_output_per_1m: float
    breakdown: dict[str, Any] = field(default_factory=dict)


def node_base_usd(node: NodeClassCost, inputs: TcoInputs) -> float:
    """Return the node purchase cost: the explicit override, else cpu + ram + nvme + devices."""
    if node.node_base_usd is not None:
        return node.node_base_usd
    nvme_rate = inputs.nvme_pro_usd_per_gb if node.nvme_pro else inputs.nvme_usd_per_gb
    return (
        node.cpu_usd
        + node.ram_gb * inputs.ram_usd_per_gb
        + node.nvme_gb * nvme_rate
        + node.network_devices * inputs.network_device_usd
    )


def _resolve_gpu_values(cost: GpuClassCost) -> tuple[float, int, str]:
    """Resolve card price and TDP, falling back to the reference; report the source."""
    card, tdp = cost.card_usd, cost.tdp_watts
    if card is not None and tdp is not None:
        return card, tdp, "configured"
    ref = GPU_PRICE_REFERENCE.get(cost.gpu_class)
    if ref is None:
        raise ValueError(
            f"no price reference for {cost.gpu_class}; set cost.cardUsd and cost.tdpWatts"
        )
    return (
        float(ref.avg_street_usd) if card is None else card,
        ref.tdp_watts if tdp is None else tdp,
        "reference",
    )


def compute_tco(
    layout: Layout,
    costs: dict[str, GpuClassCost],
    inputs: TcoInputs,
    params_b: float,
    observed_tokens_per_s: float | None,
) -> TcoResult:
    """Compute hourly cost and per-1M-token prices for ``layout`` (spec 4.7 formulas).

    Throughput is ``max(estimate_total, observed)``: the per-replica class estimate scaled by
    model size and replica count is a floor, and an observed output tokens/s (a deployment
    TOTAL) is used only when it exceeds it, so observation can only lower the price.
    """
    if inputs.kwh_usd <= 0:
        raise ValueError("kwh_usd must be set")
    if not 0 < inputs.utilization_pct <= 100:
        raise ValueError("utilization_pct must be within 1..100")
    cost = costs.get(layout.gpu_class)
    if cost is None:
        raise ValueError(f"no cost config for {layout.gpu_class}")
    if cost.node.gpus_per_node <= 0:
        raise ValueError("gpus_per_node must be positive")
    card_usd, tdp_watts, price_source = _resolve_gpu_values(cost)

    lifecycle_hours = inputs.lifecycle_years * _HOURS_PER_YEAR
    total_gpus = layout.tensor_parallel * layout.pipeline_parallel * layout.replicas
    gpus_used_on_node = total_gpus / layout.nodes_used
    card_hourly = total_gpus * card_usd / lifecycle_hours
    power_hourly = total_gpus * tdp_watts / 1000 * inputs.kwh_usd
    gpu_hourly = card_hourly + power_hourly

    base_usd = node_base_usd(cost.node, inputs)
    node_share = layout.nodes_used * (gpus_used_on_node / cost.node.gpus_per_node)
    node_amortization = node_share * base_usd / lifecycle_hours
    node_power = node_share * cost.node.node_base_watts / 1000 * inputs.kwh_usd
    node_hourly = node_amortization + node_power
    hourly = gpu_hourly + node_hourly

    if params_b <= 0:
        raise ValueError("params_b must be positive")
    # est_tokens_per_s is PER REPLICA; the cost above is for all replicas, so scale throughput
    # to the deployment total. Observed usage is already a deployment total.
    per_replica = cost.est_tokens_per_s * min(1.0, _REFERENCE_PARAMS_B / params_b)
    tokens_per_s, source = per_replica * layout.replicas, "estimate"
    # Owner rule: throughput = max(estimate_total, observed) -- observed may only LOWER price.
    if observed_tokens_per_s is not None and observed_tokens_per_s > tokens_per_s:
        tokens_per_s, source = observed_tokens_per_s, "observed"
    if tokens_per_s <= 0:
        raise ValueError("tokens_per_s must be positive")

    output_per_1m = hourly / (tokens_per_s * 3600 * inputs.utilization_pct / 100) * 1e6
    return TcoResult(
        hourly_cost_usd=hourly,
        gpu_hourly_usd=gpu_hourly,
        node_hourly_usd=node_hourly,
        node_base_usd=base_usd,
        tokens_per_s_used=tokens_per_s,
        source=source,
        price_input_per_1m=output_per_1m * inputs.input_ratio,
        price_output_per_1m=output_per_1m,
        breakdown={
            "price_source": price_source,
            "gpu_class": layout.gpu_class,
            "card_usd": card_usd,
            "tdp_watts": tdp_watts,
            "total_gpus": total_gpus,
            "nodes_used": layout.nodes_used,
            "card_amortization_hourly_usd": card_hourly,
            "gpu_power_hourly_usd": power_hourly,
            "node_share": node_share,
            "node_amortization_hourly_usd": node_amortization,
            "node_power_hourly_usd": node_power,
            "tokens_per_s_used": tokens_per_s,
            "throughput_source": source,
            "inputs": {
                "kwh_usd": inputs.kwh_usd,
                "lifecycle_years": inputs.lifecycle_years,
                "utilization_pct": inputs.utilization_pct,
                "input_ratio": inputs.input_ratio,
            },
        },
    )
