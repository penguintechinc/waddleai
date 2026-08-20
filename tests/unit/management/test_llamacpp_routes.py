"""Unit tests for llama.cpp management API routes."""

from unittest.mock import MagicMock, patch

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
    """Tests for GET /api/v1/llamacpp/deployments."""

    async def test_list_returns_200(self, client, app_mock_db, auth_headers):
        """An empty deployment table returns 200 with an empty deployments list."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get("/api/v1/llamacpp/deployments", headers=auth_headers)
        assert resp.status_code == 200
        assert (await resp.get_json())["deployments"] == []

    async def test_list_with_deployments(self, client, app_mock_db, auth_headers):
        """All rows returned by the query are serialized into the deployments list."""
        dep1 = _mock_deployment(dep_id=1, name="llama-3b")
        dep2 = _mock_deployment(dep_id=2, name="llama-7b")
        app_mock_db.return_value.select.return_value = make_select_result([dep1, dep2])
        resp = await client.get("/api/v1/llamacpp/deployments", headers=auth_headers)
        assert resp.status_code == 200
        assert len((await resp.get_json())["deployments"]) == 2

    async def test_list_requires_auth(self, client):
        """An unauthenticated request is refused with 401 before touching the DB."""
        resp = await client.get("/api/v1/llamacpp/deployments")
        assert resp.status_code == 401


class TestCreateDeployment:
    """Tests for POST /api/v1/llamacpp/deployments."""

    async def test_create_kubernetes_deployment(self, client, app_mock_db, auth_headers):
        """A valid kubernetes-type payload is inserted and returns 201 with the new id."""
        app_mock_db.llamacpp_deployments.insert.return_value = 1
        payload = {
            "name": "llama-3b",
            "deployment_type": "kubernetes",
            "model_name": "llama-3.2-3b-instruct",
            "model_url": "https://example.com/llama.gguf",
            "model_filename": "llama.gguf",
            "node_selector": {"waddleai/gpu-tier": "a100"},
        }
        resp = await client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            json=payload,
        )
        assert resp.status_code == 201
        assert (await resp.get_json())["deployment_id"] == 1

    async def test_create_remote_deployment(self, client, app_mock_db, auth_headers):
        """A valid remote-type payload is inserted and returns 201 with the new id."""
        app_mock_db.llamacpp_deployments.insert.return_value = 2
        payload = {
            "name": "remote-llama",
            "deployment_type": "remote",
            "model_name": "llama-3.2-3b-instruct",
            "endpoint_url": "http://192.168.1.50:8080",
        }
        resp = await client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            json=payload,
        )
        assert resp.status_code == 201
        assert (await resp.get_json())["deployment_id"] == 2

    async def test_create_missing_name_returns_400(self, client, auth_headers):
        """A payload without `name` is rejected with 400 and a `name is required` error."""
        resp = await client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            json={"deployment_type": "kubernetes"},
        )
        assert resp.status_code == 400
        assert "name is required" in (await resp.get_json())["error"]

    async def test_create_missing_model_name_returns_400(self, client, auth_headers):
        """A payload without `model_name` is rejected with 400 and the matching error."""
        resp = await client.post(
            "/api/v1/llamacpp/deployments",
            headers=auth_headers,
            json={"name": "test", "deployment_type": "kubernetes"},
        )
        assert resp.status_code == 400
        assert "model_name is required" in (await resp.get_json())["error"]

    async def test_create_requires_auth(self, client):
        """An unauthenticated create request is refused with 401 before validation runs."""
        resp = await client.post(
            "/api/v1/llamacpp/deployments",
            json={"name": "test"},
        )
        assert resp.status_code == 401


class TestGetDeployment:
    """Tests for GET /api/v1/llamacpp/deployments/<id>."""

    async def test_get_existing_returns_200(self, client, app_mock_db, auth_headers):
        """An existing deployment is returned with its name and model_name fields."""
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.get("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["name"] == "llama-3b"
        assert data["model_name"] == "llama-3.2-3b-instruct"

    async def test_get_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.get("/api/v1/llamacpp/deployments/99", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_requires_auth(self, client):
        """An unauthenticated get request is refused with 401 before touching the DB."""
        resp = await client.get("/api/v1/llamacpp/deployments/1")
        assert resp.status_code == 401


class TestUpdateDeployment:
    """Tests for PATCH /api/v1/llamacpp/deployments/<id>."""

    async def test_update_stopped_deployment(self, client, app_mock_db, auth_headers):
        """A stopped deployment accepts config updates (n_ctx, n_gpu_layers) and returns 200."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        payload = {"n_ctx": 8192, "n_gpu_layers": 32}
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json=payload,
        )
        assert resp.status_code == 200

    async def test_update_running_deployment_returns_409(self, client, app_mock_db, auth_headers):
        """A running deployment refuses config updates with 409 (must be stopped first)."""
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        payload = {"n_ctx": 8192}
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json=payload,
        )
        assert resp.status_code == 409

    async def test_update_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on update."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/99",
            headers=auth_headers,
            json={"n_ctx": 8192},
        )
        assert resp.status_code == 404


class TestDeleteDeployment:
    """Tests for DELETE /api/v1/llamacpp/deployments/<id>."""

    async def test_delete_stopped_succeeds(self, client, app_mock_db, auth_headers):
        """A stopped deployment is deleted without needing the force flag."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.delete("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_running_without_force_returns_409(
        self, client, app_mock_db, auth_headers
    ):
        """Deleting a running deployment without force=true is refused with 409."""
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.delete("/api/v1/llamacpp/deployments/1", headers=auth_headers)
        assert resp.status_code == 409
        assert "force=true" in (await resp.get_json())["error"]

    async def test_delete_running_with_force_succeeds(self, client, app_mock_db, auth_headers):
        """force=true on a running deployment allows the delete to proceed."""
        dep = _mock_deployment(status="running")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = await client.delete(
                "/api/v1/llamacpp/deployments/1?force=true", headers=auth_headers
            )
        assert resp.status_code == 200

    async def test_delete_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on delete."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.delete("/api/v1/llamacpp/deployments/99", headers=auth_headers)
        assert resp.status_code == 404


class TestDeployRoute:
    """Tests for POST /api/v1/llamacpp/deployments/<id>/deploy."""

    async def test_deploy_kubernetes_calls_manager(self, client, app_mock_db, auth_headers):
        """A pending kubernetes deployment calls LlamaCppManager.deploy_daemonset exactly once."""
        dep = _mock_deployment(status="pending", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = await client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.deploy_daemonset.assert_called_once()

    async def test_deploy_remote_calls_register(self, client, app_mock_db, auth_headers):
        """A pending remote deployment calls LlamaCppManager.register_remote exactly once."""
        dep = _mock_deployment(status="pending", deployment_type="remote")
        dep.endpoint_url = "http://192.168.1.50:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = await client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.register_remote.assert_called_once()

    async def test_deploy_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on deploy."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.post("/api/v1/llamacpp/deployments/99/deploy", headers=auth_headers)
        assert resp.status_code == 404

    async def test_deploy_manager_exception_returns_503(self, client, app_mock_db, auth_headers):
        """A manager exception during deploy is translated into a 503, not a 500 or a crash."""
        dep = _mock_deployment(status="pending")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.deploy_daemonset.side_effect = Exception("K8s API error")
            mock_mgr_class.return_value = mock_mgr
            resp = await client.post("/api/v1/llamacpp/deployments/1/deploy", headers=auth_headers)
        assert resp.status_code == 503


class TestRemoveRoute:
    """Tests for POST /api/v1/llamacpp/deployments/<id>/remove."""

    async def test_remove_kubernetes_calls_manager(self, client, app_mock_db, auth_headers):
        """A running kubernetes deployment calls LlamaCppManager.remove_daemonset exactly once."""
        dep = _mock_deployment(status="running", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr_class.return_value = mock_mgr
            resp = await client.post("/api/v1/llamacpp/deployments/1/remove", headers=auth_headers)
        assert resp.status_code == 200
        mock_mgr.remove_daemonset.assert_called_once()

    async def test_remove_remote_updates_status(self, client, app_mock_db, auth_headers):
        """A running remote deployment is removed via a status update, no K8s call needed."""
        dep = _mock_deployment(status="running", deployment_type="remote")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.post("/api/v1/llamacpp/deployments/1/remove", headers=auth_headers)
        assert resp.status_code == 200

    async def test_remove_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on remove."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.post("/api/v1/llamacpp/deployments/99/remove", headers=auth_headers)
        assert resp.status_code == 404


class TestHealthRoute:
    """Tests for GET /api/v1/llamacpp/deployments/<id>/health."""

    async def test_health_check_healthy(self, client, app_mock_db, auth_headers):
        """A 200 from the deployment's endpoint is reported as status=healthy."""
        dep = _mock_deployment(status="running")
        dep.endpoint_url = "http://localhost:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_requests.get.return_value = mock_resp
            resp = await client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert (await resp.get_json())["status"] == "healthy"

    async def test_health_check_unhealthy(self, client, app_mock_db, auth_headers):
        """A non-200 from the deployment's endpoint is reported as status=unhealthy."""
        dep = _mock_deployment(status="running")
        dep.endpoint_url = "http://localhost:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_requests.get.return_value = mock_resp
            resp = await client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert (await resp.get_json())["status"] == "unhealthy"

    async def test_health_check_no_endpoint(self, client, app_mock_db, auth_headers):
        """A deployment with no endpoint_url is reported as status=unknown, no HTTP call made."""
        dep = _mock_deployment(status="pending")
        dep.endpoint_url = None
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        assert (await resp.get_json())["status"] == "unknown"

    async def test_health_check_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on health check."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.get("/api/v1/llamacpp/deployments/99/health", headers=auth_headers)
        assert resp.status_code == 404


class TestExportManifest:
    """Tests for GET /api/v1/llamacpp/deployments/<id>/export/k8s."""

    async def test_export_returns_yaml(self, client, app_mock_db, auth_headers):
        """The exported response body contains the manager's generated YAML manifest."""
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.export_k8s_manifest.return_value = "kind: DaemonSet\n---\nkind: Service\n"
            mock_mgr_class.return_value = mock_mgr
            resp = await client.get(
                "/api/v1/llamacpp/deployments/1/export/k8s", headers=auth_headers
            )
        assert resp.status_code == 200
        assert b"DaemonSet" in (await resp.data)

    async def test_export_nonexistent_returns_404(self, client, app_mock_db, auth_headers):
        """A deployment id with no matching row returns 404 on export."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.get("/api/v1/llamacpp/deployments/99/export/k8s", headers=auth_headers)
        assert resp.status_code == 404

    async def test_export_content_type(self, client, app_mock_db, auth_headers):
        """The export response declares the application/x-yaml content type."""
        dep = _mock_deployment()
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.export_k8s_manifest.return_value = "kind: DaemonSet\n"
            mock_mgr_class.return_value = mock_mgr
            resp = await client.get(
                "/api/v1/llamacpp/deployments/1/export/k8s", headers=auth_headers
            )
        assert resp.content_type == "application/x-yaml"
