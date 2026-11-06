"""Tests for shared.fleet.vllm_ray.inventory (B3): parsers, class slugs, collector, upsert."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydal import DAL, Field

from shared.fleet.enums import GpuVendor, InventoryState
from shared.fleet.vllm_ray.inventory import (
    GpuRecord,
    InventoryCollector,
    normalize_class,
    parse_nvidia_smi,
    parse_rocm_smi,
    parse_xpu_smi,
    upsert_inventory,
)

FIX = Path(__file__).parent / "fixtures"
REF = Path(__file__).resolve().parents[4] / "docs" / "reference"
CLASS_MAP = {"*RTX 4090*": "nvidia-rtx4090", "*RTX 4070*": "nvidia-rtx4070"}


def _fix(name: str) -> str:
    return (FIX / name).read_text()


def _ref_rows(vendor: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(
        (REF / f"gpu-price-reference-{vendor}.json").read_text()
    )["gpus"]
    return rows


class _StubInventoryClient:
    """Minimal stand-in for RayServeClient exposing only run_inventory (B6 owns the real fake)."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads

    async def run_inventory(self) -> list[dict[str, Any]]:
        return self._payloads


def test_parse_nvidia_smi_classifies_matched_models() -> None:
    """Parse nvidia smi classifies matched models."""
    recs = parse_nvidia_smi(_fix("nvidia_smi_4090_4070.csv"), CLASS_MAP)
    assert [r.gpu_class for r in recs] == [
        "nvidia-rtx4090",
        "nvidia-rtx4090",
        "nvidia-rtx4070",
        None,
    ]
    assert recs[0].vram_total_mb == 24564
    assert recs[0].vram_used_mb == 512
    assert recs[0].pci_bus_id == "00000000:01:00.0"
    assert recs[0].vendor == GpuVendor.NVIDIA
    assert recs[0].node_uid == "" and recs[0].node_name == ""


def test_unmatched_nvidia_is_unclassified_and_unschedulable() -> None:
    """Unmatched nvidia is unclassified and unschedulable."""
    rec = parse_nvidia_smi(_fix("nvidia_smi_4090_4070.csv"), CLASS_MAP)[3]
    assert rec.state == InventoryState.UNCLASSIFIED
    assert rec.schedulable is False
    assert rec.gpu_class is None
    assert rec.product_name == "NVIDIA RTX A9000"


def test_classified_nvidia_is_warm_and_schedulable() -> None:
    """Classified nvidia is warm and schedulable."""
    rec = parse_nvidia_smi(_fix("nvidia_smi_4090_4070.csv"), CLASS_MAP)[0]
    assert rec.state == InventoryState.WARM
    assert rec.schedulable is True


def test_parse_nvidia_smi_rejects_malformed_row() -> None:
    """Parse nvidia smi rejects malformed row."""
    with pytest.raises(ValueError, match="5 fields"):
        parse_nvidia_smi("0, NVIDIA RTX 4090, 24564\n", CLASS_MAP)


def test_parse_nvidia_smi_empty_output_returns_empty_list() -> None:
    """Parse nvidia smi empty output returns empty list."""
    assert parse_nvidia_smi("", CLASS_MAP) == []


def test_degraded_sentinel_all_zero_payload_is_cold() -> None:
    """Degraded sentinel all zero payload is cold."""
    rec = parse_nvidia_smi("0, NVIDIA GeForce RTX 4090, 0, 0, 00000000:01:00.0", CLASS_MAP)[0]
    assert rec.state == InventoryState.COLD
    assert rec.schedulable is False


def test_degraded_sentinel_applies_to_rocm_and_xpu() -> None:
    """Degraded sentinel applies to rocm and xpu."""
    rocm = json.dumps(
        {
            "card0": {
                "VRAM Total Memory (B)": "0",
                "VRAM Total Used Memory (B)": "0",
                "Card Series": "AMD Instinct MI300X",
                "PCI Bus": "0000:05:00.0",
            }
        }
    )
    xpu = json.dumps(
        {
            "device_list": [
                {
                    "device_id": 0,
                    "device_name": "Intel(R) Data Center GPU Max 1100",
                    "memory_physical_size_byte": 0,
                    "pci_bdf_address": "0000:03:00.0",
                }
            ]
        }
    )
    for rec in (parse_rocm_smi(rocm)[0], parse_xpu_smi(xpu)[0]):
        assert rec.state == InventoryState.COLD and rec.schedulable is False


def test_parse_rocm_smi_converts_bytes_to_mb_and_normalizes_class() -> None:
    """Parse rocm smi converts bytes to mb and normalizes class."""
    recs = parse_rocm_smi(_fix("rocm_smi_mi300x.json"))
    assert [r.gpu_index for r in recs] == [0, 1]
    assert recs[0].vram_total_mb == 196608
    assert recs[0].vram_used_mb == 1024
    assert recs[0].gpu_class == "amd-instinct-mi300x"
    assert recs[0].vendor == GpuVendor.AMD
    assert recs[0].state == InventoryState.WARM and recs[0].schedulable is True


def test_parse_xpu_smi_reports_single_gpu_classes_and_zero_used() -> None:
    """Parse xpu smi reports single gpu classes and zero used."""
    recs = parse_xpu_smi(_fix("xpu_smi_max1100.json"))
    assert len(recs) == 2
    assert all(r.vram_used_mb == 0 for r in recs)
    assert all(r.vendor == GpuVendor.INTEL for r in recs)
    assert all(r.gpu_class == "intel-max-1100" for r in recs)
    assert recs[0].vram_total_mb == 49152


def test_mixed_amd_node_is_mixed_unsupported() -> None:
    """Mixed amd node is mixed unsupported."""
    rocm = json.dumps(
        {
            "card0": {
                "VRAM Total Memory (B)": "206158430208",
                "VRAM Total Used Memory (B)": "0",
                "Card Series": "AMD Instinct MI300X",
                "PCI Bus": "0000:05:00.0",
            },
            "card1": {
                "VRAM Total Memory (B)": "137438953472",
                "VRAM Total Used Memory (B)": "0",
                "Card Series": "AMD Instinct MI250X",
                "PCI Bus": "0000:06:00.0",
            },
        }
    )
    recs = InventoryCollector._classify_node(parse_rocm_smi(rocm))
    assert {r.state for r in recs} == {InventoryState.MIXED_UNSUPPORTED}
    assert not any(r.schedulable for r in recs)


def test_mixed_intel_node_is_mixed_unsupported_and_homogeneous_untouched() -> None:
    """Mixed intel node is mixed unsupported and homogeneous untouched."""
    recs = parse_xpu_smi(_fix("xpu_smi_max1100.json"))
    assert {r.state for r in InventoryCollector._classify_node(recs)} == {InventoryState.WARM}
    recs[1].product_name = "Intel(R) Data Center GPU Max 1550"
    out = InventoryCollector._classify_node(recs)
    assert {r.state for r in out} == {InventoryState.MIXED_UNSUPPORTED}


def test_nvidia_mixed_classes_are_not_flagged_mixed() -> None:
    """Nvidia mixed classes are not flagged mixed."""
    recs = parse_nvidia_smi(_fix("nvidia_smi_4090_4070.csv"), CLASS_MAP)
    out = InventoryCollector._classify_node(recs)
    assert InventoryState.MIXED_UNSUPPORTED not in {r.state for r in out}


def test_normalize_class_amd_intel_emit_reference_slugs() -> None:
    """Normalize class amd intel emit reference slugs."""
    assert normalize_class(GpuVendor.AMD, "AMD Instinct MI300X", {}) == "amd-instinct-mi300x"
    assert (
        normalize_class(GpuVendor.INTEL, "Intel(R) Data Center GPU Max 1100", {})
        == "intel-max-1100"
    )


def test_normalize_class_nvidia_unmatched_returns_none() -> None:
    """Normalize class nvidia unmatched returns none."""
    assert normalize_class(GpuVendor.NVIDIA, "NVIDIA RTX A9000", CLASS_MAP) is None


def test_normalize_class_ambiguous_vram_variant_needs_vram() -> None:
    """Normalize class ambiguous vram variant needs vram."""
    assert (
        normalize_class(GpuVendor.INTEL, "Intel(R) Arc(TM) A770 Graphics", {}, vram_total_mb=16256)
        == "intel-arc-a770-16gb"
    )
    assert (
        normalize_class(GpuVendor.INTEL, "Intel(R) Arc(TM) A770 Graphics", {}, vram_total_mb=8128)
        == "intel-arc-a770-8gb"
    )
    assert normalize_class(GpuVendor.INTEL, "Intel(R) Arc(TM) A770 Graphics", {}) is None


@pytest.mark.parametrize("vendor,vendor_id", [("amd", GpuVendor.AMD), ("intel", GpuVendor.INTEL)])
def test_normalize_class_covers_every_reference_slug(vendor: str, vendor_id: GpuVendor) -> None:
    """Normalize class covers every reference slug."""
    rows = _ref_rows(vendor)
    assert len(rows) >= 19  # denominator: zero rows examined must not pass
    prefix = "AMD " if vendor == "amd" else "Intel(R) "
    for row in rows:
        names = [prefix + row["model"]]
        if vendor == "intel":
            names.append(prefix + row["model"].replace("Arc ", "Arc(TM) ") + " Graphics")
        else:
            names.append(
                prefix + "Radeon " + row["model"] if row["model"].startswith("RX") else names[0]
            )
        for name in names:
            got = normalize_class(vendor_id, name, {}, vram_total_mb=row["vram_gb"] * 1024 - 16)
            assert got == row["class"], f"{name!r} -> {got!r}, want {row['class']!r}"


def test_normalize_class_nvidia_resolves_every_reference_slug_via_class_map() -> None:
    """Normalize class nvidia resolves every reference slug via class map."""
    rows = _ref_rows("nvidia")
    assert len(rows) >= 85
    cmap = {f"*{r['model']}": r["class"] for r in rows}
    for row in rows:
        assert normalize_class(GpuVendor.NVIDIA, f"NVIDIA {row['model']}", cmap) == row["class"]


def test_collector_fills_node_fields_from_payload() -> None:
    """Collector fills node fields from payload."""
    client = _StubInventoryClient(
        [
            {
                "node_uid": "u1",
                "node_name": "n1",
                "vendor": "nvidia",
                "class_map": CLASS_MAP,
                "raw": _fix("nvidia_smi_4090_4070.csv"),
            }
        ]
    )
    recs = asyncio.run(InventoryCollector(1, 1).collect(client)).records
    assert len(recs) == 4
    assert {(r.node_uid, r.node_name) for r in recs} == {("u1", "n1")}


def test_collector_dispatches_amd_intel_and_rejects_unknown_vendor() -> None:
    """Collector dispatches amd intel and rejects unknown vendor."""
    client = _StubInventoryClient(
        [
            {
                "node_uid": "a",
                "node_name": "na",
                "vendor": "amd",
                "class_map": {},
                "raw": _fix("rocm_smi_mi300x.json"),
            },
            {
                "node_uid": "i",
                "node_name": "ni",
                "vendor": "intel",
                "class_map": {},
                "raw": _fix("xpu_smi_max1100.json"),
            },
        ]
    )
    recs = asyncio.run(InventoryCollector(1, 1).collect(client)).records
    assert [r.vendor for r in recs] == [GpuVendor.AMD] * 2 + [GpuVendor.INTEL] * 2
    bad = _StubInventoryClient(
        [{"node_uid": "x", "node_name": "x", "vendor": "tpu", "class_map": {}, "raw": "x"}]
    )
    with pytest.raises(ValueError, match="tpu"):
        asyncio.run(InventoryCollector(1, 1).collect(bad))


def test_collector_zero_payloads_returns_empty_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Collector zero payloads returns empty and warns."""
    with caplog.at_level("WARNING"):
        assert asyncio.run(InventoryCollector(1, 1).collect(_StubInventoryClient([]))).records == []
    assert "no inventory payloads" in caplog.text


def test_empty_raw_uses_allocatable_and_node_class() -> None:
    """Empty raw uses allocatable and node class."""
    client = _StubInventoryClient(
        [
            {
                "node_uid": "u2",
                "node_name": "n2",
                "vendor": "nvidia",
                "class_map": {},
                "raw": "",
                "allocatable_gpus": 2,
                "node_class": "nvidia-rtx4090",
            }
        ]
    )
    recs = asyncio.run(InventoryCollector(1, 1).collect(client)).records
    assert len(recs) == 2
    assert all(r.state == InventoryState.COLD and r.schedulable is False for r in recs)
    assert all(
        r.gpu_class == "nvidia-rtx4090" and r.vram_total_mb == 0 and r.product_name == ""
        for r in recs
    )
    assert [r.gpu_index for r in recs] == [0, 1]


def test_empty_raw_without_allocatable_yields_nothing() -> None:
    """Empty raw without allocatable yields nothing."""
    client = _StubInventoryClient(
        [{"node_uid": "u3", "node_name": "n3", "vendor": "nvidia", "class_map": {}, "raw": ""}]
    )
    assert asyncio.run(InventoryCollector(1, 1).collect(client)).records == []


def _db(tmp_path: Path) -> Any:
    db = DAL("sqlite://inv.sqlite", folder=str(tmp_path))
    db.define_table(
        "gpu_inventory",
        Field("org_id", "integer"),
        Field("fleet_backend_id", "integer"),
        Field("node_uid"),
        Field("node_name"),
        Field("gpu_index", "integer"),
        Field("vendor", "integer"),
        Field("gpu_class"),
        Field("product_name"),
        Field("vram_total_mb", "integer"),
        Field("vram_used_mb", "integer"),
        Field("pci_bus_id"),
        Field("schedulable", "boolean"),
        Field("state", "integer"),
        Field("last_seen", "datetime"),
    )
    db.commit()
    db.close()  # release the main-thread connection; workers open their own
    return db


def _rec(idx: int, used: int, uid: str = "u1") -> GpuRecord:
    return GpuRecord(
        node_uid=uid,
        node_name="n1",
        gpu_index=idx,
        vendor=int(GpuVendor.NVIDIA),
        gpu_class="nvidia-rtx4090",
        product_name="NVIDIA GeForce RTX 4090",
        vram_total_mb=24564,
        vram_used_mb=used,
        pci_bus_id=f"0:{idx}",
        schedulable=True,
        state=int(InventoryState.WARM),
    )


def test_upsert_inventory_inserts_then_updates_same_key(tmp_path: Path) -> None:
    """Upsert inventory inserts then updates same key."""
    db = _db(tmp_path)

    async def _go() -> None:
        # one loop: the default executor reuses its idle worker (pydal connections are per-thread)
        await upsert_inventory(db, 1, 7, [_rec(0, 100)])
        await upsert_inventory(db, 1, 7, [_rec(0, 900)])

    asyncio.run(_go())
    rows = db(db.gpu_inventory).select()
    assert len(rows) == 1
    assert rows[0].vram_used_mb == 900 and rows[0].org_id == 7 and rows[0].last_seen is not None


def test_upsert_marks_absent_gpus_cold_and_scopes_to_backend(tmp_path: Path) -> None:
    """Upsert marks absent gpus cold and scopes to backend."""
    db = _db(tmp_path)

    async def _go() -> None:
        await upsert_inventory(db, 1, 7, [_rec(0, 1), _rec(1, 1)])
        await upsert_inventory(db, 2, 7, [_rec(0, 1)])
        await upsert_inventory(db, 1, 7, [_rec(0, 5)])

    asyncio.run(_go())
    by = {(r.fleet_backend_id, r.gpu_index): r for r in db(db.gpu_inventory).select()}
    assert by[(1, 1)].state == InventoryState.COLD and by[(1, 1)].schedulable is False
    assert by[(1, 0)].state == InventoryState.WARM
    assert by[(2, 0)].state == InventoryState.WARM


def _nv_payload(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "node_uid": "u",
        "node_name": "n",
        "vendor": "nvidia",
        "raw": _fix("nvidia_smi_4090_4070.csv"),
    }
    base.update(kw)
    return base


def test_payload_supplied_class_map_is_ignored() -> None:
    """A worker-supplied class_map must not influence classification."""
    forged = {"*A9000*": "nvidia-rtx4090"}
    client = _StubInventoryClient([_nv_payload(class_map=forged)])
    recs = asyncio.run(InventoryCollector(1, 1, class_map=CLASS_MAP).collect(client)).records
    assert recs[3].gpu_class is None and recs[3].state == InventoryState.UNCLASSIFIED
    assert recs[0].gpu_class == "nvidia-rtx4090"
    no_trusted = asyncio.run(InventoryCollector(1, 1).collect(client)).records
    assert all(r.gpu_class is None for r in no_trusted)


def test_forged_node_class_is_unclassified() -> None:
    """A node_class outside the reference slug set is rejected."""
    client = _StubInventoryClient(
        [_nv_payload(raw="", allocatable_gpus=2, node_class="nvidia-h200-forged")]
    )
    recs = asyncio.run(InventoryCollector(1, 1).collect(client)).records
    assert len(recs) == 2
    assert all(r.gpu_class is None and r.state == InventoryState.UNCLASSIFIED for r in recs)
    assert not any(r.schedulable for r in recs)


def test_unknown_class_map_slug_is_unclassified() -> None:
    """A trusted class_map pointing at a non-reference slug still yields UNCLASSIFIED."""
    recs = parse_nvidia_smi(_fix("nvidia_smi_4090_4070.csv"), {"*RTX 4090*": "nvidia-bogus"})
    assert recs[0].gpu_class is None
    assert recs[0].state == InventoryState.UNCLASSIFIED and recs[0].schedulable is False


@pytest.mark.parametrize("count", [-1, 65, 10**9, "many"])
def test_allocatable_out_of_range_rejects_payload(
    count: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Out-of-range or non-integer allocatable_gpus drops the node with a warning."""
    client = _StubInventoryClient([_nv_payload(raw="", allocatable_gpus=count)])
    with caplog.at_level("WARNING"):
        assert asyncio.run(InventoryCollector(1, 1).collect(client)).records == []
    assert "allocatable_gpus" in caplog.text


def test_allocatable_boundary_64_is_accepted() -> None:
    """64 is the inclusive upper bound."""
    client = _StubInventoryClient([_nv_payload(raw="", allocatable_gpus=64)])
    assert len(asyncio.run(InventoryCollector(1, 1).collect(client)).records) == 64


def test_parsers_cap_entries_per_payload(caplog: pytest.LogCaptureFixture) -> None:
    """Each SMI parser stops at 64 GPU entries."""
    csv_text = "\n".join(f"{i}, NVIDIA GeForce RTX 4090, 24564, 0, 0:{i}" for i in range(200))
    rocm = json.dumps(
        {
            f"card{i}": {
                "VRAM Total Memory (B)": "1048576",
                "VRAM Total Used Memory (B)": "0",
                "Card Series": "AMD Instinct MI300X",
                "PCI Bus": f"0:{i}",
            }
            for i in range(200)
        }
    )
    xpu = json.dumps(
        {
            "device_list": [
                {
                    "device_id": i,
                    "device_name": "Intel(R) Data Center GPU Max 1100",
                    "memory_physical_size_byte": 1048576,
                    "pci_bdf_address": f"0:{i}",
                }
                for i in range(200)
            ]
        }
    )
    with caplog.at_level("WARNING"):
        assert len(parse_nvidia_smi(csv_text, CLASS_MAP)) == 64
        assert len(parse_rocm_smi(rocm)) == 64
        assert len(parse_xpu_smi(xpu)) == 64
    assert "capped" in caplog.text


def test_collect_caps_total_records(caplog: pytest.LogCaptureFixture) -> None:
    """collect() drops records beyond 4096 total and logs it."""
    payloads = [_nv_payload(node_uid=f"u{i}", raw="", allocatable_gpus=64) for i in range(100)]
    with caplog.at_level("WARNING"):
        recs = asyncio.run(InventoryCollector(1, 1).collect(_StubInventoryClient(payloads))).records
    assert len(recs) == 4096
    assert "record cap" in caplog.text


def _count_cold(db: Any) -> int:
    return len([r for r in db(db.gpu_inventory).select() if r.state == InventoryState.COLD])


def test_cap_hit_is_incomplete_and_skips_cold_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hitting the record cap marks the result incomplete; no COLD sweep follows."""
    monkeypatch.setattr("shared.fleet.vllm_ray.inventory.MAX_RECORDS_PER_COLLECT", 3)
    client = _StubInventoryClient([_nv_payload(node_uid="flood")])
    res = asyncio.run(InventoryCollector(1, 1, class_map=CLASS_MAP).collect(client))
    assert res.complete is False and len(res.records) == 3
    db = _db(tmp_path)

    async def _go() -> None:
        await upsert_inventory(db, 1, 7, [_rec(0, 1, "legit")])
        await upsert_inventory(db, 1, 7, res.records, complete=res.complete)

    asyncio.run(_go())
    legit = [r for r in db(db.gpu_inventory).select() if r.node_uid == "legit"]
    assert len(legit) == 1 and legit[0].state == InventoryState.WARM
    assert _count_cold(db) == 0


def test_duplicate_node_uid_is_incomplete_first_payload_wins() -> None:
    """A repeated node_uid is dropped and marks the result incomplete."""
    client = _StubInventoryClient([_nv_payload(node_uid="d"), _nv_payload(node_uid="d")])
    res = asyncio.run(InventoryCollector(1, 1, class_map=CLASS_MAP).collect(client))
    assert res.complete is False and len(res.records) == 4


def test_unknown_node_uid_dropped_against_authoritative_list() -> None:
    """Payloads whose node_uid is not in the authoritative list are dropped."""
    client = _StubInventoryClient([_nv_payload(node_uid="ok"), _nv_payload(node_uid="evil")])
    res = asyncio.run(InventoryCollector(1, 1, class_map=CLASS_MAP).collect(client, {"ok"}))
    assert {r.node_uid for r in res.records} == {"ok"} and res.complete is False


def test_clean_collect_is_complete_and_sweeps_stale_cold(tmp_path: Path) -> None:
    """A complete run still marks absent rows COLD."""
    client = _StubInventoryClient([_nv_payload(node_uid="ok")])
    res = asyncio.run(InventoryCollector(1, 1, class_map=CLASS_MAP).collect(client, {"ok"}))
    assert res.complete is True
    db = _db(tmp_path)

    async def _go() -> None:
        await upsert_inventory(db, 1, 7, [_rec(0, 1, "gone")])
        await upsert_inventory(db, 1, 7, res.records, complete=res.complete)

    asyncio.run(_go())
    gone = [r for r in db(db.gpu_inventory).select() if r.node_uid == "gone"]
    assert gone[0].state == InventoryState.COLD
