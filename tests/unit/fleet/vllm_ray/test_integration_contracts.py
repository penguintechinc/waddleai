"""Cross-module contracts between vllm_ray inventory, fit, and the GPU price reference."""

import json
from pathlib import Path

import pytest

from shared.fleet.enums import GpuVendor, InventoryState
from shared.fleet.vllm_ray.fit import GpuRecordLike, build_pools
from shared.fleet.vllm_ray.gpu_price_reference import GPU_PRICE_REFERENCE
from shared.fleet.vllm_ray.inventory import GpuRecord, normalize_class, reference_slugs

_REF_DIR = Path(__file__).resolve().parents[4] / "docs" / "reference"
_VENDORS = {"amd": GpuVendor.AMD, "intel": GpuVendor.INTEL}


def _rows(vendor: str) -> list[dict[str, object]]:
    """Load the reference rows (product model + class slug) for one vendor."""
    rows: list[dict[str, object]] = json.loads(
        (_REF_DIR / f"gpu-price-reference-{vendor}.json").read_text()
    )["gpus"]
    return rows


def _rec(uid: str, **kw: object) -> GpuRecord:
    """Build a real inventory GpuRecord with overridable fields."""
    base: dict[str, object] = {
        "node_uid": uid,
        "node_name": uid,
        "gpu_index": 0,
        "vendor": int(GpuVendor.NVIDIA),
        "gpu_class": "nvidia-rtx4090",
        "product_name": "NVIDIA GeForce RTX 4090",
        "vram_total_mb": 24576,
        "vram_used_mb": 1024,
        "pci_bus_id": "0000:01:00.0",
        "schedulable": True,
        "state": int(InventoryState.WARM),
    }
    base.update(kw)
    return GpuRecord(**base)  # type: ignore[arg-type]  # dict[str, object] kwargs


def test_build_pools_accepts_real_gpu_records() -> None:
    """build_pools consumes inventory.GpuRecord objects (GpuRecordLike is satisfied)."""
    records = [
        _rec("n1"),
        _rec("n1", gpu_index=1),
        _rec("n2"),
        _rec("n3", state=int(InventoryState.UNCLASSIFIED), schedulable=False, gpu_class=None),
    ]
    typed: list[GpuRecordLike] = list(records)  # mypy proves the Protocol fit
    pools = build_pools(typed)
    assert len(pools) == 1
    pool = pools[0]
    assert pool.gpu_class == "nvidia-rtx4090"
    assert sorted((uid, n, free) for uid, n, free in pool.nodes) == [
        ("n1", 2, 23552),
        ("n2", 1, 23552),
    ]


def test_all_reference_json_slugs_are_python_reference_keys() -> None:
    """The JSON reference (inventory source) and the generated module agree on slugs."""
    assert reference_slugs() == frozenset(GPU_PRICE_REFERENCE)


@pytest.mark.parametrize("vendor", ["amd", "intel"])
def test_derived_slugs_are_price_reference_keys(vendor: str) -> None:
    """Every AMD/Intel slug normalize_class emits for a reference product is a key."""
    rows = _rows(vendor)
    assert rows, "zero reference rows examined"
    bad: list[tuple[str, str | None]] = []
    for row in rows:
        vram_mb = int(str(row["vram_gb"])) * 1024
        slug = normalize_class(_VENDORS[vendor], str(row["model"]), {}, vram_mb)
        if slug != row["class"] or slug not in GPU_PRICE_REFERENCE:
            bad.append((str(row["model"]), slug))
    assert not bad
