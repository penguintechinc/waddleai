"""C2c: the Kubernetes-client-backed gateway, with the kubernetes client mocked."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from services.management.app.services import fleet_secrets as fs
from services.management.app.services.fleet_secrets import KubernetesGateway


@pytest.fixture
def gw(monkeypatch):
    """A gateway whose four API clients are MagicMocks (in-cluster config 'loaded')."""
    monkeypatch.setattr("kubernetes.config.load_incluster_config", lambda: None)
    gateway = KubernetesGateway()
    gateway._core, gateway._apps = MagicMock(), MagicMock()
    gateway._batch, gateway._custom = MagicMock(), MagicMock()
    return gateway


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def test_clients_fall_back_to_kubeconfig_when_not_in_cluster(monkeypatch) -> None:
    """Outside a cluster the kubeconfig is loaded and all four clients are built once."""
    loaded: list[str] = []

    def incluster() -> None:
        raise RuntimeError("not in cluster")

    monkeypatch.setattr("kubernetes.config.load_incluster_config", incluster)
    monkeypatch.setattr("kubernetes.config.load_kube_config", lambda: loaded.append("kube"))
    gateway = KubernetesGateway()
    first = gateway._clients()
    assert loaded == ["kube"] and len(first) == 4 and gateway._clients() is not None
    assert loaded == ["kube"]


def test_read_secret_decodes_and_maps_404_to_none(gw) -> None:
    """Data is base64-decoded; a missing Secret is None; other errors propagate."""
    gw._core.read_namespaced_secret.return_value = SimpleNamespace(data={"k": _b64("v")})
    assert gw.read_secret("ns", "s") == {"k": "v"}
    gw._core.read_namespaced_secret.return_value = SimpleNamespace(data=None)
    assert gw.read_secret("ns", "s") == {}
    gw._core.read_namespaced_secret.side_effect = ApiException(status=404)
    assert gw.read_secret("ns", "s") is None
    gw._core.read_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        gw.read_secret("ns", "s")


def test_patch_secret_encodes_values_and_nulls_removed_keys(gw) -> None:
    """A merge patch base64-encodes values and sends None to delete a key."""
    gw.patch_secret("ns", "s", {"a": "x", "b": None})
    name, namespace, body = gw._core.patch_namespaced_secret.call_args.args
    assert (name, namespace) == ("s", "ns")
    assert body == {"data": {"a": _b64("x"), "b": None}}


def test_delete_and_create_secret(gw) -> None:
    """Delete ignores 404 only; create sends string_data."""
    gw.delete_secret("ns", "s")
    gw._core.delete_namespaced_secret.side_effect = ApiException(status=404)
    gw.delete_secret("ns", "s")
    gw._core.delete_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        gw.delete_secret("ns", "s")
    gw.create_secret("ns", "new", {"HF_TOKEN": "t"})
    secret = gw._core.create_namespaced_secret.call_args.args[1]
    assert secret.metadata.name == "new" and secret.string_data == {"HF_TOKEN": "t"}


def test_bump_raycluster_patches_head_and_every_worker_group(gw) -> None:
    """The annotation goes on the head and all worker templates, incrementing the generation."""
    gw._custom.get_namespaced_custom_object.return_value = {
        "spec": {
            "headGroupSpec": {
                "template": {"metadata": {"annotations": {fs.RESTART_ANNOTATION: "4"}}}
            },
            "workerGroupSpecs": [{"groupName": "g1"}, {"groupName": "g2"}],
        }
    }
    assert gw.bump_restart_generation("ns", "RayCluster", "ray") == 5
    patch = gw._custom.patch_namespaced_custom_object.call_args.kwargs["body"]["spec"]
    assert [g["groupName"] for g in patch["workerGroupSpecs"]] == ["g1", "g2"]
    for template in (patch["headGroupSpec"]["template"], patch["workerGroupSpecs"][0]["template"]):
        assert template["metadata"]["annotations"][fs.RESTART_ANNOTATION] == "5"


def test_bump_statefulset_and_unsupported_kind(gw) -> None:
    """A StatefulSet's template annotation is incremented; unknown kinds are refused."""
    template = SimpleNamespace(metadata=SimpleNamespace(annotations=None))
    gw._apps.read_namespaced_stateful_set.return_value = SimpleNamespace(
        spec=SimpleNamespace(template=template)
    )
    assert gw.bump_restart_generation("ns", "StatefulSet", "head") == 1
    body = gw._apps.patch_namespaced_stateful_set.call_args.args[2]
    assert body["spec"]["template"]["metadata"]["annotations"][fs.RESTART_ANNOTATION] == "1"
    with pytest.raises(ValueError, match="unsupported"):
        gw.bump_restart_generation("ns", "Job", "x")


def test_bump_deployment_increments_existing_generation(gw) -> None:
    """The llama.cpp RPC head is a Deployment: its template annotation is bumped (4 -> 5)."""
    template = SimpleNamespace(metadata=SimpleNamespace(annotations={fs.RESTART_ANNOTATION: "4"}))
    gw._apps.read_namespaced_deployment.return_value = SimpleNamespace(
        spec=SimpleNamespace(template=template)
    )
    assert gw.bump_restart_generation("ns", "Deployment", "head") == 5
    name, namespace, body = gw._apps.patch_namespaced_deployment.call_args.args
    assert (name, namespace) == ("head", "ns")
    assert body["spec"]["template"]["metadata"]["annotations"][fs.RESTART_ANNOTATION] == "5"
    gw._apps.patch_namespaced_stateful_set.assert_not_called()


def test_jobs_create_conflict_state_and_delete(gw) -> None:
    """A 409 on create is fine (one Job per model); states map; delete ignores 404."""
    gw.create_job("ns", {"metadata": {"name": "j"}})
    gw._batch.create_namespaced_job.side_effect = ApiException(status=409)
    gw.create_job("ns", {"metadata": {"name": "j"}})
    gw._batch.create_namespaced_job.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        gw.create_job("ns", {})
    for status, expected in (
        (SimpleNamespace(succeeded=1, failed=0), "succeeded"),
        (SimpleNamespace(succeeded=0, failed=2), "failed"),
        (SimpleNamespace(succeeded=0, failed=0), "running"),
    ):
        gw._batch.read_namespaced_job.return_value = SimpleNamespace(status=status)
        assert gw.job_state("ns", "j") == expected
    gw._batch.read_namespaced_job.side_effect = ApiException(status=404)
    assert gw.job_state("ns", "j") == "missing"
    gw._batch.read_namespaced_job.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        gw.job_state("ns", "j")
    gw.delete_job("ns", "j")
    gw._batch.delete_namespaced_job.side_effect = ApiException(status=404)
    gw.delete_job("ns", "j")
    gw._batch.delete_namespaced_job.side_effect = ApiException(status=500)
    with pytest.raises(ApiException):
        gw.delete_job("ns", "j")


def test_default_gateway_is_built_lazily_and_replaceable(monkeypatch) -> None:
    """get_gateway builds the real gateway once; set_gateway(None) resets it."""
    fs.set_gateway(None)
    first = fs.get_gateway()
    assert isinstance(first, KubernetesGateway) and fs.get_gateway() is first
    fs.set_gateway(None)


async def test_secret_failures_never_leak_the_value(caplog) -> None:
    """A client error that echoes the request body is never chained or logged."""
    leaky = MagicMock()
    leaky.patch_secret.side_effect = RuntimeError("body was SENTINEL-VALUE")
    fs.set_gateway(leaky)
    try:
        with pytest.raises(fs.SecretWriteFailed) as info:
            await fs.write_deployment_key("slug", "SENTINEL-VALUE")
        assert info.value.__cause__ is None and "SENTINEL" not in str(info.value)
        assert "SENTINEL" not in caplog.text
        with pytest.raises(fs.SecretWriteFailed):
            await fs.remove_key("slug")
        leaky.bump_restart_generation.side_effect = RuntimeError("x")
        with pytest.raises(fs.SecretWriteFailed):
            await fs.bump_restart("RayCluster", "r")
    finally:
        fs.set_gateway(None)
