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
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from prometheus_client import Counter

from shared.fleet.enums import GpuVendor, InventoryState

logger = logging.getLogger(__name__)

_INCOMPLETE_TOTAL = Counter(
    "waddleai_gpu_inventory_incomplete_total",
    "GPU inventory upserts that skipped the stale-row COLD sweep (incomplete collect)",
)

_MB = 1048576
MAX_GPUS_PER_NODE = 64
MAX_RECORDS_PER_COLLECT = 4096
STALE_AFTER_S = 180.0  # 3 inventory intervals
_REFERENCE_DIR = Path(__file__).resolve().parents[3] / "docs" / "reference"
_PARENS = re.compile(r"\([^)]*\)")
_GB = re.compile(r"^(\d+)gb$")
_AMD_NOISE = frozenset({"amd", "graphics", "series"})
_INTEL_NOISE = frozenset({"intel", "graphics", "gpu", "data", "center"})
# Slug families whose reference rows differ only by VRAM size: the size suffix is
# resolved from the product name ("16GB") or from the reported VRAM.
_VRAM_VARIANT_SLUGS = frozenset({"amd-rx9060xt", "amd-radeon-pro-w7800", "intel-arc-a770"})


@lru_cache(maxsize=1)
def reference_slugs() -> frozenset[str]:
    """Load the valid GPU class slugs from the price-reference JSON files (once).

    Raises when no slugs load: an empty set would silently unclassify every GPU.
    """
    slugs: set[str] = set()
    for vendor in ("nvidia", "amd", "intel"):
        data = json.loads((_REFERENCE_DIR / f"gpu-price-reference-{vendor}.json").read_text())
        slugs.update(str(row["class"]) for row in data["gpus"])
    if not slugs:
        raise RuntimeError(f"no GPU class slugs found under {_REFERENCE_DIR}")
    return frozenset(slugs)


def _valid_class(gpu_class: str | None) -> str | None:
    """Return gpu_class only if it is a known reference slug, else None."""
    return gpu_class if gpu_class in reference_slugs() else None


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
    gpu_class = _valid_class(gpu_class)
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


def _cap_entries[T](items: list[T], kind: str) -> list[T]:
    """Truncate a per-node GPU entry list to MAX_GPUS_PER_NODE, logging the drop."""
    if len(items) > MAX_GPUS_PER_NODE:
        logger.warning("%s entries capped at %d (got %d)", kind, MAX_GPUS_PER_NODE, len(items))
        return items[:MAX_GPUS_PER_NODE]
    return items


def parse_nvidia_smi(csv_text: str, class_map: dict[str, str]) -> list[GpuRecord]:
    """Parse ``nvidia-smi --query-gpu=index,name,memory.total,memory.used,pci.bus_id`` CSV."""
    records: list[GpuRecord] = []
    rows = [r for r in csv.reader(io.StringIO(csv_text)) if r]
    for row in _cap_entries(rows, "nvidia-smi"):
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
    for index, card in _cap_entries(cards, "rocm-smi"):
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
    for dev in _cap_entries(list(data["device_list"]), "xpu-smi"):
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


@dataclass(slots=True)
class InventoryResult:
    """Collected records, plus the nodes whose own payload was unusable this cycle.

    ``skipped_node_uids`` keep their previous rows untouched (until the age sweep);
    ``complete`` is False when any node was skipped or dropped.
    """

    records: list[GpuRecord]
    complete: bool
    skipped_node_uids: frozenset[str] = frozenset()


class _RejectPayload(Exception):  # noqa: N818 - internal control-flow signal
    """Raised to drop one untrusted node payload without failing the whole collect."""


class InventoryCollector:
    """Collects per-node GPU inventory from the Ray cluster and classifies each node."""

    def __init__(
        self, backend_id: int, org_id: int, class_map: dict[str, str] | None = None
    ) -> None:
        """Bind the collector to one backend, its org, and its TRUSTED NVIDIA class_map.

        The class_map comes from backend config, never from worker payloads.
        """
        self.backend_id = backend_id
        self.org_id = org_id
        self.class_map: dict[str, str] = dict(class_map or {})

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

    def _parse_payload(self, payload: dict[str, Any]) -> list[GpuRecord]:
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
            count = self._allocatable(payload)
            node_class = payload.get("node_class")
            valid = _valid_class(node_class if isinstance(node_class, str) else None)
            state = InventoryState.UNCLASSIFIED if node_class and not valid else InventoryState.COLD
            return [
                GpuRecord(
                    node_uid="",
                    node_name="",
                    gpu_index=i,
                    vendor=int(vendor_id),
                    gpu_class=valid,
                    product_name="",
                    vram_total_mb=0,
                    vram_used_mb=0,
                    pci_bus_id="",
                    schedulable=False,
                    state=int(state),
                )
                for i in range(count)
            ]
        if vendor == "nvidia":
            return parse_nvidia_smi(raw, self.class_map)
        if vendor == "amd":
            return parse_rocm_smi(raw)
        return parse_xpu_smi(raw)

    def _allocatable(self, payload: dict[str, Any]) -> int:
        """Validate allocatable_gpus to 0..MAX_GPUS_PER_NODE; out of range yields 0 GPUs."""
        value = payload.get("allocatable_gpus", 0) or 0
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_GPUS_PER_NODE
        ):
            logger.warning(
                "rejecting node %r: allocatable_gpus %r outside 0..%d",
                payload.get("node_uid"),
                value,
                MAX_GPUS_PER_NODE,
            )
            raise _RejectPayload
        return value

    async def collect(
        self, client: InventoryClient, authoritative_node_ids: Collection[str] | None = None
    ) -> InventoryResult:
        """Gather inventory from every worker into an ``InventoryResult``.

        A node whose payload is rejected, duplicated (ALL its payloads dropped) or cut by
        the record cap lands in ``skipped_node_uids`` so the upsert leaves its rows alone.
        When ``authoritative_node_ids`` (the trusted Ray node list) is given, payloads for
        any other node_uid are dropped. Zero payloads yields an incomplete empty result.
        """
        payloads = await client.run_inventory()
        if not payloads:
            logger.warning(
                "no inventory payloads for backend %s (org %s)", self.backend_id, self.org_id
            )
            return InventoryResult([], complete=False)
        allowed = None if authoritative_node_ids is None else set(authoritative_node_ids)
        counts: dict[str, int] = {}
        for payload in payloads:
            uid = str(payload.get("node_uid", ""))
            counts[uid] = counts.get(uid, 0) + 1
        skipped: set[str] = set()
        complete = True
        out: list[GpuRecord] = []
        capped = False
        for payload in payloads:
            uid = str(payload.get("node_uid", ""))
            if counts[uid] > 1:
                logger.warning("duplicate inventory payloads for node %r: all dropped", uid)
                skipped.add(uid)
                complete = False
                continue
            if allowed is not None and uid not in allowed:
                logger.warning("inventory payload for unknown node %r dropped", uid)
                complete = False
                continue
            if capped:
                skipped.add(uid)
                continue
            try:
                parsed = self._parse_payload(payload)
            except _RejectPayload:
                skipped.add(uid)
                complete = False
                continue
            node = self._classify_node(
                [
                    dataclasses.replace(r, node_uid=uid, node_name=str(payload["node_name"]))
                    for r in parsed
                ]
            )
            if len(out) + len(node) > MAX_RECORDS_PER_COLLECT:
                logger.warning(
                    "record cap %d hit for backend %s: node %r and later nodes skipped",
                    MAX_RECORDS_PER_COLLECT,
                    self.backend_id,
                    uid,
                )
                capped = True
                skipped.add(uid)
                complete = False
                continue
            out.extend(node)
        return InventoryResult(out, complete=complete, skipped_node_uids=frozenset(skipped))


async def upsert_inventory(
    db: Any,
    fleet_backend_id: int,
    org_id: int,
    records: list[GpuRecord],
    *,
    skip_node_uids: Collection[str] = (),
    authoritative_node_ids: Collection[str] | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> None:
    """Upsert records on (backend, node_uid, gpu_index) and COLD-sweep stale rows.

    Unseen rows go COLD, except rows of ``skip_node_uids`` (nodes whose own payload was
    rejected this cycle), which keep their previous state. Rows of nodes absent from the
    trusted ``authoritative_node_ids`` always go COLD. Independently, any row not seen for
    ``stale_after_s`` (3 inventory intervals) goes COLD whatever happened. Blocking DAL
    calls run in a worker thread so the event loop stays free.
    """
    skip = frozenset(skip_node_uids)
    trusted = None if authoritative_node_ids is None else frozenset(authoritative_node_ids)
    if skip:
        logger.warning(
            "incomplete inventory for backend %s: %d node(s) left untouched",
            fleet_backend_id,
            len(skip),
        )
        _INCOMPLETE_TOTAL.inc()

    def _write() -> None:
        table = db.gpu_inventory
        now = datetime.now(UTC).replace(tzinfo=None)
        cutoff = now - timedelta(seconds=stale_after_s)
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
            if (row.node_uid, row.gpu_index) in seen:
                continue
            stale = row.last_seen is not None and row.last_seen < cutoff
            off_list = trusted is not None and row.node_uid not in trusted
            if stale or off_list or row.node_uid not in skip:
                db(table.id == row.id).update(state=int(InventoryState.COLD), schedulable=False)
        db.commit()

    await asyncio.to_thread(_write)
