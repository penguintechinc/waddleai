"""GPU inventory discovery for the vLLM-on-Ray fleet.

Parses ``nvidia-smi`` / ``rocm-smi`` / ``xpu-smi`` output into ``GpuRecord`` rows,
emits the exact class slugs of ``docs/reference/gpu-price-reference-*.json`` (AM8),
and upserts the result into ``gpu_inventory``. Parsers are pure; the collector and
upsert are the only async/IO surfaces.
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import fnmatch
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from shared.fleet.enums import GpuVendor, InventoryState

logger = logging.getLogger(__name__)

_MB = 1048576
_PARENS = re.compile(r"\([^)]*\)")
_GB = re.compile(r"^(\d+)gb$")
_AMD_NOISE = frozenset({"amd", "graphics", "series"})
_INTEL_NOISE = frozenset({"intel", "graphics", "gpu", "data", "center"})
# Slug families whose reference rows differ only by VRAM size: the size suffix is
# resolved from the product name ("16GB") or from the reported VRAM.
_VRAM_VARIANT_SLUGS = frozenset({"amd-rx9060xt", "amd-radeon-pro-w7800", "intel-arc-a770"})


class InventoryClient(Protocol):
    """Minimal client surface the collector needs (satisfied by RayServeClient)."""

    async def run_inventory(self) -> list[dict[str, Any]]:
        """Return one payload per worker node (D5 shape)."""
        ...


@dataclass(slots=True)
class GpuRecord:
    """One discovered GPU, mirroring a ``gpu_inventory`` row."""

    node_uid: str
    node_name: str
    gpu_index: int
    vendor: int
    gpu_class: str | None
    product_name: str
    vram_total_mb: int
    vram_used_mb: int
    pci_bus_id: str
    schedulable: bool
    state: int


def _tokens(name: str, noise: frozenset[str]) -> tuple[list[str], int | None]:
    """Lowercase and tokenize a product name, returning (tokens, gb-from-name)."""
    cleaned = _PARENS.sub(" ", name.lower())
    gb: int | None = None
    out: list[str] = []
    for tok in cleaned.split():
        match = _GB.match(tok)
        if match:
            gb = int(match.group(1))
        elif tok not in noise:
            out.append(tok)
    return out, gb


def _slug_amd(name: str) -> str | None:
    """Derive the reference slug for an AMD product name, or None if empty."""
    toks, gb = _tokens(name, _AMD_NOISE)
    if toks and toks[0] == "radeon" and len(toks) > 1 and toks[1] == "rx":
        toks = toks[1:]
    if not toks:
        return None
    if toks[0] == "rx":
        return _with_variant("amd-rx" + "".join(toks[1:]), gb)
    return _with_variant("amd-" + "-".join(toks), gb)


def _slug_intel(name: str) -> str | None:
    """Derive the reference slug for an Intel product name, or None if empty."""
    toks, gb = _tokens(name, _INTEL_NOISE)
    if not toks:
        return None
    if toks[0] == "gaudi" and len(toks) > 1 and toks[1].isdigit():
        toks = [toks[0] + toks[1], *toks[2:]]
    return _with_variant("intel-" + "-".join(toks), gb)


def _with_variant(slug: str, gb: int | None) -> str | None:
    """Append the VRAM suffix for variant families; None when it cannot be resolved."""
    if slug not in _VRAM_VARIANT_SLUGS:
        return slug
    return f"{slug}-{gb}gb" if gb else None


def normalize_class(
    vendor: int, product_name: str, class_map: dict[str, str], vram_total_mb: int = 0
) -> str | None:
    """Map a product name to a reference class slug (None means unclassified).

    NVIDIA resolves through the caller-supplied fnmatch ``class_map``; AMD and Intel
    are derived. ``vram_total_mb`` disambiguates same-name VRAM variants.
    """
    if vendor == GpuVendor.NVIDIA:
        upper = product_name.upper()
        for pattern, slug in class_map.items():
            if fnmatch.fnmatchcase(upper, pattern.upper()):
                return slug
        return None
    if vram_total_mb > 0 and not re.search(r"\d+\s*gb", product_name, re.IGNORECASE):
        product_name = f"{product_name} {round(vram_total_mb / 1024)}GB"
    if vendor == GpuVendor.AMD:
        return _slug_amd(product_name)
    if vendor == GpuVendor.INTEL:
        return _slug_intel(product_name)
    return None


def _record(
    index: int,
    vendor: GpuVendor,
    product: str,
    total_mb: int,
    used_mb: int,
    pci: str,
    gpu_class: str | None,
) -> GpuRecord:
    """Build a record applying the state rules (degraded sentinel, unclassified)."""
    if total_mb == 0:
        state, schedulable = InventoryState.COLD, False
    elif gpu_class is None:
        state, schedulable = InventoryState.UNCLASSIFIED, False
    else:
        state, schedulable = InventoryState.WARM, True
    return GpuRecord(
        node_uid="",
        node_name="",
        gpu_index=index,
        vendor=int(vendor),
        gpu_class=gpu_class,
        product_name=product,
        vram_total_mb=total_mb,
        vram_used_mb=used_mb,
        pci_bus_id=pci,
        schedulable=schedulable,
        state=int(state),
    )


def parse_nvidia_smi(csv_text: str, class_map: dict[str, str]) -> list[GpuRecord]:
    """Parse ``nvidia-smi --query-gpu=index,name,memory.total,memory.used,pci.bus_id`` CSV."""
    records: list[GpuRecord] = []
    for row in csv.reader(io.StringIO(csv_text)):
        if not row:
            continue
        fields = [f.strip() for f in row]
        if len(fields) != 5:
            raise ValueError(f"nvidia-smi row needs 5 fields, got {len(fields)}: {row!r}")
        index, name, total, used, pci = fields
        total_mb = int(total)
        cls = normalize_class(GpuVendor.NVIDIA, name, class_map, total_mb)
        records.append(_record(int(index), GpuVendor.NVIDIA, name, total_mb, int(used), pci, cls))
    return records


def parse_rocm_smi(json_text: str) -> list[GpuRecord]:
    """Parse ``rocm-smi --showmeminfo vram --showproductname --json`` output."""
    data = json.loads(json_text)
    cards = sorted((int(k[4:]), v) for k, v in data.items() if re.fullmatch(r"card\d+", k))
    records: list[GpuRecord] = []
    for index, card in cards:
        total_mb = int(card["VRAM Total Memory (B)"]) // _MB
        used_mb = int(card["VRAM Total Used Memory (B)"]) // _MB
        name = str(card["Card Series"])
        cls = normalize_class(GpuVendor.AMD, name, {}, total_mb)
        records.append(
            _record(index, GpuVendor.AMD, name, total_mb, used_mb, str(card["PCI Bus"]), cls)
        )
    return records


def parse_xpu_smi(json_text: str) -> list[GpuRecord]:
    """Parse ``xpu-smi discovery -j`` output; used VRAM is 0 (needs a stats dump, v1 gap)."""
    data = json.loads(json_text)
    records: list[GpuRecord] = []
    for dev in data["device_list"]:
        total_mb = int(dev["memory_physical_size_byte"]) // _MB
        name = str(dev["device_name"])
        cls = normalize_class(GpuVendor.INTEL, name, {}, total_mb)
        records.append(
            _record(
                int(dev["device_id"]),
                GpuVendor.INTEL,
                name,
                total_mb,
                0,
                str(dev["pci_bdf_address"]),
                cls,
            )
        )
    return records


class InventoryCollector:
    """Collects per-node GPU inventory from the Ray cluster and classifies each node."""

    def __init__(self, backend_id: int, org_id: int) -> None:
        """Bind the collector to one fleet backend and its owning org."""
        self.backend_id = backend_id
        self.org_id = org_id

    @staticmethod
    def _classify_node(records: list[GpuRecord]) -> list[GpuRecord]:
        """Flag AMD/Intel nodes with more than one product as MIXED_UNSUPPORTED."""
        if not records or records[0].vendor not in (GpuVendor.AMD, GpuVendor.INTEL):
            return records
        if len({r.product_name for r in records}) < 2:
            return records
        for rec in records:
            rec.state = int(InventoryState.MIXED_UNSUPPORTED)
            rec.schedulable = False
        return records

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> list[GpuRecord]:
        """Dispatch one worker payload to its vendor parser (or the cold-node path)."""
        vendor = str(payload["vendor"])
        if vendor not in ("nvidia", "amd", "intel"):
            raise ValueError(f"unknown inventory vendor: {vendor!r}")
        raw = str(payload.get("raw", ""))
        if not raw.strip():
            vendor_id = {
                "nvidia": GpuVendor.NVIDIA,
                "amd": GpuVendor.AMD,
                "intel": GpuVendor.INTEL,
            }[vendor]
            count = int(payload.get("allocatable_gpus") or 0)
            node_class = payload.get("node_class")
            return [
                GpuRecord(
                    node_uid="",
                    node_name="",
                    gpu_index=i,
                    vendor=int(vendor_id),
                    gpu_class=node_class,
                    product_name="",
                    vram_total_mb=0,
                    vram_used_mb=0,
                    pci_bus_id="",
                    schedulable=False,
                    state=int(InventoryState.COLD),
                )
                for i in range(count)
            ]
        if vendor == "nvidia":
            return parse_nvidia_smi(raw, dict(payload.get("class_map") or {}))
        if vendor == "amd":
            return parse_rocm_smi(raw)
        return parse_xpu_smi(raw)

    async def collect(self, client: InventoryClient) -> list[GpuRecord]:
        """Gather inventory from every worker; zero payloads yields ``[]`` plus a warning."""
        payloads = await client.run_inventory()
        if not payloads:
            logger.warning(
                "no inventory payloads for backend %s (org %s)", self.backend_id, self.org_id
            )
            return []
        out: list[GpuRecord] = []
        for payload in payloads:
            node = [
                dataclasses.replace(
                    r, node_uid=str(payload["node_uid"]), node_name=str(payload["node_name"])
                )
                for r in self._parse_payload(payload)
            ]
            out.extend(self._classify_node(node))
        return out


async def upsert_inventory(
    db: Any, fleet_backend_id: int, org_id: int, records: list[GpuRecord]
) -> None:
    """Upsert records on (backend, node_uid, gpu_index); absent rows become COLD.

    Blocking DAL calls run in a worker thread so the event loop stays free.
    """

    def _write() -> None:
        table = db.gpu_inventory
        now = datetime.now(UTC).replace(tzinfo=None)
        seen: set[tuple[str, int]] = set()
        for rec in records:
            seen.add((rec.node_uid, rec.gpu_index))
            values = {
                "org_id": org_id,
                "node_name": rec.node_name,
                "vendor": rec.vendor,
                "gpu_class": rec.gpu_class,
                "product_name": rec.product_name,
                "vram_total_mb": rec.vram_total_mb,
                "vram_used_mb": rec.vram_used_mb,
                "pci_bus_id": rec.pci_bus_id,
                "schedulable": rec.schedulable,
                "state": rec.state,
                "last_seen": now,
            }
            query = (
                (table.fleet_backend_id == fleet_backend_id)
                & (table.node_uid == rec.node_uid)
                & (table.gpu_index == rec.gpu_index)
            )
            if db(query).count():
                db(query).update(**values)
            else:
                table.insert(
                    fleet_backend_id=fleet_backend_id,
                    node_uid=rec.node_uid,
                    gpu_index=rec.gpu_index,
                    **values,
                )
        for row in db(table.fleet_backend_id == fleet_backend_id).select():
            if (row.node_uid, row.gpu_index) not in seen:
                db(table.id == row.id).update(state=int(InventoryState.COLD), schedulable=False)
        db.commit()

    await asyncio.to_thread(_write)
