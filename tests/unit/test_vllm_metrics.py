"""Fleet metrics: inventory/deployment gauges, placement counter, never-raise recorders."""

from __future__ import annotations

import pytest

from shared.fleet.enums import DeploymentStatus, GpuVendor, InventoryState
from shared.fleet.vllm_ray import backend as backend_mod
from shared.fleet.vllm_ray.inventory import GpuRecord
from shared.observability import vllm_metrics
from tests.unit.fixtures.otel import points


def _gpu(
    node: str, cls: str, total: int, used: int, vendor: GpuVendor = GpuVendor.NVIDIA, index: int = 0
) -> GpuRecord:
    """A GpuRecord with the fields the gauges read."""
    return GpuRecord(
        node_uid=node,
        node_name=node,
        gpu_index=index,
        vendor=int(vendor),
        gpu_class=cls,
        product_name=cls,
        vram_total_mb=total,
        vram_used_mb=used,
        pci_bus_id="0",
        schedulable=True,
        state=int(InventoryState.WARM),
    )


def _by_labels(pts: list, *keys: str) -> dict[tuple, float]:
    """Data points keyed by the named attribute values."""
    return {tuple(p.attributes[k] for k in keys): p.value for p in pts}


def test_inventory_gauges_sum_per_vendor_class_node(metric_reader) -> None:
    """VRAM sums per (vendor, class, node); a different vendor/node is its own point."""
    vllm_metrics.record_inventory(
        [
            _gpu("n1", "nvidia-a100-80g", 81920, 1000, index=0),
            _gpu("n1", "nvidia-a100-80g", 81920, 2000, index=1),
            _gpu("n2", "amd-mi300x", 196608, 500, vendor=GpuVendor.AMD),
        ]
    )
    total = _by_labels(
        points(metric_reader, "waddleai_gpu_vram_total_mb"), "vendor", "gpu_class", "node"
    )
    used = _by_labels(
        points(metric_reader, "waddleai_gpu_vram_used_mb"), "vendor", "gpu_class", "node"
    )
    assert total == {
        ("nvidia", "nvidia-a100-80g", "n1"): 163840,
        ("amd", "amd-mi300x", "n2"): 196608,
    }
    assert used == {("nvidia", "nvidia-a100-80g", "n1"): 3000, ("amd", "amd-mi300x", "n2"): 500}


def test_inventory_snapshot_replaces_so_vanished_node_stops_reporting(metric_reader) -> None:
    """A node missing from the next refresh disappears from the gauge (no stale value)."""
    vllm_metrics.record_inventory([_gpu("n1", "c", 10, 1), _gpu("n2", "c", 20, 2)])
    vllm_metrics.record_inventory([_gpu("n2", "c", 20, 2)])
    total = _by_labels(
        points(metric_reader, "waddleai_gpu_vram_total_mb"), "vendor", "gpu_class", "node"
    )
    assert total == {("nvidia", "c", "n2"): 20}


def test_vllm_deployments_gauge_labelled_by_status_zero_for_absent(metric_reader) -> None:
    """waddleai_vllm_deployments{status}: exact counts, every status present, 0 when absent."""
    vllm_metrics.record_deployment_counts(
        {int(DeploymentStatus.SERVING): 3, int(DeploymentStatus.FAILED): 1}
    )
    got = _by_labels(points(metric_reader, "waddleai_vllm_deployments"), "status")
    assert got[("serving",)] == 3 and got[("failed",)] == 1
    assert got[("pending",)] == 0 and got[("degraded",)] == 0
    assert len(got) == len(DeploymentStatus)


def test_placement_counter_outcomes_and_unknown_raises(metric_reader) -> None:
    """Each known outcome counts once; an unknown outcome is a ValueError."""
    for outcome in ("placed", "placed", "denied_origin", "no_fit"):
        vllm_metrics.record_placement(outcome)
    got = _by_labels(points(metric_reader, "waddleai_vllm_placement_decisions_total"), "outcome")
    assert got == {("placed",): 2, ("denied_origin",): 1, ("no_fit",): 1}
    with pytest.raises(ValueError, match="unknown placement outcome"):
        vllm_metrics.record_placement("bogus")


def test_model_load_histogram_one_point_sum_12_5(metric_reader, monkeypatch) -> None:
    """The backend's load-time observation lands as one histogram point with the value."""
    monkeypatch.setattr(backend_mod, "_load_histogram", None)
    backend_mod._observe_model_load_seconds(12.5)
    (pt,) = points(metric_reader, "waddleai_vllm_model_load_seconds")
    assert pt.count == 1 and pt.sum == 12.5


def test_recorders_never_raise_when_meter_broken(monkeypatch) -> None:
    """A dead meter/exporter never breaks a reconcile (telemetry failure != request failure)."""

    def boom() -> None:
        raise RuntimeError("exporter down")

    vllm_metrics.reset_for_testing()
    monkeypatch.setattr(vllm_metrics, "get_meter", boom)
    vllm_metrics.record_inventory([_gpu("n", "c", 1, 1)])
    vllm_metrics.record_deployment_counts({5: 1})
    vllm_metrics.record_placement("placed")
    vllm_metrics.reset_for_testing()
