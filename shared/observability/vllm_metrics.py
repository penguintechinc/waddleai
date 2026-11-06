"""OTel instruments for locally hosted (vLLM/Ollama/llama.cpp) fleet metering and inventory.

Recorders never raise: a dead exporter must not turn into a request or reconcile failure.
Gauges are observable over an atomically replaced snapshot, so a node or status that vanishes
stops being reported instead of lingering at its last value.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from shared.fleet.enums import DeploymentStatus, GpuVendor
from shared.observability.metrics import get_meter

__all__ = [
    "PLACEMENT_OUTCOMES",
    "local_rate_missing_counter",
    "record_deployment_counts",
    "record_inventory",
    "record_placement",
    "reset_for_testing",
    "usage_missing_counter",
]

logger = logging.getLogger(__name__)

PLACEMENT_OUTCOMES = frozenset(
    {"placed", "denied_origin", "denied_quantization", "denied_cap", "no_fit"}
)
_VENDOR_NAMES = {
    int(GpuVendor.NVIDIA): "nvidia",
    int(GpuVendor.AMD): "amd",
    int(GpuVendor.INTEL): "intel",
}

_usage_missing: Any = None
_local_rate_missing: Any = None
_placement: Any = None
_gauges_ready = False
_vram_total: dict[tuple[str, str, str], int] = {}
_vram_used: dict[tuple[str, str, str], int] = {}
_deployment_counts: dict[int, int] = {}


def usage_missing_counter() -> Any:
    """Counter ``waddleai_vllm_usage_missing_total``: usage the provider did not report."""
    global _usage_missing
    if _usage_missing is None:
        _usage_missing = get_meter().create_counter(
            "waddleai_vllm_usage_missing_total",
            unit="1",
            description="Local-engine responses with missing/invalid usage; counts were estimated",
        )
    return _usage_missing


def local_rate_missing_counter() -> Any:
    """Counter ``waddleai_local_rate_missing_total``: requests refused for want of a local rate."""
    global _local_rate_missing
    if _local_rate_missing is None:
        _local_rate_missing = get_meter().create_counter(
            "waddleai_local_rate_missing_total",
            unit="1",
            description="Local-deployment requests refused: no virtual rate row (fail closed)",
        )
    return _local_rate_missing


def _observe(snapshot: dict[tuple[str, str, str], int]) -> Any:
    """Build an observable-gauge callback over a (vendor, gpu_class, node) -> MB snapshot."""
    from opentelemetry.metrics import Observation

    def callback(_options: Any) -> Iterable[Any]:
        return [
            Observation(value, {"vendor": v, "gpu_class": c, "node": n})
            for (v, c, n), value in dict(snapshot).items()
        ]

    return callback


def _observe_status(_options: Any) -> Iterable[Any]:
    """Observations for ``waddleai_vllm_deployments{status}``: every status, zero when absent."""
    from opentelemetry.metrics import Observation

    return [
        Observation(_deployment_counts.get(int(s), 0), {"status": s.name.lower()})
        for s in DeploymentStatus
    ]


def _ensure_gauges() -> None:
    """Create the observable gauges once per meter provider (idempotent)."""
    global _gauges_ready
    if _gauges_ready:
        return
    meter = get_meter()
    meter.create_observable_gauge(
        "waddleai_gpu_vram_total_mb",
        callbacks=[_observe(_vram_total)],
        unit="MB",
        description="Total GPU VRAM by vendor, class and node",
    )
    meter.create_observable_gauge(
        "waddleai_gpu_vram_used_mb",
        callbacks=[_observe(_vram_used)],
        unit="MB",
        description="Used GPU VRAM by vendor, class and node",
    )
    meter.create_observable_gauge(
        "waddleai_vllm_deployments",
        callbacks=[_observe_status],
        unit="1",
        description="vLLM deployments by status",
    )
    _gauges_ready = True


def record_inventory(records: list[Any]) -> None:
    """Replace the VRAM gauges' snapshot from ``GpuRecord`` s, summed per label set."""
    try:
        _ensure_gauges()
        total: dict[tuple[str, str, str], int] = {}
        used: dict[tuple[str, str, str], int] = {}
        for rec in records:
            key = (
                _VENDOR_NAMES.get(int(rec.vendor), "unknown"),
                rec.gpu_class or "unknown",
                rec.node_name,
            )
            total[key] = total.get(key, 0) + int(rec.vram_total_mb)
            used[key] = used.get(key, 0) + int(rec.vram_used_mb)
        _vram_total.clear()
        _vram_total.update(total)
        _vram_used.clear()
        _vram_used.update(used)
    except Exception:  # noqa: BLE001 -- a dead exporter never breaks a reconcile
        logger.warning("could not record GPU inventory gauges", exc_info=True)


def record_deployment_counts(counts: dict[int, int]) -> None:
    """Replace the per-status deployment counts (statuses absent from ``counts`` report 0)."""
    try:
        _ensure_gauges()
        _deployment_counts.clear()
        _deployment_counts.update({int(k): int(v) for k, v in counts.items()})
    except Exception:  # noqa: BLE001
        logger.warning("could not record deployment-count gauge", exc_info=True)


def record_placement(outcome: str) -> None:
    """Count one placement decision; an unknown outcome is a programming error (``ValueError``)."""
    global _placement
    if outcome not in PLACEMENT_OUTCOMES:
        raise ValueError(
            f"unknown placement outcome {outcome!r}; expected {sorted(PLACEMENT_OUTCOMES)}"
        )
    try:
        if _placement is None:
            _placement = get_meter().create_counter(
                "waddleai_vllm_placement_decisions_total",
                unit="1",
                description="vLLM placement decisions by outcome",
            )
        _placement.add(1, {"outcome": outcome})
    except Exception:  # noqa: BLE001
        logger.warning("could not record placement counter", exc_info=True)


def reset_for_testing() -> None:
    """Drop cached instruments/snapshots so a test can install its own meter provider."""
    global _usage_missing, _local_rate_missing, _placement, _gauges_ready
    _usage_missing = None
    _local_rate_missing = None
    _placement = None
    _gauges_ready = False
    _vram_total.clear()
    _vram_used.clear()
    _deployment_counts.clear()
