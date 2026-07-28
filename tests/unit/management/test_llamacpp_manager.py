"""Unit tests for LlamaCppManager"""
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def manager(mock_db):
    from services.management.app.services.llamacpp_manager import LlamaCppManager
    return LlamaCppManager(mock_db)


@pytest.fixture
def k8s_deployment():
    """Minimal deployment record for K8s mode."""
    dep = MagicMock()
    dep.id = 1
    dep.name = "llama-3b"
    dep.deployment_type = "kubernetes"
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://huggingface.co/example/llama-3.2-3b.gguf"
    dep.model_filename = "llama-3.2-3b.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 1
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.endpoint_url = None
    dep.status = "pending"
    # Hardening attributes (defaults when not provided)
    dep.model_cache_claim = None
    dep.cpu_request = None
    dep.cpu_limit = None
    dep.memory_request = None
    dep.memory_limit = None
    return dep


@pytest.fixture
def remote_deployment():
    dep = MagicMock()
    dep.id = 2
    dep.name = "remote-llama"
    dep.deployment_type = "remote"
    dep.model_name = "llama-3.1-8b-instruct"
    dep.endpoint_url = "http://192.168.1.50:8080"
    dep.status = "pending"
    return dep


def test_generate_daemonset_name(manager):
    name = manager._daemonset_name("my-model")
    assert name == "waddleai-llamacpp-my-model"


def test_generate_daemonset_name_sanitises_special_chars(manager):
    name = manager._daemonset_name("My Model v2.0!")
    assert name == "waddleai-llamacpp-my-model-v2-0"


def test_export_k8s_manifest_contains_daemonset(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    kinds = [d["kind"] for d in docs]
    assert "DaemonSet" in kinds
    assert "Service" in kinds


def test_export_k8s_manifest_node_selector(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    node_sel = ds["spec"]["template"]["spec"]["nodeSelector"]
    assert node_sel == {"waddleai/gpu-tier": "a100"}


def test_export_k8s_manifest_gpu_resource(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    container = ds["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"


def test_export_k8s_manifest_init_container_download_url(manager, k8s_deployment):
    """URL should be passed via env var (not in command) for security."""
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    init_c = ds["spec"]["template"]["spec"]["initContainers"][0]
    env_vars = {e["name"]: e["value"] for e in init_c["env"]}
    assert env_vars["MODEL_URL"] == k8s_deployment.model_url


def test_export_k8s_manifest_service_port(manager, k8s_deployment):
    manifest_yaml = manager.export_k8s_manifest(k8s_deployment)
    docs = list(yaml.safe_load_all(manifest_yaml))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["ports"][0]["port"] == 8080


def test_deploy_daemonset_calls_k8s_api(manager, k8s_deployment, mock_db):
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps, \
         patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core:
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.deploy_daemonset(k8s_deployment)

    mock_apps.return_value.create_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.create_namespaced_service.assert_called_once()


def test_deploy_daemonset_k8s_error_propagates(manager, k8s_deployment):
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps:
        mock_apps.return_value.create_namespaced_daemon_set.side_effect = Exception("k8s unavailable")
        with pytest.raises(Exception, match="k8s unavailable"):
            manager.deploy_daemonset(k8s_deployment)


def test_remove_daemonset_running_without_force_raises(manager, k8s_deployment):
    k8s_deployment.status = "running"
    with pytest.raises(ValueError, match="force=True"):
        manager.remove_daemonset(k8s_deployment, force=False)


def test_remove_daemonset_running_with_force_deletes(manager, k8s_deployment):
    k8s_deployment.status = "running"
    with patch("services.management.app.services.llamacpp_manager.get_k8s_apps_client") as mock_apps, \
         patch("services.management.app.services.llamacpp_manager.get_k8s_core_client") as mock_core:
        mock_apps.return_value = MagicMock()
        mock_core.return_value = MagicMock()
        manager.remove_daemonset(k8s_deployment, force=True)

    mock_apps.return_value.delete_namespaced_daemon_set.assert_called_once()
    mock_core.return_value.delete_namespaced_service.assert_called_once()


def test_register_remote_healthy_sets_running(manager, remote_deployment, mock_db):
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.return_value.status_code = 200
        manager.register_remote(remote_deployment)

    mock_db(mock_db.llamacpp_deployments.id == remote_deployment.id).update.assert_called_once_with(status="running")


def test_register_remote_unhealthy_raises(manager, remote_deployment):
    with patch("services.management.app.services.llamacpp_manager.requests") as mock_req:
        mock_req.get.side_effect = Exception("connection refused")
        with pytest.raises(ValueError, match="unreachable"):
            manager.register_remote(remote_deployment)
