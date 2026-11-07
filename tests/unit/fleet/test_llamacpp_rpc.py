"""Plan F, F2: llama.cpp RPC layout planner."""

from __future__ import annotations

import random

import pytest

from shared.fleet.llamacpp_rpc import (
    MAX_RPC_GPUS,
    RPC_AUTH_REFUSAL,
    RPC_SLOWDOWN_COPY,
    RpcRefused,
    plan_rpc_layout,
)
from shared.fleet.vllm_ray.fit import Pool

NV, AMD = 1, 2
W120 = 65 * 2**30  # 120B Q4 GGUF size fixture (bytes)
AUTH = {"mutual_auth_enabled": True, "internal_tls_enabled": False}


def plan(pools, **kw):
    """Plan with auth on; ``w`` overrides the weight bytes and ``mixed`` the mixed-class flag."""
    return plan_rpc_layout(
        weight_bytes=kw.pop("w", W120), pools=pools, allow_mixed_class=kw.pop("mixed", False),
        **AUTH, **kw,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("nodes", "cls", "want_nodes"),
    [
        ([("n1", 2, 24000), ("n2", 2, 24000)], "nvidia-rtx4090", 2),
        ([("n1", 1, 32000), ("n2", 1, 32000), ("n3", 1, 32000)], "nvidia-rtx5090", 3),
    ],
)
def test_120b_q4_fits_across_nodes(nodes, cls, want_nodes):
    """A 65 GiB GGUF spans 2-3 nodes; workers + head GPUs cover every selected GPU."""
    lay = plan([Pool(NV, cls, nodes)])
    assert lay.nodes_used == want_nodes
    assert len(lay.workers) == want_nodes - 1 + (lay.head_gpus - 1)
    assert round(sum(lay.tensor_split), 4) == 1.0 and RPC_SLOWDOWN_COPY in lay.explanation
    assert len(lay.tensor_split) == len(lay.workers) + lay.head_gpus


def test_no_fit_refused():
    """No fit refused."""
    with pytest.raises(RpcRefused, match="Does not fit"):
        plan([Pool(NV, "nvidia-rtx4090", [("n1", 1, 24000), ("n2", 1, 24000)])])


def test_no_fit_message_reports_best_pool():
    """No fit message reports best pool."""
    with pytest.raises(RpcRefused) as exc:
        plan([Pool(NV, "nvidia-rtx4090", [("n1", 1, 24000)])])
    assert "offers 23.4 GB across up to 8 GPUs" in str(exc.value)


def test_mixed_class_refused_unless_allowed():
    # Deviation from the plan fixture: 2x4090(24 GB)+2x4070(12 GB) is only 70 GB < 78 GB needed.
    """Mixed class refused unless allowed."""
    pools = [
        Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)]),
        Pool(NV, "nvidia-rtx4070", [("n2", 4, 12000)]),
    ]
    with pytest.raises(RpcRefused, match="mixes GPU classes"):
        plan(pools)
    lay = plan(pools, mixed=True)
    assert "mixing" in lay.explanation and round(sum(lay.tensor_split), 4) == 1.0
    assert {lay.head_gpu_class} <= {"nvidia-rtx4090", "nvidia-rtx4070"}


def test_single_class_preferred_over_mixed_when_it_fits():
    """Single class preferred over mixed when it fits."""
    pools = [
        Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000), ("n2", 2, 24000)]),
        Pool(NV, "nvidia-rtx4070", [("n3", 1, 12000)]),
    ]
    assert "mixing" not in plan(pools, mixed=True).explanation


def test_cross_vendor_refused():
    """Cross vendor refused."""
    with pytest.raises(RpcRefused, match="cannot mix GPU vendors"):
        plan([Pool(NV, "a", [("n1", 1, 1)]), Pool(AMD, "b", [("n2", 1, 1)])], mixed=True)


def test_auth_disabled_refused_with_spec_string():
    """Auth disabled refused with spec string."""
    with pytest.raises(RpcRefused) as exc:
        plan_rpc_layout(
            weight_bytes=W120, pools=[Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)])],
            allow_mixed_class=False, mutual_auth_enabled=False, internal_tls_enabled=True,
        )  # fmt: skip
    assert str(exc.value) == RPC_AUTH_REFUSAL


def test_no_inventory_refused():
    """No inventory refused."""
    with pytest.raises(RpcRefused, match="inventory collector"):
        plan([])
    with pytest.raises(RpcRefused, match="inventory collector"):
        plan([Pool(NV, "nvidia-rtx4090", [("n1", 2, 0)])])


def test_fits_on_one_node_has_no_workers():
    """Fits on one node has no workers."""
    lay = plan([Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)])], w=8 * 2**30)
    assert lay.workers == [] and lay.explanation.startswith("Fits on one node")
    assert lay.head_gpus == 1 and lay.tensor_split == [1.0]


def test_head_has_gpus_and_is_most_loaded():
    """Head has gpus and is most loaded."""
    lay = plan([Pool(NV, "nvidia-rtx4090", [("n1", 1, 24000), ("n2", 3, 24000)])])
    assert lay.head_gpus >= 1 and lay.head_node_uid == "n2"


def test_head_tie_breaks_on_node_uid():
    """Head tie breaks on node uid."""
    lay = plan([Pool(NV, "nvidia-rtx4090", [("n2", 2, 24000), ("n1", 2, 24000)])])
    assert lay.head_node_uid == "n1" and lay.head_gpus == 2


def test_gpu_cap_enforced():
    """Gpu cap enforced."""
    pools = [Pool(NV, "nvidia-rtx4090", [(f"n{i}", 1, 8000) for i in range(12)])]
    with pytest.raises(RpcRefused, match=f"up to {MAX_RPC_GPUS} GPUs"):
        plan(pools)


def test_tensor_split_sums_to_one():
    """Tensor split sums to one."""
    rng = random.Random(1)  # noqa: S311 -- seeded test data, not crypto
    planned = 0
    for _ in range(50):
        nodes = [
            (f"n{i}", rng.randint(1, 3), rng.choice([12000, 16000, 24000, 32000])) for i in range(4)
        ]
        try:
            lay = plan([Pool(NV, "nvidia-rtx4090", nodes)])
        except RpcRefused:
            continue
        planned += 1
        assert abs(sum(lay.tensor_split) - 1.0) < 1e-9 and all(s > 0 for s in lay.tensor_split)
    assert planned > 10  # the property was actually exercised


def test_mixed_head_node_with_two_classes_uses_single_head_class():
    """A head node holding two classes keeps one class on the head, the rest become workers."""
    pools = [
        Pool(NV, "nvidia-rtx4090", [("n1", 2, 24000)]),
        Pool(NV, "nvidia-rtx4070", [("n1", 2, 12000), ("n2", 4, 12000)]),
    ]
    lay = plan(pools, mixed=True)
    assert (lay.head_node_uid, lay.head_gpu_class, lay.head_gpus) == ("n1", "nvidia-rtx4070", 2)
    # the two 4090s on the head node became workers (one class per head container)
    assert (
        sorted((w.node_uid, w.gpu_class) for w in lay.workers).count(("n1", "nvidia-rtx4090")) == 2
    )
    assert len(lay.tensor_split) == len(lay.workers) + lay.head_gpus
