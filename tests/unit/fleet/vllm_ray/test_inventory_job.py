"""inventory_job payload builder + the contract with InventoryCollector."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from shared.fleet.enums import GpuVendor, InventoryState
from shared.fleet.vllm_ray import inventory_job as job
from shared.fleet.vllm_ray.inventory import InventoryCollector
from shared.fleet.vllm_ray.ray_client import _parse_payloads

NVIDIA = "0, NVIDIA GeForce RTX 4090, 24564, 120, 00000000:01:00.0\n"
ROCM = json.dumps(
    {
        "card0": {
            "VRAM Total Memory (B)": str(16 * 1024**3),
            "VRAM Total Used Memory (B)": "1048576",
            "Card Series": "Radeon RX 9060 XT",
            "PCI Bus": "0000:03:00.0",
        }
    }
)
XPU = json.dumps(
    {
        "device_list": [
            {
                "device_id": 0,
                "device_name": "Intel(R) Arc(TM) A770 Graphics",
                "memory_physical_size_byte": 16 * 1024**3,
                "pci_bdf_address": "0000:04:00.0",
            }
        ]
    }
)


def _env(tool: str, output: str | Exception) -> tuple[Any, Any, list[list[str]]]:
    """A ``which`` exposing only ``tool`` and a runner returning/raising ``output``."""
    calls: list[list[str]] = []

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == tool else None

    def run(argv: list[str]) -> str:
        calls.append(argv)
        if isinstance(output, Exception):
            raise output
        return output

    return which, run, calls


@pytest.mark.parametrize(
    ("tool", "vendor", "out", "flag"),
    [
        ("nvidia-smi", "nvidia", NVIDIA, "--format=csv,noheader,nounits"),
        ("rocm-smi", "amd", ROCM, "--json"),
        ("xpu-smi", "intel", XPU, "discovery"),
    ],
)
def test_payload_per_vendor_has_expected_shape_and_flags(
    tool: str, vendor: str, out: str, flag: str
) -> None:
    """Each vendor's SMI tool is invoked with the parser-expected flags."""
    which, run, calls = _env(tool, out)
    p = job.build_node_payload("nid", "host", 1, run=run, which=which)
    assert p == {
        "node_uid": "nid",
        "node_name": "host",
        "vendor": vendor,
        "raw": out,
        "allocatable_gpus": 1,
    }
    assert calls[0][0] == f"/usr/bin/{tool}" and flag in calls[0]


def test_no_smi_tool_yields_none() -> None:
    """A node with no vendor tool reports nothing rather than guessing a vendor."""
    assert job.build_node_payload("n", "h", 1, run=lambda a: "", which=lambda b: None) is None


def test_failing_tool_yields_cold_empty_raw_and_node_class() -> None:
    """Tool present but failing -> raw '' (collector cold path); node_class passes through."""
    which, run, _ = _env("nvidia-smi", subprocess.CalledProcessError(1, "x"))
    p = job.build_node_payload("n", "h", 2, run=run, which=which, node_class="nvidia-rtx4090")
    assert p is not None and p["raw"] == "" and p["node_class"] == "nvidia-rtx4090"


class _Client:
    """InventoryClient fed from the job's real output lines."""

    def __init__(self, lines: list[str]) -> None:
        self._payloads = _parse_payloads("\n".join(lines))

    async def run_inventory(self) -> list[dict[str, Any]]:
        return self._payloads


@pytest.mark.parametrize(
    ("tool", "out", "vendor"),
    [
        ("nvidia-smi", NVIDIA, GpuVendor.NVIDIA),
        ("rocm-smi", ROCM, GpuVendor.AMD),
        ("xpu-smi", XPU, GpuVendor.INTEL),
    ],
)
async def test_job_output_parses_through_inventory_collector(
    tool: str, out: str, vendor: GpuVendor
) -> None:
    """Contract: format_line output survives log parsing and the collector for all vendors."""
    which, run, _ = _env(tool, out)
    p = job.build_node_payload("nid", "host", 1, run=run, which=which)
    assert p is not None
    line = job.format_line(p)
    assert line.startswith("WAGPU_JSON:") and "\n" not in line
    result = await InventoryCollector(1, 1, {"*RTX 4090*": "nvidia-rtx4090"}).collect(
        _Client(["noise", line, "more noise"]), {"nid"}
    )
    assert result.complete
    (rec,) = result.records
    assert rec.vendor == int(vendor) and rec.node_uid == "nid" and rec.vram_total_mb > 0
    assert rec.state in (int(InventoryState.WARM), int(InventoryState.UNCLASSIFIED))
