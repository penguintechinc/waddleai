"""Ray job entrypoint: one GPU inventory payload per alive GPU node.

Submitted by ``HttpRayServeClient.run_inventory`` as
``python3 -m shared.fleet.vllm_ray.inventory_job``. It fans out one zero-CPU task per alive
GPU node (hard node affinity), runs the vendor SMI tool present on that node with the exact
flags ``inventory.py``'s parsers expect, and prints one ``WAGPU_JSON:{...}`` line per node.
Ray is imported inside ``main()`` only, so the payload builder is unit-testable without Ray.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess  # nosec B404 - fixed argv, no shell
import sys
from collections.abc import Callable
from typing import Any

PAYLOAD_PREFIX = "WAGPU_JSON:"
SMI_TIMEOUT_S = 30
NODE_CLASS_ENV = "WADDLEAI_GPU_CLASS"

# (vendor slug, binary, exact argv tail the inventory.py parser for that vendor expects)
_TOOLS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "nvidia",
        "nvidia-smi",
        (
            "--query-gpu=index,name,memory.total,memory.used,pci.bus_id",
            "--format=csv,noheader,nounits",
        ),
    ),
    ("amd", "rocm-smi", ("--showmeminfo", "vram", "--showproductname", "--json")),
    ("intel", "xpu-smi", ("discovery", "-j")),
)

Runner = Callable[[list[str]], str]
Which = Callable[[str], str | None]


def _run_smi(argv: list[str]) -> str:
    """Run an SMI command and return stdout; raises on non-zero exit or timeout."""
    out = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv built from the _TOOLS table
        argv, capture_output=True, text=True, timeout=SMI_TIMEOUT_S, check=True
    )
    return out.stdout


def build_node_payload(
    node_uid: str,
    node_name: str,
    allocatable_gpus: int,
    *,
    run: Runner = _run_smi,
    which: Which = shutil.which,
    node_class: str | None = None,
) -> dict[str, Any] | None:
    """Build the payload ``InventoryCollector`` consumes for one node, or None without a tool.

    A present-but-failing SMI tool yields ``raw == ""`` (the collector's cold-node path)
    rather than an invented success; no tool at all yields None (nothing to report).
    """
    for vendor, binary, tail in _TOOLS:
        path = which(binary)
        if path is None:
            continue
        try:
            raw = run([path, *tail])
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"{binary} failed on {node_name}: {exc}", file=sys.stderr)  # noqa: T201
            raw = ""
        payload: dict[str, Any] = {
            "node_uid": node_uid,
            "node_name": node_name,
            "vendor": vendor,
            "raw": raw,
            "allocatable_gpus": allocatable_gpus,
        }
        if node_class:
            payload["node_class"] = node_class
        return payload
    return None


def format_line(payload: dict[str, Any]) -> str:
    """One ``WAGPU_JSON:`` log line (single-line JSON) for ``payload``."""
    return PAYLOAD_PREFIX + json.dumps(payload, separators=(",", ":"))


def main() -> int:
    """Fan out one task per alive GPU node and print one payload line per node."""
    import ray  # noqa: PLC0415 - Ray only exists inside the cluster job
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy  # noqa: PLC0415

    ray.init(address="auto", logging_level="WARNING")

    @ray.remote(num_cpus=0)
    def _probe(node_uid: str, allocatable: int) -> dict[str, Any] | None:
        return build_node_payload(
            node_uid,
            socket.gethostname(),
            allocatable,
            node_class=os.environ.get(NODE_CLASS_ENV),
        )

    refs = []
    for node in ray.nodes():
        gpus = int(node.get("Resources", {}).get("GPU", 0))
        if not node.get("Alive") or gpus <= 0:
            continue
        strategy = NodeAffinitySchedulingStrategy(node_id=node["NodeID"], soft=False)
        refs.append(_probe.options(scheduling_strategy=strategy).remote(node["NodeID"], gpus))
    for payload in ray.get(refs, timeout=SMI_TIMEOUT_S * 2):
        if payload is not None:
            print(format_line(payload), flush=True)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
