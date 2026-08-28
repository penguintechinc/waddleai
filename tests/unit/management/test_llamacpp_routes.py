"""Unit tests for llama.cpp management API routes."""

from unittest.mock import MagicMock, patch

from services.management.app.api.v1.llamacpp import (
    _validate_model_filename,
    _validate_model_url,
)
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


class TestValidateModelUrl:
    """Tests for `_validate_model_url` (regression: gaps left by security review 2026-07-26).

    regression: gh-146 follow-up — plaintext http, embedded control chars.
    """

    def test_https_accepted(self):
        """A well-formed https URL with a netloc is accepted."""
        assert _validate_model_url("https://example.com/llama.gguf") is True

    def test_http_rejected(self):
        """Plaintext http is rejected — a model downloaded unencrypted is MITM-swappable."""
        assert _validate_model_url("http://example.com/llama.gguf") is False

    def test_scheme_relative_rejected(self):
        """A URL with no scheme (and thus no netloc via `//`) is rejected."""
        assert _validate_model_url("example.com/llama.gguf") is False

    def test_ftp_scheme_rejected(self):
        """A non-https scheme is rejected outright."""
        assert _validate_model_url("ftp://example.com/llama.gguf") is False

    def test_https_without_netloc_rejected(self):
        """A https URL with no host (e.g. `https:///path`) is rejected."""
        assert _validate_model_url("https:///llama.gguf") is False

    def test_embedded_newline_rejected(self):
        r"""A literal embedded `\n` is rejected — argument/header injection risk downstream."""
        assert _validate_model_url("https://example.com/llama.gguf\nEvil-Header: 1") is False

    def test_embedded_carriage_return_rejected(self):
        r"""A literal embedded `\r` is rejected alongside `\n`."""
        assert _validate_model_url("https://example.com/\rllama.gguf") is False

    def test_percent_encoded_newline_rejected(self):
        """A percent-encoded `%0a` is rejected even though it is not a literal control char."""
        assert _validate_model_url("https://example.com/llama.gguf%0aEvil") is False

    def test_percent_encoded_carriage_return_rejected(self):
        """A percent-encoded `%0d` is rejected, case-insensitively."""
        assert _validate_model_url("https://example.com/llama.gguf%0DEvil") is False

    def test_null_byte_rejected(self):
        """An embedded null byte is rejected."""
        assert _validate_model_url("https://example.com/llama.gguf\x00.exe") is False

    def test_tab_rejected(self):
        """An embedded tab character is rejected."""
        assert _validate_model_url("https://example.com/\tllama.gguf") is False

    def test_shell_metacharacter_still_rejected(self):
        """Existing shell-metacharacter protection (Vuln D) is not weakened by the rewrite."""
        assert _validate_model_url("https://example.com/$(whoami).gguf") is False

    def test_empty_rejected(self):
        """An empty string is rejected."""
        assert _validate_model_url("") is False

    def test_https_with_port_and_query_accepted(self):
        """A https URL with a non-default port and query string is still accepted."""
        assert _validate_model_url("https://cdn.example.com:8443/models/llama.gguf?v=2") is True


class TestValidateModelFilename:
    """Tests for `_validate_model_filename` (regression: gaps left by security review 2026-07-26).

    regression: gh-146 follow-up — bare `..` path traversal.
    """

    def test_normal_filename_accepted(self):
        """A plain alphanumeric filename with a `.gguf` suffix is accepted."""
        assert _validate_model_filename("llama-3.2-3b-instruct.Q4_K_M.gguf") is True

    def test_bare_double_dot_rejected(self):
        """A bare `..` (no slash) is rejected — it still resolves to the parent directory."""
        assert _validate_model_filename("..") is False

    def test_bare_single_dot_rejected(self):
        """A bare `.` is rejected — it resolves to the current directory, not a file."""
        assert _validate_model_filename(".") is False

    def test_all_dots_rejected(self):
        """A filename consisting only of dots (e.g. `...`) carries no real filename value."""
        assert _validate_model_filename("...") is False

    def test_path_separator_slash_rejected(self):
        """An embedded forward slash (traversal or absolute path) is rejected."""
        assert _validate_model_filename("../etc/passwd") is False

    def test_absolute_path_rejected(self):
        """A leading `/` (absolute path) is rejected."""
        assert _validate_model_filename("/etc/passwd") is False

    def test_dotdot_slash_encoded_rejected(self):
        """A literal (non-decoded) `..%2f` is rejected — `%` is outside the allowlist."""
        assert _validate_model_filename("..%2fetc%2fpasswd") is False

    def test_backslash_traversal_rejected(self):
        r"""A Windows-style `..\` traversal is rejected."""
        assert _validate_model_filename("..\\windows\\system32") is False

    def test_tilde_rejected(self):
        """A leading `~` (home-directory expansion) is rejected."""
        assert _validate_model_filename("~/llama.gguf") is False

    def test_unicode_dot_lookalike_rejected(self):
        """A Unicode one-dot-leader lookalike for `..` is rejected (outside the ASCII allowlist)."""
        assert _validate_model_filename("․․") is False

    def test_null_byte_rejected(self):
        """An embedded null byte is rejected."""
        assert _validate_model_filename("llama.gguf\x00.sh") is False

    def test_embedded_newline_rejected(self):
        """An embedded newline is rejected."""
        assert _validate_model_filename("llama.gguf\nEvil") is False

    def test_embedded_carriage_return_rejected(self):
        """An embedded carriage return is rejected."""
        assert _validate_model_filename("llama.gguf\rEvil") is False

    def test_overlong_filename_rejected(self):
        """A filename longer than 255 characters is rejected."""
        assert _validate_model_filename("a" * 256 + ".gguf") is False

    def test_max_length_filename_accepted(self):
        """A filename at exactly the 255-character cap is still accepted."""
        assert _validate_model_filename("a" * 250 + ".gguf") is True

    def test_empty_rejected(self):
        """An empty string is rejected."""
        assert _validate_model_filename("") is False


class TestCreateDeploymentValidatorGaps:
    """End-to-end coverage of the validator gaps through the create-deployment route."""

    async def test_create_rejects_http_model_url(self, client, auth_headers):
        """A plaintext http model_url is rejected with 400 at the API layer."""
        payload = {
            "name": "llama-3b",
            "model_name": "llama-3.2-3b-instruct",
            "model_url": "http://example.com/llama.gguf",
        }
        resp = await client.post("/api/v1/llamacpp/deployments", headers=auth_headers, json=payload)
        assert resp.status_code == 400

    async def test_create_rejects_bare_dotdot_filename(self, client, auth_headers):
        """A bare `..` model_filename is rejected with 400 at the API layer."""
        payload = {
            "name": "llama-3b",
            "model_name": "llama-3.2-3b-instruct",
            "model_filename": "..",
        }
        resp = await client.post("/api/v1/llamacpp/deployments", headers=auth_headers, json=payload)
        assert resp.status_code == 400


class TestCreateDeploymentModelValidation:
    """POST /api/v1/llamacpp/deployments -- model_url/model_filename rejection paths."""

    async def test_create_invalid_model_url_returns_400(self, client, auth_headers):
        """A model_url containing a shell metacharacter is rejected with 400."""
        payload = {
            "name": "bad-url",
            "model_name": "llama-3.2-3b-instruct",
            "model_url": "https://example.com/model.gguf; rm -rf /",
        }
        resp = await client.post("/api/v1/llamacpp/deployments", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        assert "model_url" in (await resp.get_json())["error"]

    async def test_create_invalid_model_filename_returns_400(self, client, auth_headers):
        """A model_filename containing a path separator is rejected with 400."""
        payload = {
            "name": "bad-filename",
            "model_name": "llama-3.2-3b-instruct",
            "model_filename": "../../etc/passwd",
        }
        resp = await client.post("/api/v1/llamacpp/deployments", headers=auth_headers, json=payload)
        assert resp.status_code == 400
        assert "model_filename" in (await resp.get_json())["error"]


class TestUpdateDeploymentModelValidation:
    """PATCH /api/v1/llamacpp/deployments/<id> -- model_url/model_filename validation."""

    async def test_update_invalid_model_url_returns_400(self, client, app_mock_db, auth_headers):
        """A PATCH with a shell-metacharacter model_url is rejected with 400."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json={"model_url": "https://example.com/model.gguf`whoami`"},
        )
        assert resp.status_code == 400
        assert "model_url" in (await resp.get_json())["error"]

    async def test_update_invalid_model_filename_returns_400(
        self, client, app_mock_db, auth_headers
    ):
        """A PATCH with a path-traversal model_filename is rejected with 400."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json={"model_filename": "../evil.gguf"},
        )
        assert resp.status_code == 400
        assert "model_filename" in (await resp.get_json())["error"]

    async def test_update_with_no_recognized_fields_skips_db_write(
        self, client, app_mock_db, auth_headers
    ):
        """A PATCH payload with no allowed keys returns 200 without touching the DB."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json={"unrecognized_field": "value"},
        )
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_not_called()

    async def test_update_empty_model_url_skips_validation(self, client, app_mock_db, auth_headers):
        """An empty-string model_url in the PATCH body is falsy, so validation is skipped."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json={"model_url": "  "},
        )
        assert resp.status_code == 200

    async def test_update_empty_model_filename_skips_validation(
        self, client, app_mock_db, auth_headers
    ):
        """An empty-string model_filename in the PATCH body is falsy, so validation is skipped."""
        dep = _mock_deployment(status="stopped")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        resp = await client.patch(
            "/api/v1/llamacpp/deployments/1",
            headers=auth_headers,
            json={"model_filename": "  "},
        )
        assert resp.status_code == 200


class TestDeleteDeploymentForceErrorHandling:
    """DELETE ...?force=true -- a manager exception during forced removal is swallowed."""

    async def test_force_delete_manager_exception_still_deletes_row(
        self, client, app_mock_db, auth_headers
    ):
        """remove_daemonset raising on a forced delete is logged, not fatal -- row is deleted."""
        dep = _mock_deployment(status="running", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.remove_daemonset.side_effect = Exception("k8s unreachable")
            mock_mgr_class.return_value = mock_mgr
            resp = await client.delete(
                "/api/v1/llamacpp/deployments/1?force=true", headers=auth_headers
            )
        assert resp.status_code == 200
        app_mock_db.return_value.delete.assert_called_once()


class TestRemoveRouteErrorHandling:
    """POST .../remove -- a manager exception is translated into a 503."""

    async def test_remove_kubernetes_manager_exception_returns_503(
        self, client, app_mock_db, auth_headers
    ):
        """remove_daemonset raising during remove returns 503 with the error message."""
        dep = _mock_deployment(status="running", deployment_type="kubernetes")
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.LlamaCppManager") as mock_mgr_class:
            mock_mgr = MagicMock()
            mock_mgr.remove_daemonset.side_effect = Exception("k8s unreachable")
            mock_mgr_class.return_value = mock_mgr
            resp = await client.post("/api/v1/llamacpp/deployments/1/remove", headers=auth_headers)
        assert resp.status_code == 503
        assert "k8s unreachable" in (await resp.get_json())["error"]


class TestHealthRouteErrorHandling:
    """GET .../health -- a request exception is reported as unhealthy, not a 500."""

    async def test_health_check_request_exception_returns_unhealthy(
        self, client, app_mock_db, auth_headers
    ):
        """requests.get raising is caught and reported as status=unhealthy with the error text."""
        dep = _mock_deployment(status="running")
        dep.endpoint_url = "http://localhost:8080"
        app_mock_db.return_value.select.return_value.first.return_value = dep
        with patch("services.management.app.api.v1.llamacpp.requests") as mock_requests:
            mock_requests.get.side_effect = Exception("connection refused")
            resp = await client.get("/api/v1/llamacpp/deployments/1/health", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "unhealthy"
        assert "connection refused" in data["error"]


class TestAuthorizationBoundary:
    """Scope-boundary coverage.

    LLAMACPP_ADMIN is a global-admin-only permission (shared/auth/rbac.py) --
    resource_manager and plain user roles never hold it. llamacpp_deployments
    has no org_id column (see services/management/app/models_sqlalchemy.py)
    and Role.ADMIN is a single system-wide role, not per-org, so there is no
    cross-org 403/404 case to test here: the resource is platform-global by
    design, not tenant-scoped (see the PR description). The scope check below
    is the equivalent authorization boundary that does apply.
    """

    async def test_list_resource_manager_forbidden(self, client, rm_auth_headers):
        """resource_manager cannot list deployments -- 403."""
        resp = await client.get("/api/v1/llamacpp/deployments", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_list_plain_user_forbidden(self, client, user_auth_headers):
        """A plain user cannot list deployments -- 403."""
        resp = await client.get("/api/v1/llamacpp/deployments", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_create_resource_manager_forbidden(self, client, rm_auth_headers):
        """resource_manager cannot create a deployment -- 403, before any validation runs."""
        resp = await client.post(
            "/api/v1/llamacpp/deployments", headers=rm_auth_headers, json={"name": "x"}
        )
        assert resp.status_code == 403

    async def test_delete_resource_manager_forbidden(self, client, rm_auth_headers):
        """resource_manager cannot delete a deployment -- 403."""
        resp = await client.delete("/api/v1/llamacpp/deployments/1", headers=rm_auth_headers)
        assert resp.status_code == 403
