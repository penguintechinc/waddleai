"""Fit calculator for vLLM-on-Ray deployments: weight sizing, layout enumeration, refusals.

Decides whether a model fits the schedulable GPU pools and recommends tensor/pipeline
layouts. All sizing is integer megabytes so results are exact and reproducible.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from shared.fleet.enums import GpuVendor, InventoryState, LayoutStrategy, Quantization

CONTEXT_MB = 1536
_MAX_PP = 3
_VENDOR_PREFIX = re.compile(r"^(nvidia|amd|intel)-")
_FP8_PREFIXES: dict[int, tuple[str, ...]] = {
    int(GpuVendor.NVIDIA): (
        "nvidia-rtx40",
        "nvidia-rtx50",
        "nvidia-l4",
        "nvidia-l40",
        "nvidia-h100",
        "nvidia-h200",
        "nvidia-b200",
    ),
    int(GpuVendor.AMD): ("amd-mi300",),
}

QUANT_MATRIX: dict[int, set[int]] = {
    GpuVendor.NVIDIA: {
        Quantization.NONE,
        Quantization.AWQ,
        Quantization.GPTQ,
        Quantization.BNB,
        Quantization.FP8,
    },
    GpuVendor.AMD: {Quantization.NONE, Quantization.AWQ, Quantization.GPTQ, Quantization.FP8},
    GpuVendor.INTEL: {Quantization.NONE},
}
_QUANT_NAMES = {
    int(Quantization.AWQ): "AWQ",
    int(Quantization.GPTQ): "GPTQ",
    int(Quantization.FP8): "FP8",
    int(Quantization.BNB): "BNB",
}
_ALTERNATIVE_ORDER = (int(Quantization.AWQ), int(Quantization.GPTQ), int(Quantization.FP8))

_EMPTY_REFUSAL = "No schedulable GPU pool. Check that GPUs are classified (see inventory)."


_AddFn = Callable[["Layout", int, list[str], bool, tuple[object, ...]], None]


class GpuRecordLike(Protocol):
    """Structural view of an inventory GPU record (the fields pool building reads)."""

    node_uid: str
    vendor: int
    gpu_class: str | None
    vram_total_mb: int
    vram_used_mb: int
    schedulable: bool
    state: int


@dataclass(slots=True)
class Pool:
    """GPUs of one vendor/class; nodes are (node_uid, gpu_count, min free MB per GPU)."""

    vendor: int
    gpu_class: str
    nodes: list[tuple[str, int, int]] = field(default_factory=list)


@dataclass(slots=True)
class Layout:
    """One candidate placement: tensor/pipeline parallel degrees, replicas, and footprint."""

    vendor: int
    gpu_class: str
    replicas: int
    tensor_parallel: int
    pipeline_parallel: int
    strategy: int
    nodes_used: int
    vram_needed_mb: int
    vram_available_mb: int
    recommended: bool
    explanation: str


@dataclass(slots=True)
class FitResult:
    """Outcome of a fit check: fitting layouts (best first) or a human-readable refusal."""

    fits: bool
    layouts: list[Layout] = field(default_factory=list)
    refusal: str | None = None


def class_label(gpu_class: str) -> str:
    """Return a display label for a class slug, e.g. ``nvidia-rtx4090`` -> ``RTX 4090``."""
    name = _VENDOR_PREFIX.sub("", gpu_class)
    match = re.fullmatch(r"rtx(\d+)", name)
    if match:
        return f"RTX {match.group(1)}"
    return name.upper()


def quant_allowed(vendor: int, gpu_class: str, quantization: int) -> bool:
    """Return whether ``quantization`` is supported on this vendor/class (FP8 needs Ada+/MI300)."""
    allowed = QUANT_MATRIX.get(vendor)
    if allowed is None or quantization not in allowed:
        return False
    if quantization == Quantization.FP8:
        return gpu_class.startswith(_FP8_PREFIXES.get(vendor, ()))
    return True


def estimate_weight_bytes(params_b: float, quantization: int, dtype: str) -> int:
    """Estimate model weight bytes for a parameter count (billions) and quantization scheme."""
    if quantization == Quantization.NONE:
        if dtype not in ("bfloat16", "float16"):
            raise ValueError(f"unsupported dtype {dtype!r}; use bfloat16 or float16")
        return int(params_b * 1e9 * 2)
    if quantization == Quantization.FP8:
        return int(params_b * 1e9 * 1)
    if quantization in (Quantization.AWQ, Quantization.GPTQ):
        return int(params_b * 1e9 * 0.5 * 1.1)
    if quantization == Quantization.BNB:
        return int(params_b * 1e9 * 0.5 * 1.15)
    raise ValueError(f"unknown quantization {quantization}")


def _total_mb(params_b: float, quantization: int, dtype: str, kv_headroom_pct: int) -> int:
    """Weights plus KV headroom in whole MB (integer ceil arithmetic, no float drift)."""
    weight_mb = -(-estimate_weight_bytes(params_b, quantization, dtype) // 1048576)
    return -(-weight_mb * (100 + kv_headroom_pct) // 100)


def _per_gpu_mb(total_mb: int, tp: int, pp: int) -> int:
    """Per-GPU requirement for a tp x pp layout, including fixed context overhead."""
    return -(-total_mb // (tp * pp)) + CONTEXT_MB


def build_pools(records: list[GpuRecordLike]) -> list[Pool]:
    """Group schedulable WARM GPUs by (vendor, class) into pools of per-node free VRAM."""
    grouped: dict[tuple[int, str], dict[str, list[int]]] = {}
    for rec in records:
        if not rec.schedulable or rec.state != InventoryState.WARM or rec.gpu_class is None:
            continue
        nodes = grouped.setdefault((int(rec.vendor), rec.gpu_class), {})
        nodes.setdefault(rec.node_uid, []).append(rec.vram_total_mb - rec.vram_used_mb)
    return [
        Pool(vendor, cls, [(uid, len(free), min(free)) for uid, free in nodes.items()])
        for (vendor, cls), nodes in grouped.items()
    ]


def _divisors(n: int, intel: bool) -> list[int]:
    """Tensor-parallel degrees to try for a node with ``n`` GPUs (Intel: single GPU only)."""
    if intel:
        return [1]
    return [d for d in range(1, n + 1) if n % d == 0]


def _rank(layout: Layout, per_gpu: int) -> tuple[int, int, int]:
    """Sort key: fewest nodes, most replicas, smallest per-GPU footprint."""
    return (layout.nodes_used, -layout.replicas, per_gpu)


def _explain(layout: Layout, labels: list[str]) -> str:
    """Render the explanation sentence for a layout (recommended suffix added later)."""
    need = round(layout.vram_needed_mb / 1024)
    avail = round(layout.vram_available_mb / 1024)
    tp, pp = layout.tensor_parallel, layout.pipeline_parallel
    label = labels[0]
    if pp == 1 and layout.replicas == 1:
        return f"Fits on 1 node using {tp}× {label} ({need} GB of {avail} GB)."
    if pp == 1:
        return (
            f"Fits on 1 node using {tp}× {label} per replica, {layout.replicas} replicas "
            f"({need} GB of {avail} GB)."
        )
    distinct = list(dict.fromkeys(labels))
    if len(distinct) > 1:
        return (
            f"Fits across {pp} nodes, mixing {' and '.join(distinct)} stages "
            f"({need} GB of {avail} GB)."
        )
    return f"Fits across {pp} nodes using {tp}× {label} per stage ({need} GB of {avail} GB)."


def _enumerate(
    pools: list[Pool], total_mb: int, allow_mixed_class: bool
) -> list[tuple[tuple[int, int, int], Layout, bool]]:
    """Enumerate fitting layouts as (rank key, layout, is_mixed); duplicates collapsed."""
    found: dict[tuple[object, ...], tuple[tuple[int, int, int], Layout, bool]] = {}

    def add(
        layout: Layout, per_gpu: int, labels: list[str], mixed: bool, ident: tuple[object, ...]
    ) -> None:
        if ident in found:
            return
        layout.explanation = _explain(layout, labels)
        found[ident] = (_rank(layout, per_gpu), layout, mixed)

    for pool in pools:
        intel = pool.vendor == GpuVendor.INTEL
        label = class_label(pool.gpu_class)
        for _uid, count, free in pool.nodes:
            for tp in _divisors(count, intel):
                per_gpu = _per_gpu_mb(total_mb, tp, 1)
                if per_gpu > free:
                    continue
                layout = Layout(
                    vendor=pool.vendor,
                    gpu_class=pool.gpu_class,
                    replicas=count // tp,
                    tensor_parallel=tp,
                    pipeline_parallel=1,
                    strategy=int(LayoutStrategy.STRICT_PACK),
                    nodes_used=1,
                    vram_needed_mb=per_gpu * tp * (count // tp),
                    vram_available_mb=free * count,
                    recommended=False,
                    explanation="",
                )
                add(layout, per_gpu, [label], False, (pool.gpu_class, tp, 1, count // tp, 1))
        if intel:
            continue
        for pp in range(2, min(len(pool.nodes), _MAX_PP) + 1):
            for tp in sorted({d for _u, c, _f in pool.nodes for d in _divisors(c, False)}):
                per_gpu = _per_gpu_mb(total_mb, tp, pp)
                eligible = [n for n in pool.nodes if n[1] >= tp and n[2] >= per_gpu]
                if len(eligible) < pp:
                    continue
                chosen = eligible[:pp]
                layout = Layout(
                    vendor=pool.vendor,
                    gpu_class=pool.gpu_class,
                    replicas=1,
                    tensor_parallel=tp,
                    pipeline_parallel=pp,
                    strategy=int(LayoutStrategy.STRICT_SPREAD),
                    nodes_used=pp,
                    vram_needed_mb=per_gpu * tp * pp,
                    vram_available_mb=sum(tp * n[2] for n in chosen),
                    recommended=False,
                    explanation="",
                )
                add(layout, per_gpu, [label] * pp, False, (pool.gpu_class, tp, pp, 1, pp))
    if allow_mixed_class:
        _enumerate_mixed(pools, total_mb, add)
    return sorted(found.values(), key=lambda item: item[0])


def _enumerate_mixed(pools: list[Pool], total_mb: int, add: _AddFn) -> None:
    """Add pipeline layouts whose stages (one node each) span at least two GPU classes."""
    by_vendor: dict[int, list[tuple[Pool, tuple[str, int, int]]]] = {}
    for pool in pools:
        if pool.vendor == GpuVendor.INTEL:
            continue
        for node in pool.nodes:
            by_vendor.setdefault(pool.vendor, []).append((pool, node))
    for vendor, nodes in by_vendor.items():
        for pp in range(2, min(len(nodes), _MAX_PP) + 1):
            for tp in sorted({d for _p, n in nodes for d in _divisors(n[1], False)}):
                per_gpu = _per_gpu_mb(total_mb, tp, pp)
                for combo in itertools.combinations(nodes, pp):
                    classes = [p.gpu_class for p, _n in combo]
                    if len(set(classes)) < 2:
                        continue
                    if any(n[1] < tp or n[2] < per_gpu for _p, n in combo):
                        continue
                    layout = Layout(
                        vendor=vendor,
                        gpu_class=classes[0],
                        replicas=1,
                        tensor_parallel=tp,
                        pipeline_parallel=pp,
                        strategy=int(LayoutStrategy.STRICT_SPREAD),
                        nodes_used=pp,
                        vram_needed_mb=per_gpu * tp * pp,
                        vram_available_mb=sum(tp * n[2] for _p, n in combo),
                        recommended=False,
                        explanation="",
                    )
                    labels = [class_label(c) for c in classes]
                    add(
                        layout,
                        per_gpu,
                        labels,
                        True,
                        ("mixed", tuple(classes), tp, pp, tuple(n[0] for _p, n in combo)),
                    )


def _join_alternatives(items: list[str]) -> str:
    """Join as ``a, b, or c`` / ``a or b`` / ``a``."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return f"{', '.join(items[:-1])}, or {items[-1]}"


def _quant_refusal(pool: Pool, quantization: int) -> str:
    """Message for a quantization no pool supports, listing what the first pool allows."""
    alts = [
        _QUANT_NAMES[q]
        for q in _ALTERNATIVE_ORDER
        if quant_allowed(pool.vendor, pool.gpu_class, q) and q != quantization
    ]
    alts.append("bf16/fp16")
    name = _QUANT_NAMES.get(quantization, str(quantization))
    label = class_label(pool.gpu_class)
    return f"{name} is not supported on {label}. Choose {_join_alternatives(alts)}."


def fit_check(
    params_b: float,
    quantization: int,
    dtype: str,
    max_model_len: int,
    pools: list[Pool],
    allow_mixed_class: bool,
    kv_headroom_pct: int = 20,
) -> FitResult:
    """Return the fitting layouts (best first) or a refusal explaining what would help.

    ``max_model_len`` is accepted and passed through; it does not change the fit math in
    v1 (``kv_headroom_pct`` is the knob).
    """
    del max_model_len  # v1: KV headroom percentage is the only context-length knob
    if not pools:
        return FitResult(False, [], _EMPTY_REFUSAL)
    usable = [p for p in pools if quant_allowed(p.vendor, p.gpu_class, quantization)]
    if not usable:
        return FitResult(False, [], _quant_refusal(pools[0], quantization))
    total_mb = _total_mb(params_b, quantization, dtype, kv_headroom_pct)
    ranked = _enumerate(usable, total_mb, allow_mixed_class)
    if ranked:
        layouts = [item[1] for item in ranked]
        layouts[0].recommended = True
        layouts[0].explanation += " Recommended."
        return FitResult(True, layouts, None)
    options: list[str] = []
    if quantization == Quantization.NONE:
        awq_pools = [p for p in pools if quant_allowed(p.vendor, p.gpu_class, Quantization.AWQ)]
        awq_total = _total_mb(params_b, int(Quantization.AWQ), "float16", kv_headroom_pct)
        if awq_pools and _enumerate(awq_pools, awq_total, allow_mixed_class):
            options.append(f"use 4-bit quantization (needs {-(-awq_total // 1024)} GB)")
    if not options:
        biggest = max(n[1] for p in usable for n in p.nodes)
        options.append(
            f"add GPUs with at least {-(-_per_gpu_mb(total_mb, biggest, 1) // 1024)} GB VRAM each"
        )
    return FitResult(False, [], "Does not fit in any pool. Options: " + "; ".join(options) + ".")
