"""Layout planner for llama.cpp multi-node serving over rpc-server (Plan F, F2).

Picks which GPUs serve one GGUF model: one ``llama-server`` head plus one ``rpc-server`` worker
pod per remaining GPU, all of one vendor. Pure functions over ``Pool`` inventory (no I/O), so every
refusal is deterministic and every refusal string is admin-facing copy that says what to change.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from shared.fleet.vllm_ray.fit import Pool

__all__ = [
    "MAX_RPC_GPUS",
    "RPC_AUTH_REFUSAL",
    "RPC_KV_CTX_OVERHEAD_MB",
    "RPC_SLOWDOWN_COPY",
    "RpcGpu",
    "RpcLayout",
    "RpcRefused",
    "RpcWorker",
    "plan_rpc_layout",
]

logger = logging.getLogger(__name__)

MAX_RPC_GPUS: int = 8
RPC_KV_CTX_OVERHEAD_MB: int = 1536  # per GPU, runtime context (spec 4.4)
RPC_SLOWDOWN_COPY: str = "slower across nodes (expect 20–50% lower decode throughput)"
# AM17: llama.cpp RPC needs Cilium mutual auth ONLY; rpc-server has no TLS, so internal TLS never
# satisfies the link.
RPC_AUTH_REFUSAL: str = "enable Cilium mutual auth (SPIRE) to deploy llama.cpp across nodes"

_VENDOR_NAMES = {1: "NVIDIA", 2: "AMD", 3: "Intel"}


class RpcRefused(ValueError):  # noqa: N818 -- name fixed by the plan
    """The RPC layout cannot be planned; ``str(exc)`` is plain-language admin copy."""


@dataclass(slots=True, frozen=True)
class RpcGpu:
    """One schedulable GPU a layout may use."""

    node_uid: str
    gpu_class: str
    vram_free_mb: int


@dataclass(slots=True)
class RpcWorker:
    """One rpc-server pod; it owns exactly one GPU (rpc-server exposes one device per process)."""

    node_uid: str
    gpu_class: str
    vram_free_mb: int
    tensor_split_share: float


@dataclass(slots=True)
class RpcLayout:
    """A planned head + workers layout with a VRAM-proportional tensor split."""

    vendor: int
    head_node_uid: str
    head_gpu_class: str
    head_gpus: int
    workers: list[RpcWorker]  # device order: workers first, head-local GPUs after
    tensor_split: list[float]  # len == len(workers) + head_gpus; sums to 1.0 at 4 dp
    vram_needed_mb: int
    vram_available_mb: int
    nodes_used: int
    explanation: str


def _expand(pools: list[Pool]) -> list[RpcGpu]:
    """One ``RpcGpu`` per GPU across the pools, dropping GPUs with no free VRAM."""
    gpus: list[RpcGpu] = []
    for pool in pools:
        for node_uid, count, free_mb in pool.nodes:
            if free_mb <= 0:
                continue
            gpus.extend(RpcGpu(node_uid, pool.gpu_class, free_mb) for _ in range(count))
    return gpus


def _need_mb(weight_bytes: int, kv_headroom_pct: int, gpus: int) -> int:
    """VRAM the model needs on ``gpus`` GPUs: weights plus KV headroom plus per-GPU context."""
    weights_mb = math.ceil(weight_bytes * (1 + kv_headroom_pct / 100) / 2**20)
    return weights_mb + gpus * RPC_KV_CTX_OVERHEAD_MB


def _greedy(
    gpus: list[RpcGpu], weight_bytes: int, kv_headroom_pct: int
) -> tuple[list[RpcGpu], bool]:
    """Take GPUs (largest VRAM, then fullest node) until the need is met or the cap is hit.

    Returns the chosen GPUs and whether they satisfy the need. Preferring fuller nodes at equal
    VRAM keeps the node count (and so the cross-node slowdown) down.
    """
    per_node: dict[str, int] = {}
    for gpu in gpus:
        per_node[gpu.node_uid] = per_node.get(gpu.node_uid, 0) + 1
    ordered = sorted(gpus, key=lambda g: (-g.vram_free_mb, -per_node[g.node_uid], g.node_uid))
    chosen: list[RpcGpu] = []
    total = 0
    for gpu in ordered[:MAX_RPC_GPUS]:
        chosen.append(gpu)
        total += gpu.vram_free_mb
        if total >= _need_mb(weight_bytes, kv_headroom_pct, len(chosen)):
            return chosen, True
    return chosen, False


def _candidate_sets(gpus: list[RpcGpu]) -> list[tuple[bool, list[RpcGpu]]]:
    """Per-class GPU sets (mixed=False) plus the cross-class union; refusal is the caller's call."""
    by_class: dict[str, list[RpcGpu]] = {}
    for gpu in gpus:
        by_class.setdefault(gpu.gpu_class, []).append(gpu)
    sets = [(False, members) for _cls, members in sorted(by_class.items())]
    if len(by_class) > 1:
        sets.append((True, gpus))
    return sets


def _split_shares(free: list[int]) -> list[float]:
    """VRAM-proportional shares at 4 dp; the last is the remainder so the sum is exactly 1.0."""
    total = sum(free)
    shares = [round(f / total, 4) for f in free[:-1]]
    shares.append(round(1 - sum(shares), 4))
    return shares


def _assemble(vendor: int, chosen: list[RpcGpu], need_mb: int, explanation_tail: str) -> RpcLayout:
    """Pick the head node, order the devices (workers first) and compute the tensor split."""
    per_node: dict[str, list[RpcGpu]] = {}
    for gpu in chosen:
        per_node.setdefault(gpu.node_uid, []).append(gpu)
    head_uid = sorted(per_node, key=lambda uid: (-len(per_node[uid]), uid))[0]
    head_pool = per_node[head_uid]
    class_counts: dict[str, int] = {}
    for gpu in head_pool:
        class_counts[gpu.gpu_class] = class_counts.get(gpu.gpu_class, 0) + 1
    head_class = sorted(class_counts, key=lambda c: (-class_counts[c], c))[0]
    head_gpus = [g for g in head_pool if g.gpu_class == head_class]
    # identity, not equality: identical GPUs on one node are distinct devices
    head_ids = {id(g) for g in head_gpus}
    worker_gpus = sorted(
        (g for g in chosen if id(g) not in head_ids), key=lambda g: (g.node_uid, -g.vram_free_mb)
    )
    devices = worker_gpus + head_gpus
    shares = _split_shares([g.vram_free_mb for g in devices])
    workers = [
        RpcWorker(g.node_uid, g.gpu_class, g.vram_free_mb, share)
        for g, share in zip(worker_gpus, shares, strict=False)
    ]
    available = sum(g.vram_free_mb for g in chosen)
    return RpcLayout(
        vendor=vendor,
        head_node_uid=head_uid,
        head_gpu_class=head_class,
        head_gpus=len(head_gpus),
        workers=workers,
        tensor_split=shares,
        vram_needed_mb=need_mb,
        vram_available_mb=available,
        nodes_used=len(per_node),
        explanation=explanation_tail,
    )


def plan_rpc_layout(
    *,
    weight_bytes: int,
    pools: list[Pool],
    allow_mixed_class: bool,
    kv_headroom_pct: int = 20,
    mutual_auth_enabled: bool,
    internal_tls_enabled: bool,
) -> RpcLayout:
    """Plan a one-vendor head + rpc-server layout for a GGUF of ``weight_bytes``.

    Refuses (``RpcRefused``) when mutual auth is off, vendors are mixed, classes are mixed
    without consent, there is no inventory, or the model does not fit in ``MAX_RPC_GPUS`` GPUs.
    ``internal_tls_enabled`` is only logged: rpc-server has no TLS, so it never satisfies auth.
    """
    if not mutual_auth_enabled:
        logger.warning(
            "llama.cpp RPC layout refused: mutual auth disabled (internal_tls=%s)",
            internal_tls_enabled,
        )
        raise RpcRefused(RPC_AUTH_REFUSAL)
    vendors = sorted({pool.vendor for pool in pools})
    if len(vendors) > 1:
        names = " and ".join(_VENDOR_NAMES.get(v, str(v)) for v in vendors)
        raise RpcRefused(f"RPC layouts cannot mix GPU vendors ({names}). Choose one vendor's pool.")
    gpus = _expand(pools)
    if not gpus:
        raise RpcRefused(
            "No GPU inventory for this vendor. The GPU inventory collector must run first."
        )
    vendor = vendors[0]

    fitting: list[tuple[tuple[object, ...], bool, list[RpcGpu]]] = []
    best_miss: tuple[int, int, list[RpcGpu]] | None = None  # (available, need, gpus)
    for mixed, members in _candidate_sets(gpus):
        chosen, ok = _greedy(members, weight_bytes, kv_headroom_pct)
        if ok:
            nodes = len({g.node_uid for g in chosen})
            spare = sum(g.vram_free_mb for g in chosen) - _need_mb(
                weight_bytes, kv_headroom_pct, len(chosen)
            )
            labels = tuple(sorted({g.gpu_class for g in chosen}))
            fitting.append(((nodes, len(chosen), -spare, labels), mixed, chosen))
        else:
            avail = sum(g.vram_free_mb for g in chosen)
            if best_miss is None or avail > best_miss[0]:
                best_miss = (avail, _need_mb(weight_bytes, kv_headroom_pct, len(chosen)), chosen)

    usable = [f for f in fitting if allow_mixed_class or not f[1]]
    if not usable:
        if fitting:  # only a mixed-class set fits and the admin has not allowed it
            classes = sorted({g.gpu_class for g in fitting[0][2]})
            raise RpcRefused(
                f"Layout mixes GPU classes {classes[0]} and {classes[-1]}. "
                "Enable allow_mixed_class to split by VRAM across both."
            )
        avail_mb, need_mb, _ = best_miss or (0, _need_mb(weight_bytes, kv_headroom_pct, 1), [])
        raise RpcRefused(
            f"Does not fit: needs {need_mb / 1024:.1f} GB; the best one-vendor pool offers "
            f"{avail_mb / 1024:.1f} GB across up to {MAX_RPC_GPUS} GPUs. "
            "Options: a smaller quantization, or more GPUs."
        )

    _rank, mixed, chosen = min(usable, key=lambda f: f[0])
    need_mb = _need_mb(weight_bytes, kv_headroom_pct, len(chosen))
    layout = _assemble(vendor, chosen, need_mb, "")
    if layout.nodes_used == 1:
        layout.workers = []
        layout.tensor_split = _split_shares([g.vram_free_mb for g in chosen])
        layout.head_gpus = len(chosen)
        layout.explanation = "Fits on one node. Use the single-node topology; RPC is not needed."
        return layout
    classes = sorted({g.gpu_class for g in chosen})
    mix = f" mixing {classes[0]} and {classes[-1]} GPUs" if mixed else ""
    layout.explanation = (
        f"Fits across {layout.nodes_used} nodes{mix} ({need_mb / 1024:.0f} GB of "
        f"{layout.vram_available_mb / 1024:.0f} GB). Layers split by VRAM. "
        f"RPC is {RPC_SLOWDOWN_COPY}."
    )
    return layout
