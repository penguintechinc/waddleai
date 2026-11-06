"""Orchestration helpers for llama.cpp multi-node (RPC) deployments (Plan F, F5).

Turns the org's GPU inventory into planner pools, plans a layout, resolves the vendor image and
the node hostnames the manifests pin to, and rebuilds a layout from a persisted row. Everything
here is synchronous and tenant-scoped (every inventory read is filtered by ``org_id``); callers
wrap it in ``asyncio.to_thread``. No Quart imports.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared.fleet.enums import GpuVendor, LlamaCppTopology
from shared.fleet.llamacpp_rpc import RpcLayout, RpcRefused, RpcWorker, plan_rpc_layout
from shared.fleet.pricing import GPU_CLASSES_LABEL
from shared.fleet.vllm_ray.fit import Pool, build_pools

logger = logging.getLogger(__name__)

IMAGE_ENV_BY_VENDOR: dict[int, str] = {
    int(GpuVendor.NVIDIA): "WADDLEAI_LLAMACPP_RPC_IMAGE_CUDA",
    int(GpuVendor.AMD): "WADDLEAI_LLAMACPP_RPC_IMAGE_ROCM",
    int(GpuVendor.INTEL): "WADDLEAI_LLAMACPP_RPC_IMAGE_SYCL",
}
NO_INVENTORY = "No GPU inventory for this vendor. The GPU inventory collector must run first."


class RpcImageNotConfigured(RuntimeError):  # noqa: N818 -- name states the condition
    """The vendor's RPC image variable is unset; the message names the variable to set."""


class RpcPlacementUnavailable(RuntimeError):  # noqa: N818 -- name states the condition
    """A persisted RPC layout can no longer be resolved against the live inventory."""


def backend_for_org(db: Any, org_id: int) -> Any | None:
    """The org's first (lowest id) enabled vllm_ray fleet backend, which owns its GPU inventory."""
    table = db.fleet_backends
    return (
        db((table.org_id == org_id) & (table.type == "vllm_ray") & (table.status != "disabled"))
        .select(orderby=table.id, limitby=(0, 1))
        .first()
    )


def _inventory_rows(db: Any, org_id: int, backend_id: int) -> list[Any]:
    """The org's inventory rows for one backend (tenant filter applied in the query)."""
    table = db.gpu_inventory
    return list(db((table.org_id == org_id) & (table.fleet_backend_id == backend_id)).select())


def pools_from_inventory(
    db: Any,
    fleet_backend_id: int,
    org_id: int,
    allowed: list[tuple[str, str]] | None = None,
) -> list[Pool]:
    """Schedulable WARM GPUs of one backend grouped into ``Pool`` s (class, then node).

    ``allowed`` (``(node_uid, gpu_class)`` pairs, one per requested GPU) restricts the pool to
    those GPUs: at most as many GPUs per pair as the request names, never more than exist.
    """
    rows = _inventory_rows(db, org_id, fleet_backend_id)
    if allowed is not None:
        budget: dict[tuple[str, str], int] = {}
        for pair in allowed:
            budget[pair] = budget.get(pair, 0) + 1
        kept = []
        for row in rows:
            key = (row.node_uid, row.gpu_class)
            if budget.get(key, 0) > 0:
                budget[key] -= 1
                kept.append(row)
        rows = kept
    return build_pools(rows)


def hostnames_for(db: Any, org_id: int, backend_id: int, node_uids: set[str]) -> dict[str, str]:
    """``node_uid -> Kubernetes hostname`` from the org's inventory; unknown uids are omitted."""
    names: dict[str, str] = {}
    for row in _inventory_rows(db, org_id, backend_id):
        if row.node_uid in node_uids:
            names[row.node_uid] = row.node_name
    return names


def rpc_image_for_vendor(vendor: int) -> str:
    """Digest-pinned RPC image for a vendor from its env variable (never an upstream fallback)."""
    variable = IMAGE_ENV_BY_VENDOR.get(int(vendor))
    if variable is None:
        raise RpcImageNotConfigured(f"no RPC image variable exists for GPU vendor {vendor}")
    image = os.environ.get(variable, "").strip()
    if not image:
        raise RpcImageNotConfigured(
            f"{variable} is not set; the llama.cpp RPC image for this GPU vendor is not configured."
        )
    if "@sha256:" not in image:
        raise RpcImageNotConfigured(f"{variable} must be a digest-pinned image (name@sha256:...).")
    return image


def internal_tls_enabled() -> bool:
    """Ray internal TLS flag from the chart; only logged by the planner (rpc-server has no TLS)."""
    return os.environ.get("WADDLEAI_FLEET_RAY_INTERNAL_TLS", "").strip().lower() == "true"


def plan_for_org(
    db: Any,
    org_id: int,
    *,
    gguf_bytes: int,
    allow_mixed_class: bool,
    allowed: list[tuple[str, str]] | None,
    mutual_auth_enabled: bool,
) -> tuple[RpcLayout, int, dict[str, str]]:
    """Plan a layout from the org's inventory; returns ``(layout, backend_id, hostnames)``.

    Raises ``RpcRefused`` (admin-facing copy) for every refusal, including a missing backend.
    """
    backend = backend_for_org(db, org_id)
    if backend is None:
        raise RpcRefused(NO_INVENTORY)
    pools = pools_from_inventory(db, int(backend.id), org_id, allowed)
    layout = plan_rpc_layout(
        weight_bytes=gguf_bytes,
        pools=pools,
        allow_mixed_class=allow_mixed_class,
        mutual_auth_enabled=mutual_auth_enabled,
        internal_tls_enabled=internal_tls_enabled(),
    )
    uids = {layout.head_node_uid, *(w.node_uid for w in layout.workers)}
    return layout, int(backend.id), hostnames_for(db, org_id, int(backend.id), uids)


def rpc_workers_json(layout: RpcLayout) -> list[dict[str, Any]]:
    """The persisted per-worker description: one entry per rpc-server pod (one GPU each)."""
    return [
        {
            "node_uid": w.node_uid,
            "gpu_class": w.gpu_class,
            "gpu_count": 1,
            "tensor_split_share": w.tensor_split_share,
        }
        for w in layout.workers
    ]


def layout_columns(layout: RpcLayout) -> dict[str, Any]:
    """Row columns that persist ``layout`` (also what pricing and re-deploys read back)."""
    return {
        "topology": int(LlamaCppTopology.RPC_MULTI_NODE),
        "rpc_workers": rpc_workers_json(layout),
        "tensor_split": ",".join(str(share) for share in layout.tensor_split),
        "rpc_head_node_uid": layout.head_node_uid,
        "gpu_count": layout.head_gpus,
        "n_gpu_layers": 999,
        "node_selector": {GPU_CLASSES_LABEL: layout.head_gpu_class},
    }


def single_node_columns() -> dict[str, Any]:
    """Row columns that reset a deployment to the single-node topology."""
    return {
        "topology": int(LlamaCppTopology.SINGLE_NODE),
        "rpc_workers": None,
        "tensor_split": None,
        "rpc_head_node_uid": None,
    }


def layout_from_row(db: Any, dep: Any, org_id: int | None) -> tuple[RpcLayout, dict[str, str]]:
    """Rebuild the persisted layout and node hostnames for a re-deploy or manifest export.

    Hostnames and the head's vendor come from the org's live inventory; a node that has left it
    raises ``RpcPlacementUnavailable`` (the layout must be re-planned, never guessed).
    """
    if org_id is None:
        raise RpcPlacementUnavailable("this deployment has no owning organization")
    backend = backend_for_org(db, org_id)
    head_uid = getattr(dep, "rpc_head_node_uid", None)
    head_class = ((dep.node_selector or {}).get(GPU_CLASSES_LABEL)) if head_uid else None
    if backend is None or not head_uid or not head_class or not dep.tensor_split:
        raise RpcPlacementUnavailable("this deployment has no stored RPC layout; re-create it")
    workers = [
        RpcWorker(
            str(w["node_uid"]),
            str(w["gpu_class"]),
            0,  # free VRAM is a planning input only; it is not persisted or rendered
            float(w.get("tensor_split_share", 0)),
        )
        for w in (dep.rpc_workers or [])
    ]
    uids = {head_uid, *(w.node_uid for w in workers)}
    hostnames = hostnames_for(db, org_id, int(backend.id), uids)
    gone = sorted(uids - set(hostnames))
    if gone:
        raise RpcPlacementUnavailable(
            f"node(s) {', '.join(gone)} left the GPU inventory; re-create the deployment"
        )
    vendor = next(
        (
            int(r.vendor)
            for r in _inventory_rows(db, org_id, int(backend.id))
            if r.node_uid == head_uid and r.gpu_class == head_class
        ),
        None,
    )
    if vendor is None:
        raise RpcPlacementUnavailable("the head node's GPUs are no longer in the GPU inventory")
    shares = [float(s) for s in str(dep.tensor_split).split(",") if s]
    layout = RpcLayout(
        vendor=vendor,
        head_node_uid=str(head_uid),
        head_gpu_class=str(head_class),
        head_gpus=int(dep.gpu_count or 1),
        workers=workers,
        tensor_split=shares,
        vram_needed_mb=0,
        vram_available_mb=0,
        nodes_used=len(uids),
        explanation="",
    )
    return layout, hostnames
