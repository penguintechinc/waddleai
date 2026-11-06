"""One TCO pricing formula for every locally hosted deployment kind (AM10).

vLLM, Ollama and llama.cpp deployments all price through ``compute_tco`` (AUTO) or a manual
override, and the resulting virtual price is written to ``token_conversion_rates`` so quota
charging, chargeback and savings use a single path.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import math
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from shared.fleet.enums import LlamaCppTopology, LocalDeploymentKind, PriceMode
from shared.fleet.vllm_ray.fit import Layout
from shared.fleet.vllm_ray.tco import (
    GpuClassCost,
    NodeClassCost,
    TcoInputs,
    TcoResult,
    compute_tco,
)
from shared.quota.virtual_price import (
    DEFAULT_BASE_COST_PER_WADDLEAI_TOKEN,
    rate_model_key,
    virtual_rates,
)

__all__ = [
    "GPU_CLASSES_LABEL",
    "NodeClassLookup",
    "PricingInputsUnavailable",
    "apply_pricing",
    "gpu_set_for",
    "load_pricing_inputs",
    "rate_model_key",
    "virtual_rates",
]

logger = logging.getLogger(__name__)

GPU_CLASSES_LABEL = "waddleai.penguintech.io/gpu-classes"
_PROVIDERS = {
    int(LocalDeploymentKind.VLLM): "vllm",
    int(LocalDeploymentKind.OLLAMA): "ollama",
    int(LocalDeploymentKind.LLAMACPP): "llamacpp",
}
_PRICE_QUANTUM = Decimal("0.000001")  # NUMERIC(12, 6) price columns

# Resolves a deployment's GPU class from its live pod's node label (None: no pod scheduled).
NodeClassLookup = Callable[[Any], str | None]


class PricingInputsUnavailable(Exception):  # noqa: N818 -- name fixed by AM23
    """The deployment's GPU class is not yet knowable (no scheduled pod, no node selector)."""


def _single_class(value: Any) -> str | None:
    """The one class named by a label value; several classes are ambiguous and refused."""
    if not value:
        return None
    classes = [c.strip() for c in str(value).split(",") if c.strip()]
    if len(classes) > 1:
        raise PricingInputsUnavailable(
            "node has multiple GPU classes; set the deployment's gpu_class explicitly"
        )
    return classes[0] if classes else None


def _explicit_count(value: Any) -> int:
    """A deployment's explicit positive GPU count; missing/zero never defaults to 1."""
    try:
        count = int(value) if value is not None else 0
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        raise PricingInputsUnavailable("deployment gpu_count must be an explicit positive number")
    return count


def _resolve_class(row: Any, selector: Any, lookup: NodeClassLookup | None) -> str:
    """AM23: live pod node label, else row node_selector, else PricingInputsUnavailable."""
    cls = _single_class(lookup(row)) if lookup is not None else None
    if cls is None and isinstance(selector, dict):
        cls = _single_class(selector.get(GPU_CLASSES_LABEL))
    if cls is None:
        raise PricingInputsUnavailable("awaiting node placement for pricing")
    return cls


def gpu_set_for(
    kind: int, row: Any, *, node_class_lookup: NodeClassLookup | None = None
) -> tuple[list[tuple[str, int]], NodeClassCost | None]:
    """Return ``[(gpu_class, count)]`` the deployment commits; node cost is resolved later.

    vLLM uses the layout (committed replicas only). Ollama/llama.cpp resolve the class from
    the live pod's node (injected ``node_class_lookup``), falling back to the row's selector.
    A multi-node llama.cpp RPC layout lists the head plus every worker (AM17).
    """
    if kind == LocalDeploymentKind.VLLM:
        total = row.tensor_parallel * row.pipeline_parallel * row.replicas_min
        return [(row.gpu_class, total)], None
    if kind == LocalDeploymentKind.OLLAMA:
        config = getattr(row, "gpu_config", None) or {}
        cls = _resolve_class(row, config.get("node_selector"), node_class_lookup)
        return [(cls, _explicit_count(config.get("gpu_count")))], None
    if kind == LocalDeploymentKind.LLAMACPP:
        cls = _resolve_class(row, getattr(row, "node_selector", None), node_class_lookup)
        entries = [(cls, _explicit_count(getattr(row, "gpu_count", None)))]
        if getattr(row, "topology", None) == LlamaCppTopology.RPC_MULTI_NODE:
            for worker in getattr(row, "rpc_workers", None) or []:
                entries.append((str(worker["gpu_class"]), int(worker["gpu_count"])))
        return entries, None
    raise ValueError(f"unknown deployment kind {kind}")


def _to_decimal(value: Any) -> Decimal:
    """Exact Decimal from a float/int/Decimal column value."""
    return Decimal(str(value))


def _model_lookup_name(kind: int, row: Any) -> str:
    """Name used to find the model's ``model_registry.params_b``."""
    if kind == LocalDeploymentKind.VLLM:
        return str(row.model_ref)
    if kind == LocalDeploymentKind.LLAMACPP:
        return str(row.model_name)
    return str(getattr(row, "model_name", None) or row.name)


def _params_b(db: Any, name: str) -> float | None:
    """Parameter count (billions) from the model registry, or None when unset/unknown."""
    entry = db(db.model_registry.name == name).select().first()
    value = getattr(entry, "params_b", None) if entry is not None else None
    return None if value is None else float(value)


def _llamacpp_nodes(row: Any, gpu_set: list[tuple[str, int]]) -> int:
    """Distinct nodes a llama.cpp deployment spans: 1 single-node, head + worker nodes for RPC.

    Workers may share the head's node (and each other's), so the count comes from node ids, never
    from the number of worker entries. An RPC row with no recorded head node is refused: guessing
    would silently misprice the deployment.
    """
    if getattr(row, "topology", None) != LlamaCppTopology.RPC_MULTI_NODE:
        return len(gpu_set)
    head = getattr(row, "rpc_head_node_uid", None)
    if not head:
        raise PricingInputsUnavailable("RPC deployment has no recorded head node for pricing")
    workers = getattr(row, "rpc_workers", None) or []
    return len({str(head), *(str(w["node_uid"]) for w in workers)})


def _build_layout(kind: int, row: Any, gpu_set: list[tuple[str, int]], per_node: int) -> Layout:
    """Layout for pricing; ``nodes_used`` comes from the layout, never a constant (AM17)."""
    cls = gpu_set[0][0]
    total = sum(count for _c, count in gpu_set)
    if kind == LocalDeploymentKind.VLLM:
        tp, pp, replicas = row.tensor_parallel, row.pipeline_parallel, row.replicas_min
        nodes = math.ceil(total / per_node) if pp == 1 else pp * replicas
        return Layout(1, cls, replicas, tp, pp, 1, nodes, 0, 0, True, "")
    return Layout(1, cls, 1, total, 1, 1, _llamacpp_nodes(row, gpu_set), 0, 0, True, "")


async def apply_pricing(
    db: Any,
    kind: int,
    row: Any,
    inputs: TcoInputs,
    costs: dict[str, GpuClassCost],
    observed_tokens_per_s: float | None,
    *,
    node_class_lookup: NodeClassLookup | None = None,
    base_cost_per_waddleai_token: Decimal = DEFAULT_BASE_COST_PER_WADDLEAI_TOKEN,
) -> TcoResult | None:
    """Price a local deployment (AUTO TCO or MANUAL) and upsert its conversion-rate row.

    Mutates the price fields on ``row`` (the caller persists them) and returns the TCO result,
    or ``None`` for MANUAL. Raises ``PricingInputsUnavailable`` before any write when the GPU
    class is not yet known.
    """
    from shared.quota.virtual_price import upsert_virtual_rate  # noqa: PLC0415 -- C6 patch point

    provider = _PROVIDERS.get(kind)
    if provider is None:
        raise ValueError(f"unknown deployment kind {kind}")
    result: TcoResult | None = None
    if row.price_mode == PriceMode.MANUAL:
        price_in, price_out = (
            _to_decimal(row.price_input_per_1m),
            _to_decimal(row.price_output_per_1m),
        )
    elif row.price_mode == PriceMode.AUTO_TCO:
        gpu_set, _ = gpu_set_for(kind, row, node_class_lookup=node_class_lookup)
        if len({cls for cls, _c in gpu_set}) != 1:
            raise ValueError("AUTO pricing needs one GPU class per deployment")
        cls = gpu_set[0][0]
        cost = costs.get(cls)
        if cost is None:
            raise ValueError(f"no cost config for {cls}")
        params_b = await asyncio.to_thread(_params_b, db, _model_lookup_name(kind, row))
        if params_b is None:
            raise ValueError("set model params_b before AUTO pricing")
        layout = _build_layout(kind, row, gpu_set, cost.node.gpus_per_node)
        result = compute_tco(layout, {cls: cost}, inputs, params_b, observed_tokens_per_s)
        price_in = _to_decimal(round(result.price_input_per_1m, 6)).quantize(_PRICE_QUANTUM)
        price_out = _to_decimal(round(result.price_output_per_1m, 6)).quantize(_PRICE_QUANTUM)
    else:
        raise ValueError(f"unknown price_mode {row.price_mode}")

    # validates (finite, > 0) before anything is written
    virtual_rates(price_in, price_out, base_cost_per_waddleai_token)
    if result is not None:
        row.price_input_per_1m, row.price_output_per_1m = price_in, price_out
        row.price_breakdown = result.breakdown
        # only an observation that actually set the price is recorded (no stale/ignored value)
        row.observed_tokens_per_s = observed_tokens_per_s if result.source == "observed" else None
    await asyncio.to_thread(
        functools.partial(
            upsert_virtual_rate,
            db,
            provider,
            rate_model_key(row),
            price_in,
            price_out,
            base_cost_per_waddleai_token=base_cost_per_waddleai_token,
        )
    )
    logger.info("priced %s deployment %s mode=%s", provider, rate_model_key(row), row.price_mode)
    return result


_TCO_FIELDS = {f.name for f in dataclasses.fields(TcoInputs)}
_NODE_FIELDS = {f.name for f in dataclasses.fields(NodeClassCost)}


def _parse_group(group: dict[str, Any]) -> GpuClassCost:
    """Build one GpuClassCost from a snake_case worker_groups entry, validating its shape."""
    cls = group.get("gpu_class")
    cost = group.get("cost")
    if not cls or not isinstance(cost, dict):
        raise ValueError(f"worker_groups entry needs gpu_class and cost: {group!r}")
    if "est_tokens_per_s" not in cost:
        raise ValueError(f"worker_groups[{cls}].cost.est_tokens_per_s is required")
    node = cost.get("node")
    if not isinstance(node, dict):
        raise ValueError(f"worker_groups[{cls}].cost.node is required")
    unknown = set(node) - _NODE_FIELDS
    if unknown:
        raise ValueError(f"worker_groups[{cls}].cost.node unknown keys: {sorted(unknown)}")
    try:
        node_cost = NodeClassCost(**node)
    except TypeError as exc:
        raise ValueError(f"worker_groups[{cls}].cost.node invalid: {exc}") from exc
    return GpuClassCost(
        gpu_class=str(cls),
        card_usd=cost.get("card_usd"),
        tdp_watts=cost.get("tdp_watts"),
        est_tokens_per_s=float(cost["est_tokens_per_s"]),
        node=node_cost,
    )


def load_pricing_inputs(config: dict[str, Any]) -> tuple[TcoInputs, dict[str, GpuClassCost]]:
    """Parse the snake_case backend config into ``TcoInputs`` and per-class costs (AM9/AM21)."""
    tco = config.get("tco") or {}
    unknown = set(tco) - _TCO_FIELDS
    if unknown:
        raise ValueError(f"unknown tco keys: {sorted(unknown)}")
    costs: dict[str, GpuClassCost] = {}
    for group in config.get("worker_groups") or []:
        parsed = _parse_group(group)
        if parsed.gpu_class in costs:
            raise ValueError(f"duplicate worker_groups gpu_class {parsed.gpu_class}")
        costs[parsed.gpu_class] = parsed
    return TcoInputs(**tco), costs
