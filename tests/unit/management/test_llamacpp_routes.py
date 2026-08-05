"""Unit tests for llama.cpp management API routes"""
import json
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.management.conftest import make_select_result


def _mock_deployment(dep_id=1, name="llama-3b", status="pending", deployment_type="kubernetes"):
    """Create a mock llama.cpp deployment."""
    dep = MagicMock()
    dep.id = dep_id
    dep.name = name
    dep.status = status
    dep.deployment_type = deployment_type
    dep.model_name = "llama-3.2-3b-instruct"
    dep.model_url = "https://example.com/llama.gguf"
    dep.model_filename = "llama.gguf"
    dep.n_ctx = 4096
    dep.n_gpu_layers = -1
    dep.gpu_count = 1
    dep.endpoint_url = None
    dep.k8s_namespace = "waddleai"
    dep.k8s_daemonset_name = "waddleai-llamacpp-llama-3b"
    dep.node_selector = {"waddleai/gpu-tier": "a100"}
    dep.node_affinity = None
    dep.status_message = None
    dep.created_at = None
    dep.modified_at = None
    return dep


class TestListDeployments:
    def test_list_returns_200(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = client.get("/api/v1/llamacpp/deployments", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["deployments"] == []

    def test_list_with_deployments(self, client, app_mock_db, auth_headers):
        dep1 = _mock_deployment(dep_id=1, name="llama-3b")
        dep2 = _mock_deployment(dep_id=2, name="llama-7b")
        app_mock_db.return_value.select.return_value = make_select_result([dep1, dep2])
        resp = client.get("/api/v1/llamacpp/deployments", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.get_json()["deployments"]) == 2

    def test_list_requires_auth(self, client):
        resp = client.get("/api/v1/llamacpp/deployments")
        assert resp.status_code == 401


class TestCreateDeployment:
    def test_create_kubernetes_deployment(self, client, app_mock_db, auth_headers):
        app_mock_db.llamacpp_deployments.insert.return_value = 1
        payload = {
            "name": "llama-3b",
            "deployment_type": "kubernetes",
            "model_name": "llama-3.2-3b-instruct",
            "model_url": "https://example.com/llama.gguf",
            "model_filename": "llama.gguf",
            "node_selector": {"waddleai/gpu-tier": "a100"},
        }
        resp = client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.get_json()["deployment_id"] == 1

    def test_create_remote_deployment(self, client, app_mock_db, auth_headers):
        app_mock_db.llamacpp_deployments.insert.return_value = 2
        payload = {
            "name": "remote-llama",
            "deployment_type": "remote",
            "model_name": "llama-3.2-3b-instruct",
            "endpoint_url": "http://192.168.1.50:8080",
        }
        resp = client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.get_json()["deployment_id"] == 2

    def test_create_missing_name_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            data=json.dumps({"deployment_type": "kubernetes"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "name is required" in resp.get_json()["error"]

    def test_create_missing_model_name_returns_400(self, client, auth_headers):
        resp = client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            data=json.dumps({"name": "test", "deployment_type": "kubernetes"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "model_name is required" in resp.get_json()["error"]

    def test_create_requires_auth(self, client):
        resp = client.post(
            "/api/v1/llamacpp/deployments",
            data=json.dumps({"name": "test"}),
            content_type="application/json",
        )
        assert resp.status_code == 401


class TestGetDeployment:
    def test_get_existing_returns_200(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = client.get("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["name"] == "llama-3b"
        assert data["model_name"] == "llama-3.2-3b-instruct"

    def test_get_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.get("/api/v1/llamacpp/deployments/99", headers=auth_headers)
        assert resp.status_code == 404

    def test_get_requires_auth(self, client):
        resp = client.get("/api/v1/llamacpp/deployments/1")
        assert resp.status_code == 401


class TestUpdateDeployment:
    def test_update_stopped_deployment(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        payload = {"n_ctx": 8192, "n_gpu_layers": 32}
        resp = client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_update_running_deployment_returns_409(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        payload = {"n_ctx": 8192}
        resp = client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert resp.status_code == 409

    def test_update_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.patch(
            "/api/v1/llamacpp/deployments/99",
            headers=auth_headers,
            data=json.dumps({"n_ctx": 8192}),
            content_type="application/json",
        )
        assert resp.status_code == 404


class TestDeleteDeployment:
    def test_delete_stopped_succeeds(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = client.delete("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_running_without_force_returns_409(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = client.delete("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 409
        assert "force=true" in resp.get_json()["error"]

    def test_delete_running_with_force_succeeds(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = client.delete("/api/v1/llamacpp/deployments/1?force=true", headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.delete("/api/v1/llamacpp/deployments/99", headers=auth_headers)
        assert resp.status_code == 404


class TestDeployRoute:
    def test_deploy_kubernetes_calls_manager(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="pending", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.deploy_daemonset.assert_called_once()

    def test_deploy_remote_calls_register(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="pending", deployment_type="remote")
        dep.endpoint_url = "http://192.168.1.50:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.register_remote.assert_called_once()

    def test_deploy_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.post("/api/v1/llamacpp/deployments/99/deploy", headers=auth_headers)
        assert resp.status_code == 404

    def test_deploy_manager_exception_returns_503(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="pending")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.deploy_daemonset.side_effect = Exception("K8s API error")
            mock_mgr_class.return_value = mock_mgr
            resp = client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 503


class TestRemoveRoute:
    def test_remove_kubernetes_calls_manager(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = client.post("/api/v1/llamacpp/deployments/1/remove", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.remove_daemonset.assert_called_once()

    def test_remove_remote_updates_status(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running", deployment_type="remote")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = client.post("/api/v1/llamacpp/deployments/1/remove", headers=auth_headers)
        assert resp.status_code == 200

    def test_remove_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.post("/api/v1/llamacpp/deployments/99/remove", headers=auth_headers)
        assert resp.status_code == 404


class TestHealthRoute:
    def test_health_check_healthy(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running")
        dep.endpoint_url = "http://localhost:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_requests.get.return_value = mock_resp
            resp = client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "healthy"

    def test_health_check_unhealthy(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="running")
        dep.endpoint_url = "http://localhost:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_requests.get.return_value = mock_resp
            resp = client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "unhealthy"

    def test_health_check_no_endpoint(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment(status="pending")
        dep.endpoint_url = None
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "unknown"

    def test_health_check_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.get("/api/v1/llamacpp/deployments/99/health", headers=auth_headers)
        assert resp.status_code == 404


class TestExportManifest:
    def test_export_returns_yaml(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.export_k8s_manifest.return_value = "kind: DaemonSet\n---\nkind: Service\n"
            mock_mgr_class.return_value = mock_mgr
            resp = client.get("/api/v1/llamacpp/deployments/1/export/k8s", headers=auth_headers)
        assert resp.status_code == 200
        assert b"DaemonSet" in resp.data

    def test_export_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = client.get("/api/v1/llamacpp/deployments/99/export/k8s", headers=auth_headers)
        assert resp.status_code == 404

    def test_export_content_type(self, client, app_mock_db, auth_headers):
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.export_k8s_manifest.return_value = "kind: DaemonSet\n"
            mock_mgr_class.return_value = mock_mgr
            resp = client.get("/api/v1/llamacpp/deployments/1/export/k8s", headers=auth_headers)
        assert resp.content_type == "application/x-yaml"
