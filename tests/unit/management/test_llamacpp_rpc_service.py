"""Plan F, F5: llama.cpp RPC orchestration helpers (inventory pools, images, stored layouts)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.management.app.services import llamacpp_rpc_service as svc
from shared.fleet.llamacpp_rpc import RpcLayout, RpcRefused, RpcWorker
from shared.fleet.pricing import GPU_CLASSES_LABEL
from tests.unit.management.vllm_harness import seed_backend, seed_gpus

GIB65 = 65 * 2**30


@pytest.fixture
def inv(vllm_db):
    """Org 1: backend with node-a (2x4090) and node-b (2x4090); org 2: its own backend + GPUs."""
    backend = seed_backend(vllm_db)
    seed_gpus(vllm_db, backend, node="node-a", count=2, vram_mb=24564)
    seed_gpus(vllm_db, backend, node="node-b", count=2, vram_mb=24564)
    other = seed_backend(vllm_db, org_id=2, name="other")
    seed_gpus(vllm_db, other, org_id=2, node="node-z", count=8, vram_mb=80000)
    return SimpleNamespace(db=vllm_db, backend=backend, other=other)


def _dep(**over):
    """A stored RPC deployment row stand-in."""
    values = {
        "rpc_head_node_uid": "uid-node-a", "node_selector": {GPU_CLASSES_LABEL: "nvidia-rtx4090"},
        "tensor_split": "0.25,0.25,0.25,0.25", "gpu_count": 2,
        "rpc_workers": [
            {"node_uid": "uid-node-b", "gpu_class": "nvidia-rtx4090", "gpu_count": 1,
             "tensor_split_share": 0.25},
            {"node_uid": "uid-node-b", "gpu_class": "nvidia-rtx4090", "gpu_count": 1,
             "tensor_split_share": 0.25},
        ],
    }  # fmt: skip
    values.update(over)
    return SimpleNamespace(**values)


def test_backend_for_org_is_tenant_scoped(inv):
    """Each org gets its own backend; a disabled backend or an org without one gets None."""
    assert svc.backend_for_org(inv.db, 1).id == inv.backend
    assert svc.backend_for_org(inv.db, 2).id == inv.other
    assert svc.backend_for_org(inv.db, 3) is None
    inv.db(inv.db.fleet_backends.id == inv.backend).update(status="disabled")
    assert svc.backend_for_org(inv.db, 1) is None


def test_pools_only_contain_the_orgs_schedulable_warm_gpus(inv):
    """Org 1's pool never includes org 2's GPUs; unschedulable and cold GPUs are dropped."""
    inv.db(inv.db.gpu_inventory.node_name == "node-b").update(schedulable=False)
    (pool,) = svc.pools_from_inventory(inv.db, inv.backend, 1)
    assert pool.nodes == [("uid-node-a", 2, 24564)]
    inv.db(inv.db.gpu_inventory.node_name == "node-b").update(schedulable=True, state=2)
    assert svc.pools_from_inventory(inv.db, inv.backend, 1)[0].nodes == [("uid-node-a", 2, 24564)]
    assert svc.pools_from_inventory(inv.db, inv.other, 1) == []  # org 1 cannot read org 2's rows


def test_allowed_restricts_gpus_by_budget(inv):
    """``allowed`` keeps at most as many GPUs per (node, class) as requested."""
    allowed = [("uid-node-a", "nvidia-rtx4090")] * 3 + [("uid-node-b", "nvidia-rtx4090")]
    (pool,) = svc.pools_from_inventory(inv.db, inv.backend, 1, allowed)
    assert sorted(pool.nodes) == [("uid-node-a", 2, 24564), ("uid-node-b", 1, 24564)]
    assert svc.pools_from_inventory(inv.db, inv.backend, 1, [("uid-node-q", "x")]) == []


def test_plan_for_org_returns_layout_backend_and_hostnames(inv):
    """A fitting model plans across two nodes with the Kubernetes hostnames of both."""
    layout, backend_id, hostnames = svc.plan_for_org(
        inv.db, 1, gguf_bytes=GIB65, allow_mixed_class=False, allowed=None, mutual_auth_enabled=True
    )
    assert backend_id == inv.backend and layout.nodes_used == 2
    assert hostnames == {"uid-node-a": "node-a", "uid-node-b": "node-b"}


def test_plan_for_org_without_backend_or_auth_refuses(inv):
    """No backend -> the collector message; auth off -> the planner's refusal."""
    with pytest.raises(RpcRefused, match="inventory collector"):
        svc.plan_for_org(
            inv.db, 3, gguf_bytes=GIB65, allow_mixed_class=False, allowed=None,
            mutual_auth_enabled=True,
        )  # fmt: skip
    with pytest.raises(RpcRefused, match="mutual auth"):
        svc.plan_for_org(
            inv.db, 1, gguf_bytes=GIB65, allow_mixed_class=False, allowed=None,
            mutual_auth_enabled=False,
        )  # fmt: skip


def test_hostnames_omit_unknown_nodes(inv):
    """Only nodes in the org's inventory resolve to hostnames."""
    assert svc.hostnames_for(inv.db, 1, inv.backend, {"uid-node-a", "uid-node-z"}) == {
        "uid-node-a": "node-a"
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [(None, "is not set"), ("  ", "is not set"), ("ghcr.io/x/y:cuda", "digest-pinned")],
)
def test_rpc_image_refusals(monkeypatch, value, message):
    """A missing, blank or tag-only image variable is refused, naming the variable."""
    if value is None:
        monkeypatch.delenv("WADDLEAI_LLAMACPP_RPC_IMAGE_ROCM", raising=False)
    else:
        monkeypatch.setenv("WADDLEAI_LLAMACPP_RPC_IMAGE_ROCM", value)
    with pytest.raises(svc.RpcImageNotConfigured, match=message) as exc:
        svc.rpc_image_for_vendor(2)
    assert "WADDLEAI_LLAMACPP_RPC_IMAGE_ROCM" in str(exc.value)


def test_rpc_image_for_each_vendor_and_unknown(monkeypatch):
    """Each vendor reads its own variable; an unknown vendor has none."""
    pinned = "r/i:t@sha256:" + "c" * 64
    for vendor, var in svc.IMAGE_ENV_BY_VENDOR.items():
        monkeypatch.setenv(var, pinned)
        assert svc.rpc_image_for_vendor(vendor) == pinned
    with pytest.raises(svc.RpcImageNotConfigured, match="no RPC image variable"):
        svc.rpc_image_for_vendor(9)


def test_internal_tls_flag_from_env(monkeypatch):
    """The chart's internal-TLS flag is read as a boolean (and only logged by the planner)."""
    monkeypatch.setenv("WADDLEAI_FLEET_RAY_INTERNAL_TLS", "true")
    assert svc.internal_tls_enabled() is True
    monkeypatch.setenv("WADDLEAI_FLEET_RAY_INTERNAL_TLS", "no")
    assert svc.internal_tls_enabled() is False


def test_layout_columns_round_trip(inv):
    """Persisted columns rebuild the same head, workers, split and node count."""
    layout, _backend, hostnames = svc.plan_for_org(
        inv.db, 1, gguf_bytes=GIB65, allow_mixed_class=False, allowed=None, mutual_auth_enabled=True
    )
    cols = svc.layout_columns(layout)
    assert cols["topology"] == 2 and cols["rpc_head_node_uid"] == layout.head_node_uid
    assert cols["node_selector"] == {GPU_CLASSES_LABEL: layout.head_gpu_class}
    dep = SimpleNamespace(**cols)
    rebuilt, rebuilt_hosts = svc.layout_from_row(inv.db, dep, 1)
    assert (rebuilt.head_node_uid, rebuilt.head_gpus, rebuilt.nodes_used) == (
        layout.head_node_uid, layout.head_gpus, layout.nodes_used,
    )  # fmt: skip
    assert rebuilt.tensor_split == layout.tensor_split and rebuilt.vendor == layout.vendor
    assert [(w.node_uid, w.gpu_class) for w in rebuilt.workers] == [
        (w.node_uid, w.gpu_class) for w in layout.workers
    ]
    assert rebuilt_hosts == hostnames


def test_single_node_columns_clear_rpc_state():
    """Going single-node nulls the worker list, split and head and sets topology 1."""
    assert svc.single_node_columns() == {
        "topology": 1, "rpc_workers": None, "tensor_split": None, "rpc_head_node_uid": None,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"rpc_head_node_uid": None}, "no stored RPC layout"),
        ({"tensor_split": None}, "no stored RPC layout"),
        ({"node_selector": {}}, "no stored RPC layout"),
        ({"rpc_head_node_uid": "uid-gone"}, "left the GPU inventory"),
    ],
)
def test_layout_from_row_refuses_unresolvable_rows(inv, over, message):
    """A row without a layout, or whose node left the inventory, is refused, never guessed."""
    with pytest.raises(svc.RpcPlacementUnavailable, match=message):
        svc.layout_from_row(inv.db, _dep(**over), 1)


def test_layout_from_row_no_org_and_no_backend_and_cross_org(inv):
    """No owner, no backend, or another org's inventory all refuse."""
    with pytest.raises(svc.RpcPlacementUnavailable, match="no owning organization"):
        svc.layout_from_row(inv.db, _dep(), None)
    with pytest.raises(svc.RpcPlacementUnavailable):
        svc.layout_from_row(inv.db, _dep(), 3)
    with pytest.raises(svc.RpcPlacementUnavailable, match="left the GPU inventory"):
        svc.layout_from_row(inv.db, _dep(), 2)  # org 2's inventory has none of org 1's nodes


def test_layout_from_row_head_class_missing_from_inventory(inv):
    """The head's class must still exist on the head node."""
    dep = _dep(node_selector={GPU_CLASSES_LABEL: "nvidia-rtx3090"})
    with pytest.raises(svc.RpcPlacementUnavailable, match="head node's GPUs"):
        svc.layout_from_row(inv.db, dep, 1)


def test_rpc_workers_json_shape():
    """Each worker pod is one GPU with its share."""
    layout = RpcLayout(1, "h", "c", 1, [RpcWorker("n", "c", 10, 0.5)], [0.5, 0.5], 1, 2, 2, "x")
    assert svc.rpc_workers_json(layout) == [
        {"node_uid": "n", "gpu_class": "c", "gpu_count": 1, "tensor_split_share": 0.5}
    ]


def test_base_name_is_unique_per_public_id_and_fits_dns():
    """Regression (tenant collision): the same name with two public ids never collides."""
    a = svc.rpc_base_name("shared-name", "11111111-1111-1111-1111-111111111111")
    b = svc.rpc_base_name("shared-name", "22222222-2222-2222-2222-222222222222")
    assert a != b and a.startswith("llamacpp-shared-name-") and len(a) <= 48
    assert len(svc.rpc_base_name("x" * 60, "11111111-1111-1111-1111-111111111111")) <= 48


def test_base_name_taken_checks_every_org_and_honours_exclude(inv):
    """A name used by ANY org is taken; the row's own id can be excluded."""
    row_id = inv.db.llamacpp_deployments.insert(
        name="n", deployment_type="kubernetes", status="stopped", model_name="m",
        public_id="33333333-3333-3333-3333-333333333333", owner_org_id=2,
        k8s_daemonset_name="llamacpp-n-333333333333",
    )  # fmt: skip
    assert svc.base_name_taken(inv.db, "llamacpp-n-333333333333") is True
    assert svc.base_name_taken(inv.db, "llamacpp-n-333333333333", exclude_id=row_id) is False
    assert svc.base_name_taken(inv.db, "llamacpp-other") is False
