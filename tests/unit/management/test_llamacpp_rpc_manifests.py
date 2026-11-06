"""Plan F, F3: llama.cpp RPC head/worker/Service/CNP rendering and deploy/remove."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.management.app.services.fleet_secrets import DEPLOYMENT_KEYS_SECRET
from services.management.app.services.llamacpp_manager import (
    LLAMACPP_IMAGE,
    LlamaCppManager,
    resource_for,
)
from shared.fleet.llamacpp_rpc import RPC_AUTH_REFUSAL, RpcLayout, RpcRefused, RpcWorker
from shared.fleet.vllm_ray.slug import derive_slug

IMG = "ghcr.io/penguintechinc/waddleai/llamacpp-rpc:cuda-0123456@sha256:" + "a" * 64
HOSTS = {"n1": "gpu-a1", "n2": "gpu-a2"}
MGR = "services.management.app.services.llamacpp_manager"
PUBLIC_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def layout() -> RpcLayout:
    """Head n1 with 2 RTX 4090s; two single-GPU workers on n2; 4 devices, split 4 x 0.25."""
    ws = [
        RpcWorker("n2", "nvidia-rtx4090", 23000, 0.25),
        RpcWorker("n2", "nvidia-rtx4090", 23000, 0.25),
    ]
    return RpcLayout(1, "n1", "nvidia-rtx4090", 2, ws, [0.25] * 4, 80000, 92000, 2, "x")


@pytest.fixture
def mgr():
    """A manager with mutual auth on."""
    return LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": True})


@pytest.fixture
def dep():
    """An RPC deployment row stand-in."""
    return SimpleNamespace(
        id=7, name="qwen-rpc", k8s_namespace="waddleai", model_filename="m.gguf",
        model_url="https://h.example/m.gguf", n_ctx=8192, status="pending",
        public_id=PUBLIC_ID, k8s_daemonset_name=None, topology=2,
    )  # fmt: skip


def kind(mgr, dep, k):
    """Rendered manifests of one kind."""
    return [d for d in mgr.export_rpc_manifests(dep, layout(), HOSTS, IMG, 8192) if d["kind"] == k]


def test_head_pinned_to_planned_node(mgr, dep):
    """Head Deployment is pinned to the planned hostname and requests the planned GPU count."""
    spec = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "gpu-a1"}
    assert spec["containers"][0]["resources"]["limits"]["nvidia.com/rtx4090"] == "2"


def test_head_args(mgr, dep):
    """Head args carry the worker list, split, 999 layers and the per-deployment key file."""
    args = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert len(args[args.index("--rpc") + 1].split(",")) == 2
    assert args[args.index("--n-gpu-layers") + 1] == "999"
    assert args[args.index("--tensor-split") + 1] == "0.25,0.25,0.25,0.25"
    slug = derive_slug(dep.name, PUBLIC_ID)
    assert args[args.index("--api-key-file") + 1] == f"/secrets/deployment-keys/{slug}"
    assert args[args.index("--ctx-size") + 1] == "8192"


def test_rpc_endpoints_follow_statefulset_pod_dns(mgr, dep):
    """Each --rpc entry is <sts>-<ordinal>.<headless svc>.<ns>.svc:50052 in device order."""
    args = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["args"]
    base = mgr._daemonset_name(dep.name)
    assert args[args.index("--rpc") + 1].split(",") == [
        f"{base}-rpc-0-0.{base}-rpc.waddleai.svc:50052",
        f"{base}-rpc-0-1.{base}-rpc.waddleai.svc:50052",
    ]


def test_head_mounts_only_its_own_key(mgr, dep):
    """The shared deployment-keys Secret is projected with items=[own slug], never wholesale."""
    pod = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]
    secret_vol = next(v for v in pod["volumes"] if v["name"] == "deployment-key")["secret"]
    slug = derive_slug(dep.name, PUBLIC_ID)
    assert secret_vol == {
        "secretName": DEPLOYMENT_KEYS_SECRET,
        "items": [{"key": slug, "path": slug}],
    }
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "deployment-key")
    assert mount["readOnly"] is True


def test_one_statefulset_per_worker_node(mgr, dep):
    """Two workers on one node become one StatefulSet pinned to that node."""
    sts = kind(mgr, dep, "StatefulSet")
    assert len(sts) == 1 and sts[0]["spec"]["replicas"] == 2
    assert sts[0]["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/hostname": "gpu-a2"
    }


def test_worker_runs_rpc_server_hardened(mgr, dep):
    """rpc-server binds in-pod, one GPU, rootless with a read-only rootfs and PSA seccomp."""
    sts = kind(mgr, dep, "StatefulSet")[0]
    pod = sts["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["args"][:2] == ["-H", "0.0.0.0"]  # noqa: S104 -- asserting the in-pod bind address
    assert c["args"][c["args"].index("-p") + 1] == "50052"
    assert c["args"][c["args"].index("-d") + 1] == "CUDA0"
    assert c["resources"]["limits"]["nvidia.com/rtx4090"] == "1"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}


def test_worker_ingress_only_from_head(mgr, dep):
    """The worker CNP admits only this deployment's head, with mutual auth required."""
    cnp = kind(mgr, dep, "CiliumNetworkPolicy")[0]
    base = mgr._daemonset_name(dep.name)
    assert cnp["metadata"]["name"] == f"{base}-rpc-allow"
    assert (
        cnp["spec"]["endpointSelector"]["matchLabels"]["waddleai.penguintech.io/llamacpp-role"]
        == "worker"
    )
    rule = cnp["spec"]["ingress"][0]
    assert rule["fromEndpoints"] == [
        {
            "matchLabels": {
                "waddleai.penguintech.io/llamacpp-deployment": base,
                "waddleai.penguintech.io/llamacpp-role": "head",
                "waddleai.penguintech.io/llamacpp-public-id": str(PUBLIC_ID),
            }
        }
    ]
    assert rule["authentication"] == {"mode": "required"}
    assert rule["toPorts"] == [{"ports": [{"port": "50052", "protocol": "TCP"}]}]


def test_head_policy_ingress_egress(mgr, dep):
    """Head CNP: proxy+management in on 8080 (mutual auth); DNS, workers, model host out."""
    head = kind(mgr, dep, "CiliumNetworkPolicy")[1]["spec"]
    ingress = head["ingress"][0]
    assert ingress["authentication"] == {"mode": "required"}
    assert ingress["toPorts"] == [{"ports": [{"port": "8080", "protocol": "TCP"}]}]
    assert len(ingress["fromEndpoints"]) == 2
    kinds = [tuple(sorted(rule)) for rule in head["egress"]]
    assert ("toEndpoints", "toPorts") in kinds
    worker_rule = next(r for r in head["egress"] if r.get("authentication"))
    assert worker_rule["toPorts"] == [{"ports": [{"port": "50052", "protocol": "TCP"}]}]
    fqdn_rule = next(r for r in head["egress"] if "toFQDNs" in r)
    assert fqdn_rule["toFQDNs"] == [{"matchName": "h.example"}]


def test_head_policy_huggingface_cdn(mgr, dep):
    """A huggingface.co model URL also admits the *.hf.co CDN the weights redirect to."""
    dep.model_url = "https://huggingface.co/o/r/resolve/main/m.gguf"
    head = kind(mgr, dep, "CiliumNetworkPolicy")[1]["spec"]
    fqdns = next(r for r in head["egress"] if "toFQDNs" in r)["toFQDNs"]
    assert {"matchPattern": "*.hf.co"} in fqdns


def test_head_policy_without_model_url_has_no_fqdn_egress(mgr, dep):
    """No model_url means no internet egress rule."""
    dep.model_url = None
    head = kind(mgr, dep, "CiliumNetworkPolicy")[1]["spec"]
    assert all("toFQDNs" not in r for r in head["egress"])


def test_topology_selectors_from_env(mgr, dep, monkeypatch):
    """The chart's CILIUM_TOPOLOGY selectors win over the shipped defaults; junk falls back."""
    monkeypatch.setenv(
        "CILIUM_TOPOLOGY",
        '{"selectors": {"aiproxy": {"app": "px"}, "management": {"app": "mg"}}}',
    )
    ingress = kind(mgr, dep, "CiliumNetworkPolicy")[1]["spec"]["ingress"][0]
    assert ingress["fromEndpoints"] == [
        {"matchLabels": {"app": "px"}},
        {"matchLabels": {"app": "mg"}},
    ]
    monkeypatch.setenv("CILIUM_TOPOLOGY", "{not json")
    fallback = kind(mgr, dep, "CiliumNetworkPolicy")[1]["spec"]["ingress"][0]
    assert fallback["fromEndpoints"][0]["matchLabels"]["app.kubernetes.io/component"] == "proxy"


def test_services_not_exposed(mgr, dep):
    """Both Services are ClusterIP/headless; the headless one publishes not-ready addresses."""
    services = {s["metadata"]["name"]: s for s in kind(mgr, dep, "Service")}
    base = mgr._daemonset_name(dep.name)
    assert services[f"{base}-svc"]["spec"]["type"] == "ClusterIP"
    assert services[f"{base}-rpc"]["spec"]["clusterIP"] == "None"
    for svc in services.values():
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"
        assert "nodePort" not in str(svc)


def test_head_image_is_rpc_image(mgr, dep):
    """Head and workers use the digest-pinned RPC image, never the upstream llama.cpp one."""
    head = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["image"]
    sts = kind(mgr, dep, "StatefulSet")[0]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert head == sts == IMG and head != LLAMACPP_IMAGE


def test_tag_only_image_refused(mgr, dep):
    """A tag-only image is refused: digest pinning is mandatory."""
    with pytest.raises(ValueError, match="pinned by digest"):
        mgr.export_rpc_manifests(dep, layout(), HOSTS, "ghcr.io/x/y:cuda", 8192)


def test_head_init_waits_for_workers_with_bounded_loop(mgr, dep):
    """The head's init container waits on every worker FQDN and gives up loudly after ~20 min."""
    init = kind(mgr, dep, "Deployment")[0]["spec"]["template"]["spec"]["initContainers"]
    wait = next(c for c in init if c["name"] == "wait-workers")
    assert len(wait["env"][0]["value"].split()) == 2
    assert "exit 1" in wait["command"][2] and "telnet://$w" in wait["command"][2]
    assert [c["name"] for c in init] == ["download-model", "wait-workers"]


def test_mutual_auth_disabled_no_k8s_calls(dep):
    """With mutual auth off the deploy is refused before any Kubernetes client is built."""
    m = LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": False})
    with patch(f"{MGR}.get_k8s_apps_client") as apps:
        with pytest.raises(RpcRefused) as exc:
            m.deploy_rpc(dep, layout(), HOSTS, IMG)
    assert str(exc.value) == RPC_AUTH_REFUSAL and apps.call_count == 0
    with pytest.raises(RpcRefused):
        m.export_rpc_manifests(dep, layout(), HOSTS, IMG, 8192)


@pytest.mark.parametrize(
    ("env", "expected"),
    [("true", True), ("TRUE", True), ("false", False), ("", False), (None, False)],
)
def test_mutual_auth_defaults_from_chart_env_failing_closed(monkeypatch, env, expected):
    """Without an explicit config value the chart's env decides; anything but true is off."""
    if env is None:
        monkeypatch.delenv("WADDLEAI_FLEET_MUTUAL_AUTH", raising=False)
    else:
        monkeypatch.setenv("WADDLEAI_FLEET_MUTUAL_AUTH", env)
    assert LlamaCppManager(db=MagicMock()).mutual_auth_enabled is expected
    # a truthy non-bool config value is not an opt-in
    assert (
        LlamaCppManager(db=MagicMock(), config={"mutual_auth_enabled": "yes"}).mutual_auth_enabled
        is False
    )


def test_tensor_split_length_mismatch_refused(mgr, dep):
    """A split whose length differs from the device count never renders."""
    bad = layout()
    bad.tensor_split = bad.tensor_split[:-1]
    with pytest.raises(ValueError, match="tensor_split length"):
        mgr.export_rpc_manifests(dep, bad, HOSTS, IMG, 8192)


def test_missing_hostname_and_long_name_refused(mgr, dep):
    """A node with no hostname mapping and an over-long name are both refused up front."""
    with pytest.raises(ValueError, match="no Kubernetes hostname"):
        mgr.export_rpc_manifests(dep, layout(), {"n1": "gpu-a1"}, IMG, 8192)
    dep.name = "x" * 40
    with pytest.raises(ValueError, match="too long"):
        mgr.export_rpc_manifests(dep, layout(), HOSTS, IMG, 8192)


def test_mixed_class_workers_get_one_statefulset_per_class(mgr, dep):
    """Workers of two classes on one node cannot share a container resource: one STS each."""
    lay = layout()
    lay.workers = [
        RpcWorker("n2", "nvidia-rtx4070", 12000, 0.1),
        RpcWorker("n2", "nvidia-rtx4090", 23000, 0.2),
    ]
    lay.tensor_split = [0.1, 0.2, 0.35, 0.35]
    sts = [
        d
        for d in mgr.export_rpc_manifests(dep, lay, HOSTS, IMG, 8192)
        if d["kind"] == "StatefulSet"
    ]
    resources = {
        next(
            k
            for k in s["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
            if "." in k
        )
        for s in sts
    }
    assert len(sts) == 2 and resources == {"nvidia.com/rtx4070", "nvidia.com/rtx4090"}


@pytest.mark.parametrize(
    ("vendor", "cls", "expected"),
    [
        (1, "nvidia-rtx4090", "nvidia.com/rtx4090"),
        (1, "nvidia-rtx-pro-6000", "nvidia.com/rtx-pro-6000"),
        (2, "amd-instinct-mi300x", "amd.com/gpu"),
        (3, "intel-arc-b580", "gpu.intel.com/i915"),
    ],
)
def test_resource_for(vendor, cls, expected):
    """NVIDIA resources are per class; AMD and Intel use their plugin resource."""
    assert resource_for(vendor, cls) == expected


def test_resource_for_rejects_unknown():
    """An NVIDIA class without the vendor prefix, or an unknown vendor, is refused."""
    with pytest.raises(ValueError, match="cannot derive"):
        resource_for(1, "rtx4090")
    with pytest.raises(ValueError, match="unknown GPU vendor"):
        resource_for(9, "x")


def _clients(**overrides):
    """Patch all three Kubernetes client factories; returns the mocks."""
    apps, core, crd = MagicMock(), MagicMock(), MagicMock()
    for key, value in overrides.items():
        {"apps": apps, "core": core, "crd": crd}[key].configure_mock(**value)
    return apps, core, crd


def test_deploy_creates_policies_first_head_last_and_updates_row(mgr, dep):
    """Create order is CNPs, Services, StatefulSet, then the head; the row moves to deploying."""
    apps, core, crd = _clients()
    calls = MagicMock()
    calls.attach_mock(crd.create_namespaced_custom_object, "cnp")
    calls.attach_mock(core.create_namespaced_service, "svc")
    calls.attach_mock(apps.create_namespaced_stateful_set, "sts")
    calls.attach_mock(apps.create_namespaced_deployment, "head")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        mgr.deploy_rpc(dep, layout(), HOSTS, IMG)
    assert [c[0] for c in calls.mock_calls] == ["cnp", "cnp", "svc", "svc", "sts", "head"]
    update = mgr.db.return_value.update.call_args.kwargs
    base = mgr._daemonset_name(dep.name)
    assert update["status"] == "deploying"
    assert update["endpoint_url"] == f"http://{base}-svc.waddleai:8080"
    assert update["tensor_split"] == "0.25,0.25,0.25,0.25"


def test_deploy_rollback_on_failure(mgr, dep):
    """A failure mid-deploy deletes everything created so far and re-raises."""
    apps, core, crd = _clients()
    apps.create_namespaced_stateful_set.side_effect = RuntimeError("quota")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        with pytest.raises(RuntimeError, match="quota"):
            mgr.deploy_rpc(dep, layout(), HOSTS, IMG)
    assert core.delete_namespaced_service.call_count == 2
    assert crd.delete_namespaced_custom_object.call_count == 2
    apps.delete_namespaced_deployment.assert_not_called()
    mgr.db.return_value.update.assert_not_called()


def test_rollback_logs_and_continues_past_delete_failures(mgr, dep, caplog):
    """A failing rollback delete is logged and the remaining deletes still run."""
    apps, core, crd = _clients()
    apps.create_namespaced_stateful_set.side_effect = RuntimeError("quota")
    core.delete_namespaced_service.side_effect = RuntimeError("api down")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        with pytest.raises(RuntimeError, match="quota"):
            mgr.deploy_rpc(dep, layout(), HOSTS, IMG)
    assert "failed to delete Service" in caplog.text
    assert crd.delete_namespaced_custom_object.call_count == 2


def test_remove_rpc_deletes_everything_and_stops_row(mgr, dep):
    """Remove lists worker StatefulSets by label, deletes all objects and marks stopped."""
    apps, core, crd = _clients()
    sts = SimpleNamespace(metadata=SimpleNamespace(name="b-rpc-0"))
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(items=[sts])
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        mgr.remove_rpc(dep)
    apps.delete_namespaced_stateful_set.assert_called_once_with(
        name="b-rpc-0", namespace="waddleai"
    )
    apps.delete_namespaced_deployment.assert_called_once()
    assert core.delete_namespaced_service.call_count == 2
    assert crd.delete_namespaced_custom_object.call_count == 2
    assert mgr.db.return_value.update.call_args.kwargs == {"status": "stopped"}
    selector = apps.list_namespaced_stateful_set.call_args.kwargs["label_selector"]
    # base-only: legacy objects (created before the owner label) must still be found
    assert (
        selector == f"waddleai.penguintech.io/llamacpp-deployment={mgr._daemonset_name(dep.name)}"
    )


def test_remove_rpc_tolerates_404_but_reports_other_failures(mgr, dep):
    """Already-gone objects are fine; any other delete failure raises after trying the rest."""
    apps, core, crd = _clients()
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(items=[])
    gone = RuntimeError("gone")
    gone.status = 404
    crd.delete_namespaced_custom_object.side_effect = gone
    core.delete_namespaced_service.side_effect = RuntimeError("boom")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        with pytest.raises(RuntimeError, match="could not delete Service"):
            mgr.remove_rpc(dep)
    apps.delete_namespaced_deployment.assert_called_once()  # the rest still ran
    mgr.db.return_value.update.assert_not_called()


def test_remove_daemonset_dispatches_rpc_rows(mgr, dep):
    """The legacy remove entry point routes an RPC row to remove_rpc (never the DaemonSet path)."""
    with (
        patch.object(LlamaCppManager, "remove_rpc") as remove,
        patch(f"{MGR}.get_k8s_apps_client") as apps,
    ):
        mgr.remove_daemonset(dep, force=True)
    remove.assert_called_once_with(dep)
    apps.assert_not_called()


def test_get_k8s_custom_client_falls_back_to_kubeconfig():
    """Outside a cluster the custom client loads the kubeconfig."""
    from services.management.app.services import llamacpp_manager as module

    with (
        patch("kubernetes.config.load_incluster_config", side_effect=RuntimeError("no")),
        patch("kubernetes.config.load_kube_config") as kube,
        patch("kubernetes.client.CustomObjectsApi") as api,
    ):
        assert module.get_k8s_custom_client() is api.return_value
    kube.assert_called_once()


def test_every_object_and_selector_carries_the_owner_public_id(mgr, dep):
    """Regression (tenant collision): all objects and pod selectors are scoped by public_id."""
    docs = mgr.export_rpc_manifests(dep, layout(), HOSTS, IMG, 8192)
    owner = "waddleai.penguintech.io/llamacpp-public-id"
    for doc in docs:
        spec = doc["spec"]
        if doc["kind"] == "Service":
            assert spec["selector"][owner] == str(PUBLIC_ID)
        elif doc["kind"] == "CiliumNetworkPolicy":
            assert spec["endpointSelector"]["matchLabels"][owner] == str(PUBLIC_ID)
        else:
            assert spec["selector"]["matchLabels"][owner] == str(PUBLIC_ID)
            assert spec["template"]["metadata"]["labels"][owner] == str(PUBLIC_ID)


def test_remove_rpc_keeps_policies_and_everything_else_when_a_statefulset_delete_fails(mgr, dep):
    """Fail closed: a surviving worker StatefulSet keeps its CNPs; nothing else is touched."""
    apps, core, crd = _clients()
    sts = SimpleNamespace(metadata=SimpleNamespace(name="b-rpc-0"))
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(items=[sts])
    apps.delete_namespaced_stateful_set.side_effect = RuntimeError("api down")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        with pytest.raises(RuntimeError, match="policies kept"):
            mgr.remove_rpc(dep)
    crd.delete_namespaced_custom_object.assert_not_called()
    core.delete_namespaced_service.assert_not_called()
    apps.delete_namespaced_deployment.assert_not_called()
    mgr.db.return_value.update.assert_not_called()


def test_remove_rpc_deletes_workers_before_policies(mgr, dep):
    """Worker StatefulSets are deleted first, the policies last-ish (after workers succeeded)."""
    apps, core, crd = _clients()
    order = MagicMock()
    apps.list_namespaced_stateful_set.return_value = SimpleNamespace(
        items=[SimpleNamespace(metadata=SimpleNamespace(name="b-rpc-0"))]
    )
    order.attach_mock(apps.delete_namespaced_stateful_set, "sts")
    order.attach_mock(crd.delete_namespaced_custom_object, "cnp")
    with (
        patch(f"{MGR}.get_k8s_apps_client", return_value=apps),
        patch(f"{MGR}.get_k8s_core_client", return_value=core),
        patch(f"{MGR}.get_k8s_custom_client", return_value=crd),
    ):
        mgr.remove_rpc(dep)
    assert order.mock_calls[0][0] == "sts"
