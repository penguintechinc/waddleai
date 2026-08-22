"""Pytest test suite for WaddleAI Management API v1 - Ollama Deployment Routes.

Tests all endpoints:
- GET /api/v1/ollama/deployments - list deployments (admin only)
- GET /api/v1/ollama/deployments/<id> - get deployment (admin only)
- POST /api/v1/ollama/deployments - create deployment (admin only)
- PUT /api/v1/ollama/deployments/<id> - update deployment (admin only)
- DELETE /api/v1/ollama/deployments/<id> - delete deployment (admin only)
- POST /api/v1/ollama/deployments/<id>/start - start (admin only)
- POST /api/v1/ollama/deployments/<id>/stop - stop (admin only)
- POST /api/v1/ollama/deployments/<id>/restart - restart (admin only)
- GET /api/v1/ollama/deployments/<id>/health - health check (admin only)
- GET /api/v1/ollama/deployments/<id>/docker-compose - export docker-compose (admin only)
- GET /api/v1/ollama/deployments/<id>/k8s-manifest - export k8s manifest (admin only)
- GET /api/v1/ollama/deployments/<id>/models - list deployment models (admin only)
- POST /api/v1/ollama/deployments/<id>/models/pull - pull model (admin only)
- DELETE /api/v1/ollama/deployments/<id>/models/<model_name> - remove model (admin only)
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from services.management.app.services.ollama_manager import OllamaDeploymentManager, PullStatus
from tests.unit.management.conftest import make_select_result

# ============================================================================
# Mock Builders
# ============================================================================


def make_mock_deployment(dep_id=1, name="test-deployment", status="active"):
    """Create a mock Ollama deployment."""
    d = MagicMock()
    d.id = dep_id
    d.name = name
    d.endpoint_url = "http://localhost:11434"
    d.deployment_type = "external"
    d.docker_compose_config = None
    d.gpu_config = {}
    d.resource_limits = {}
    d.status = status
    d.health_status = "healthy"
    d.auto_start = True
    d.last_health_check = None  # Must be None to avoid .isoformat() error
    d.created_at = None  # Must be None to avoid .isoformat() error
    return d


def make_mock_model(model_id=1, name="llama3.2", deployment_id=1):
    """Create a mock Ollama model."""
    m = MagicMock()
    m.id = model_id
    m.model_name = name
    m.model_tag = "latest"
    m.deployment_id = deployment_id
    m.status = "available"
    m.size_bytes = 0
    m.auto_pull = True
    m.last_updated = None
    return m


def make_mock_manager(**method_returns):
    """Build a spec'd fake OllamaDeploymentManager returning fixed values per method.

    Specced against the real class so a call to a nonexistent method name
    fails loudly instead of silently auto-creating an attribute.
    """
    manager = MagicMock(spec=OllamaDeploymentManager)
    for name, value in method_returns.items():
        getattr(manager, name).return_value = value
    return manager


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_app_config(flask_app):
    """Ensure OLLAMA config is enabled for each test."""
    flask_app.config["ENABLE_OLLAMA_MANAGEMENT"] = True
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "both"
    yield
    # Cleanup if needed
    flask_app.config["ENABLE_OLLAMA_MANAGEMENT"] = True
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "both"


# ============================================================================
# GET /ollama/deployments - List Deployments
# ============================================================================


async def test_list_ollama_deployments_admin_success(client, app_mock_db, auth_headers):
    """Admin can list all Ollama deployments."""
    dep1 = make_mock_deployment(dep_id=1, name="dep1")
    dep2 = make_mock_deployment(dep_id=2, name="dep2")

    # Mock select() to return deployments
    app_mock_db.return_value.select.return_value = make_select_result([dep1, dep2])
    # Mock count() for model counts
    app_mock_db.return_value.count.side_effect = [2, 3]  # dep1 has 2 models, dep2 has 3

    resp = await client.get("/api/v1/ollama/deployments", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["total"] == 2
    assert len(data["deployments"]) == 2
    assert data["deployments"][0]["name"] == "dep1"
    assert data["deployments"][0]["model_count"] == 2
    assert data["deployments"][1]["name"] == "dep2"
    assert data["deployments"][1]["model_count"] == 3


async def test_list_ollama_deployments_empty(client, app_mock_db, auth_headers):
    """List returns empty array when no deployments."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["total"] == 0
    assert data["deployments"] == []


async def test_list_ollama_deployments_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments", headers=user_auth_headers)

    assert resp.status_code == 403


async def test_list_ollama_deployments_disabled(client, app_mock_db, auth_headers, flask_app):
    """Returns 403 when ENABLE_OLLAMA_MANAGEMENT is False."""
    flask_app.config["ENABLE_OLLAMA_MANAGEMENT"] = False

    resp = await client.get("/api/v1/ollama/deployments", headers=auth_headers)

    assert resp.status_code == 403
    assert "disabled" in (await resp.get_json())["error"].lower()


# ============================================================================
# GET /ollama/deployments/<id> - Get Single Deployment
# ============================================================================


async def test_get_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can get a specific deployment with its models."""
    dep = make_mock_deployment(dep_id=1, name="test-dep")
    model1 = make_mock_model(model_id=1, name="llama3.2")
    model2 = make_mock_model(model_id=2, name="mistral")

    # Mock first select() for deployment, second for models
    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([model1, model2]),
    ]

    resp = await client.get("/api/v1/ollama/deployments/1", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["id"] == 1
    assert data["name"] == "test-dep"
    assert len(data["models"]) == 2
    assert data["models"][0]["model_name"] == "llama3.2"


async def test_get_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999", headers=auth_headers)

    assert resp.status_code == 404
    assert "not found" in (await resp.get_json())["error"].lower()


async def test_get_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments/1", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments - Create Deployment
# ============================================================================


async def test_create_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can create a new deployment."""
    # Mock: no existing deployment
    app_mock_db.return_value.select.return_value = make_select_result([])
    # Mock: insert returns ID 5
    app_mock_db.ollama_deployments.insert.return_value = 5

    payload = {
        "name": "new-deploy",
        "endpoint_url": "http://ollama:11434",
        "deployment_type": "external",
    }

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 201
    data = await resp.get_json()
    assert isinstance(data.get("id"), int)
    assert data["name"] == "new-deploy"
    assert data["deployment_type"] == "external"
    assert "message" in data


async def test_create_ollama_deployment_duplicate_name(client, app_mock_db, auth_headers):
    """Cannot create deployment with duplicate name."""
    existing = make_mock_deployment(name="existing")
    app_mock_db.return_value.select.return_value = make_select_result([existing])

    payload = {"name": "existing", "endpoint_url": "http://ollama:11434"}

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 409
    assert "exists" in (await resp.get_json())["error"].lower()


async def test_create_ollama_deployment_missing_name(client, app_mock_db, auth_headers):
    """Returns 400 if 'name' is missing."""
    payload = {"endpoint_url": "http://ollama:11434"}

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 400
    assert "name" in (await resp.get_json())["error"].lower()


async def test_create_ollama_deployment_missing_endpoint(client, app_mock_db, auth_headers):
    """Returns 400 if 'endpoint_url' is missing."""
    payload = {"name": "test"}

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 400
    assert "endpoint_url" in (await resp.get_json())["error"].lower()


async def test_create_ollama_deployment_no_body(client, app_mock_db, auth_headers):
    """Returns 400 if no request body."""
    resp = await client.post("/api/v1/ollama/deployments", headers=auth_headers)

    assert resp.status_code == 400


async def test_create_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    payload = {"name": "test", "endpoint_url": "http://localhost:11434"}

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=user_auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 403


# ============================================================================
# PUT /ollama/deployments/<id> - Update Deployment
# ============================================================================


async def test_update_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can update a deployment."""
    dep = make_mock_deployment(dep_id=1, name="old-name")

    # First select: get deployment to update
    # Second select: check for duplicate name (none found)
    # Third select: get updated deployment to regenerate config (if docker type)
    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([]),  # No duplicate name
    ]

    payload = {"name": "new-name", "endpoint_url": "http://new-endpoint:11434"}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200
    assert "updated" in (await resp.get_json())["message"].lower()


async def test_update_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    payload = {"name": "new-name"}

    resp = await client.put(
        "/api/v1/ollama/deployments/999", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 404


async def test_update_ollama_deployment_duplicate_name(client, app_mock_db, auth_headers):
    """Cannot update to a name that already exists."""
    dep = make_mock_deployment(dep_id=1, name="old")
    existing = make_mock_deployment(dep_id=2, name="taken")

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),  # Get dep to update
        make_select_result([existing]),  # Check for dup name
    ]

    payload = {"name": "taken"}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 409


async def test_update_ollama_deployment_no_body(client, app_mock_db, auth_headers):
    """Returns 400 if no request body."""
    resp = await client.put("/api/v1/ollama/deployments/1", headers=auth_headers)

    assert resp.status_code == 400


async def test_update_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    payload = {"name": "new-name"}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=user_auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 403


# ============================================================================
# DELETE /ollama/deployments/<id> - Delete Deployment
# ============================================================================


async def test_delete_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can delete a deployment."""
    dep = make_mock_deployment(dep_id=1)

    app_mock_db.return_value.select.return_value = make_select_result([dep])
    app_mock_db.return_value.count.return_value = 0  # No models

    resp = await client.delete("/api/v1/ollama/deployments/1", headers=auth_headers)

    assert resp.status_code == 200
    assert "deleted" in (await resp.get_json())["message"].lower()


async def test_delete_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.delete("/api/v1/ollama/deployments/999", headers=auth_headers)

    assert resp.status_code == 404


async def test_delete_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.delete("/api/v1/ollama/deployments/1", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments/<id>/start - Start Deployment
# ============================================================================


async def test_start_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can start a Docker deployment."""
    dep = make_mock_deployment(dep_id=1)
    dep.deployment_type = "docker"
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.post("/api/v1/ollama/deployments/1/start", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert data["status"] == "running"


async def test_start_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.post("/api/v1/ollama/deployments/999/start", headers=auth_headers)

    assert resp.status_code == 404


async def test_start_ollama_deployment_external_type(client, app_mock_db, auth_headers):
    """Returns 400 if deployment is not Docker type."""
    dep = make_mock_deployment(dep_id=1)
    dep.deployment_type = "external"
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.post("/api/v1/ollama/deployments/1/start", headers=auth_headers)

    assert resp.status_code == 400
    assert "docker" in (await resp.get_json())["error"].lower()


async def test_start_ollama_deployment_manual_mode(client, app_mock_db, auth_headers, flask_app):
    """Returns 400 if in manual mode."""
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "manual"

    resp = await client.post("/api/v1/ollama/deployments/1/start", headers=auth_headers)

    assert resp.status_code == 400


async def test_start_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.post("/api/v1/ollama/deployments/1/start", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments/<id>/stop - Stop Deployment
# ============================================================================


async def test_stop_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can stop a deployment."""
    dep = make_mock_deployment(dep_id=1, status="running")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.post("/api/v1/ollama/deployments/1/stop", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert data["status"] == "stopped"


async def test_stop_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.post("/api/v1/ollama/deployments/999/stop", headers=auth_headers)

    assert resp.status_code == 404


async def test_stop_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.post("/api/v1/ollama/deployments/1/stop", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments/<id>/restart - Restart Deployment
# ============================================================================


async def test_restart_ollama_deployment_success(client, app_mock_db, auth_headers):
    """Admin can restart a deployment."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.post("/api/v1/ollama/deployments/1/restart", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert data["status"] == "running"


async def test_restart_ollama_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.post("/api/v1/ollama/deployments/999/restart", headers=auth_headers)

    assert resp.status_code == 404


async def test_restart_ollama_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.post("/api/v1/ollama/deployments/1/restart", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/deployments/<id>/health - Health Check
# ============================================================================


async def test_health_check_deployment_success(client, app_mock_db, auth_headers):
    """Admin can check deployment health."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    # OllamaDeploymentManager.health_check() makes a real httpx.Client(...).get()
    # call to the deployment's endpoint_url -- mock the client at the module level
    # so the test never hits the network.
    mock_response = MagicMock(status_code=200)
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value.get.return_value = mock_response

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client",
        return_value=mock_http_client,
    ):
        resp = await client.get("/api/v1/ollama/deployments/1/health", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert data["health_status"] == "healthy"
    assert "checked_at" in data


async def test_health_check_deployment_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/health", headers=auth_headers)

    assert resp.status_code == 404


async def test_health_check_deployment_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments/1/health", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/deployments/<id>/docker-compose - Export Docker Compose
# ============================================================================


async def test_export_docker_compose_success(client, app_mock_db, auth_headers):
    """Admin can export docker-compose config."""
    dep = make_mock_deployment(dep_id=1, name="test-dep")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/docker-compose", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert "attachment" in resp.headers.get("Content-Disposition", "")


async def test_export_docker_compose_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/docker-compose", headers=auth_headers)

    assert resp.status_code == 404


async def test_export_docker_compose_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get(
        "/api/v1/ollama/deployments/1/docker-compose", headers=user_auth_headers
    )

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/deployments/<id>/k8s-manifest - Export K8s Manifest
# ============================================================================


async def test_export_k8s_manifest_success(client, app_mock_db, auth_headers):
    """Admin can export Kubernetes manifest."""
    dep = make_mock_deployment(dep_id=1, name="test-dep")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/k8s-manifest", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert "attachment" in resp.headers.get("Content-Disposition", "")


async def test_export_k8s_manifest_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/k8s-manifest", headers=auth_headers)

    assert resp.status_code == 404


async def test_export_k8s_manifest_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments/1/k8s-manifest", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/deployments/<id>/models - List Models
# ============================================================================


async def test_list_ollama_models_success(client, app_mock_db, auth_headers):
    """Admin can list models in a deployment."""
    dep = make_mock_deployment(dep_id=1)
    model1 = make_mock_model(model_id=1, name="llama3.2")
    model2 = make_mock_model(model_id=2, name="mistral")

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([model1, model2]),
        make_select_result([]),  # route status for model1
        make_select_result([]),  # route status for model2
    ]

    resp = await client.get("/api/v1/ollama/deployments/1/models", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert len(data["models"]) == 2


async def test_list_ollama_models_empty(client, app_mock_db, auth_headers):
    """Returns empty models list if none exist."""
    dep = make_mock_deployment(dep_id=1)

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([]),
    ]

    resp = await client.get("/api/v1/ollama/deployments/1/models", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["models"] == []


async def test_list_ollama_models_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/models", headers=auth_headers)

    assert resp.status_code == 404


async def test_list_ollama_models_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments/1/models", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments/<id>/models/pull - Pull Model
# ============================================================================


async def test_pull_ollama_model_new(client, app_mock_db, auth_headers):
    """Admin can pull a new model to a deployment."""
    dep = make_mock_deployment(dep_id=1)

    # Select #1: route's own deployment existence check
    # Select #2: OllamaDeploymentManager.pull_model()'s internal deployment fetch
    # Select #3: OllamaDeploymentManager.pull_model()'s existing-model check (none)
    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([dep]),
        make_select_result([]),
    ]
    app_mock_db.ollama_models.insert.return_value = 10

    payload = {"model": "llama3.2", "tag": "latest"}

    # OllamaDeploymentManager.pull_model() makes a real httpx.Client(...).post()
    # call to the deployment's endpoint_url -- mock the client at the module level
    # so the test never hits the network.
    mock_response = MagicMock(status_code=200)
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value.post.return_value = mock_response

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client",
        return_value=mock_http_client,
    ):
        resp = await client.post(
            "/api/v1/ollama/deployments/1/models/pull",
            headers=auth_headers,
            data=json.dumps(payload),
        )

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert "llama3.2" in data["model"]
    # NOTE: adapted from the original "pulling" expectation. pull_model() is
    # fully synchronous (blocks on the httpx call, up to its 3600s timeout)
    # and only ever returns PullStatus.status of "completed" or "error" --
    # "pulling" is set on the DB row as a mid-flight state but is never the
    # value returned in the JSON body. See task report for a flagged product
    # question (should this endpoint be async/background instead?).
    assert data["status"] == "completed"


async def test_pull_ollama_model_existing(client, app_mock_db, auth_headers):
    """Pulling an existing model re-pulls it and reports completion."""
    dep = make_mock_deployment(dep_id=1)
    model = make_mock_model(model_id=5, name="llama3.2")

    # Select #1: route's own deployment existence check
    # Select #2: OllamaDeploymentManager.pull_model()'s internal deployment fetch
    # Select #3: OllamaDeploymentManager.pull_model()'s existing-model check (found)
    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([dep]),
        make_select_result([model]),  # Model already exists
    ]

    payload = {"model": "llama3.2", "tag": "latest"}

    # OllamaDeploymentManager.pull_model() makes a real httpx.Client(...).post()
    # call to the deployment's endpoint_url -- mock the client at the module level
    # so the test never hits the network.
    mock_response = MagicMock(status_code=200)
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value.post.return_value = mock_response

    with patch(
        "services.management.app.services.ollama_manager.httpx.Client",
        return_value=mock_http_client,
    ):
        resp = await client.post(
            "/api/v1/ollama/deployments/1/models/pull",
            headers=auth_headers,
            data=json.dumps(payload),
        )

    assert resp.status_code == 200
    # NOTE: adapted from the original "pulling" substring expectation -- see
    # test_pull_ollama_model_new for the full explanation. pull_model() only
    # ever returns "completed" or "error" as its final JSON status.
    assert (await resp.get_json())["status"] == "completed"


async def test_pull_ollama_model_not_found_deployment(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    payload = {"model": "llama3.2"}

    resp = await client.post(
        "/api/v1/ollama/deployments/999/models/pull", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 404


async def test_pull_ollama_model_missing_model_field(client, app_mock_db, auth_headers):
    """Returns 400 if 'model' field is missing."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    payload = {"tag": "latest"}  # Missing 'model'

    resp = await client.post(
        "/api/v1/ollama/deployments/1/models/pull", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 400


async def test_pull_ollama_model_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    payload = {"model": "llama3.2"}

    resp = await client.post(
        "/api/v1/ollama/deployments/1/models/pull",
        headers=user_auth_headers,
        data=json.dumps(payload),
    )

    assert resp.status_code == 403


# ============================================================================
# DELETE /ollama/deployments/<id>/models/<model_name> - Remove Model
# ============================================================================


async def test_remove_ollama_model_success(client, app_mock_db, auth_headers):
    """Admin can remove a model from a deployment."""
    dep = make_mock_deployment(dep_id=1)
    model = make_mock_model(model_id=5, name="llama3.2")

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([model]),
    ]

    resp = await client.delete("/api/v1/ollama/deployments/1/models/llama3.2", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["deployment_id"] == 1
    assert data["model"] == "llama3.2"


async def test_remove_ollama_model_not_found_deployment(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.delete(
        "/api/v1/ollama/deployments/999/models/llama3.2", headers=auth_headers
    )

    assert resp.status_code == 404


async def test_remove_ollama_model_not_found_model(client, app_mock_db, auth_headers):
    """Returns 404 if model not found in deployment."""
    dep = make_mock_deployment(dep_id=1)

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),
        make_select_result([]),
    ]  # Model not found

    resp = await client.delete(
        "/api/v1/ollama/deployments/1/models/nonexistent", headers=auth_headers
    )

    assert resp.status_code == 404


async def test_remove_ollama_model_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.delete(
        "/api/v1/ollama/deployments/1/models/llama3.2", headers=user_auth_headers
    )

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments - Create Deployment: extra validation/type branches
# ============================================================================


async def test_create_ollama_deployment_empty_body_dict(client, app_mock_db, auth_headers):
    """A syntactically valid but empty JSON object ({}) is falsy -> 400.

    Distinct from test_create_ollama_deployment_no_body: sending no body at
    all never reaches the handler's own `if not data` check (it fails
    earlier during Quart's request parsing). This exercises that check
    directly.
    """
    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps({})
    )

    assert resp.status_code == 400
    assert "required" in (await resp.get_json())["error"].lower()


async def test_create_ollama_deployment_docker_type_generates_compose(
    client, app_mock_db, auth_headers
):
    """Creating a 'docker' deployment generates a docker-compose config."""
    app_mock_db.return_value.select.return_value = make_select_result([])
    app_mock_db.ollama_deployments.insert.return_value = 7

    payload = {
        "name": "docker-dep",
        "endpoint_url": "http://ollama:11434",
        "deployment_type": "docker",
    }

    resp = await client.post(
        "/api/v1/ollama/deployments", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 201
    data = await resp.get_json()
    assert data["deployment_type"] == "docker"


# ============================================================================
# PUT /ollama/deployments/<id> - Update Deployment: field/branch coverage
# ============================================================================


async def test_update_ollama_deployment_empty_body_dict(client, app_mock_db, auth_headers):
    """An empty JSON object ({}) body is falsy -> 400."""
    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps({})
    )

    assert resp.status_code == 400


async def test_update_ollama_deployment_only_endpoint_url(client, app_mock_db, auth_headers):
    """Updating only endpoint_url skips the name-conflict check entirely."""
    dep = make_mock_deployment(dep_id=1, name="unchanged")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    payload = {"endpoint_url": "http://new-endpoint:11434"}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200


async def test_update_ollama_deployment_only_gpu_config_external(client, app_mock_db, auth_headers):
    """Updating gpu_config on a non-docker deployment updates fields but skips regen."""
    dep = make_mock_deployment(dep_id=1)
    dep.deployment_type = "external"

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),  # fetch deployment to update
        make_select_result([dep]),  # refetch to decide on docker-compose regen
    ]

    payload = {"gpu_config": {"count": 1, "driver": "nvidia"}}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200


async def test_update_ollama_deployment_resource_limits_docker_regenerates_compose(
    client, app_mock_db, auth_headers
):
    """Updating resource_limits on a docker deployment regenerates its compose config."""
    dep = make_mock_deployment(dep_id=1, name="docker-dep")
    dep.deployment_type = "docker"

    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),  # fetch deployment to update
        make_select_result([dep]),  # refetch -> deployment_type == "docker" -> regen
    ]

    payload = {"resource_limits": {"cpu": "8", "memory": "16G"}}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200


async def test_update_ollama_deployment_only_auto_start(client, app_mock_db, auth_headers):
    """Updating only auto_start skips the docker-compose regeneration path."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    payload = {"auto_start": False}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200


async def test_update_ollama_deployment_unrecognized_field_is_a_noop(
    client, app_mock_db, auth_headers
):
    """A body with no recognized fields leaves update_fields empty (no-op update)."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    payload = {"unrelated_field": "some-value"}

    resp = await client.put(
        "/api/v1/ollama/deployments/1", headers=auth_headers, data=json.dumps(payload)
    )

    assert resp.status_code == 200
    assert "updated" in (await resp.get_json())["message"].lower()


# ============================================================================
# POST /ollama/deployments/<id>/stop, /restart - manual-mode disabled branch
# ============================================================================


async def test_stop_ollama_deployment_manual_mode(client, app_mock_db, auth_headers, flask_app):
    """Returns 400 when orchestrated mode is disabled."""
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "manual"

    resp = await client.post("/api/v1/ollama/deployments/1/stop", headers=auth_headers)

    assert resp.status_code == 400
    assert "disabled" in (await resp.get_json())["error"].lower()


async def test_restart_ollama_deployment_manual_mode(client, app_mock_db, auth_headers, flask_app):
    """Returns 400 when orchestrated mode is disabled."""
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "manual"

    resp = await client.post("/api/v1/ollama/deployments/1/restart", headers=auth_headers)

    assert resp.status_code == 400
    assert "disabled" in (await resp.get_json())["error"].lower()


# ============================================================================
# GET /ollama/deployments/<id>/logs - Get Logs
# ============================================================================


async def test_get_ollama_logs_manual_mode(client, app_mock_db, auth_headers, flask_app):
    """Returns 400 when orchestrated mode is disabled."""
    flask_app.config["OLLAMA_MANAGEMENT_MODE"] = "manual"

    resp = await client.get("/api/v1/ollama/deployments/1/logs", headers=auth_headers)

    assert resp.status_code == 400


async def test_get_ollama_logs_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/logs", headers=auth_headers)

    assert resp.status_code == 404


async def test_get_ollama_logs_default_lines(client, app_mock_db, auth_headers):
    """Without a 'lines' query param, defaults to 100."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/logs", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["lines"] == 100
    assert data["deployment_id"] == 1


async def test_get_ollama_logs_custom_lines(client, app_mock_db, auth_headers):
    """A 'lines' query param overrides the default line count."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/logs?lines=50", headers=auth_headers)

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["lines"] == 50


async def test_get_ollama_logs_invalid_lines_falls_back_to_default(
    client, app_mock_db, auth_headers
):
    """A non-integer 'lines' query param silently falls back to the default (100)."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get(
        "/api/v1/ollama/deployments/1/logs?lines=not-a-number", headers=auth_headers
    )

    assert resp.status_code == 200
    data = await resp.get_json()
    assert data["lines"] == 100


async def test_get_ollama_logs_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/deployments/1/logs", headers=user_auth_headers)

    assert resp.status_code == 403


# ============================================================================
# POST /ollama/deployments/<id>/models/pull - manager error branch
# ============================================================================


async def test_pull_ollama_model_manager_error(client, app_mock_db, auth_headers):
    """A manager pull failure (PullStatus.error set) maps to a 500 response."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.side_effect = [
        make_select_result([dep]),  # route's own deployment existence check
    ]

    fake_status = PullStatus(
        model="llama3.2", status="error", completed=False, error="connection refused"
    )
    fake_manager = make_mock_manager(pull_model=fake_status)

    payload = {"model": "llama3.2"}

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.post(
            "/api/v1/ollama/deployments/1/models/pull",
            headers=auth_headers,
            data=json.dumps(payload),
        )

    assert resp.status_code == 500
    data = await resp.get_json()
    assert data["error"] == "connection refused"
    assert data["status"] == "error"


# ============================================================================
# GET /ollama/deployments/<id>/docker-compose - existing-config & GPU branches
# ============================================================================


async def test_export_docker_compose_uses_existing_config(client, app_mock_db, auth_headers):
    """If a stored docker_compose_config exists, it is dumped as-is (no regeneration)."""
    dep = make_mock_deployment(dep_id=1, name="test-dep")
    dep.docker_compose_config = {"version": "3.8", "services": {"stored": {}}}
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/docker-compose", headers=auth_headers)

    assert resp.status_code == 200
    text = await resp.get_data(as_text=True)
    assert "stored" in text


async def test_export_docker_compose_generates_with_gpu(client, app_mock_db, auth_headers):
    """No stored config + gpu_config.count > 0 generates a compose file with a GPU reservation."""
    dep = make_mock_deployment(dep_id=1, name="gpu-dep")
    dep.docker_compose_config = None
    dep.gpu_config = {"count": 2, "driver": "nvidia"}
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/docker-compose", headers=auth_headers)

    assert resp.status_code == 200
    text = await resp.get_data(as_text=True)
    assert "nvidia" in text
    assert "capabilities" in text


# ============================================================================
# GET /ollama/deployments/<id>/k8s-manifest - daemonset & GPU branches
# ============================================================================


async def test_export_k8s_manifest_daemonset_uses_manager(client, app_mock_db, auth_headers):
    """A 'kubernetes-daemonset' deployment delegates manifest generation to the manager."""
    dep = make_mock_deployment(dep_id=1, name="ds-dep")
    dep.deployment_type = "kubernetes-daemonset"
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    fake_manifest = "apiVersion: apps/v1\nkind: DaemonSet\n"
    fake_manager = make_mock_manager(generate_daemonset_manifest=fake_manifest)

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get("/api/v1/ollama/deployments/1/k8s-manifest", headers=auth_headers)

    assert resp.status_code == 200
    assert "daemonset" in resp.headers.get("Content-Disposition", "").lower()
    text = await resp.get_data(as_text=True)
    assert "DaemonSet" in text


async def test_export_k8s_manifest_with_gpu(client, app_mock_db, auth_headers):
    """A non-daemonset deployment with gpu_config.count > 0 requests nvidia.com/gpu limits."""
    dep = make_mock_deployment(dep_id=1, name="gpu-k8s-dep")
    dep.deployment_type = "external"
    dep.gpu_config = {"count": 3, "driver": "nvidia"}
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    resp = await client.get("/api/v1/ollama/deployments/1/k8s-manifest", headers=auth_headers)

    assert resp.status_code == 200
    text = await resp.get_data(as_text=True)
    assert "nvidia.com/gpu" in text


# ============================================================================
# GET /ollama/deployments/<id>/metallb-service - Export MetalLB Service
# ============================================================================


async def test_export_metallb_service_success(client, app_mock_db, auth_headers):
    """Admin can export a MetalLB LoadBalancer Service for a deployment."""
    dep = make_mock_deployment(dep_id=1, name="metallb-dep")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    fake_yaml = "apiVersion: v1\nkind: Service\n"
    fake_manager = make_mock_manager(generate_metallb_service=fake_yaml)

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get(
            "/api/v1/ollama/deployments/1/metallb-service", headers=auth_headers
        )

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert "attachment" in resp.headers.get("Content-Disposition", "")


async def test_export_metallb_service_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get("/api/v1/ollama/deployments/999/metallb-service", headers=auth_headers)

    assert resp.status_code == 404


async def test_export_metallb_service_no_models(client, app_mock_db, auth_headers):
    """Returns 400 if the deployment has no models assigned (empty manager output)."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    fake_manager = make_mock_manager(generate_metallb_service="")

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get(
            "/api/v1/ollama/deployments/1/metallb-service", headers=auth_headers
        )

    assert resp.status_code == 400
    assert "no models" in (await resp.get_json())["error"].lower()


async def test_export_metallb_service_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get(
        "/api/v1/ollama/deployments/1/metallb-service", headers=user_auth_headers
    )

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/deployments/<id>/metallb-model-services - Per-Model Services
# ============================================================================


async def test_export_metallb_model_services_success(client, app_mock_db, auth_headers):
    """Admin can export per-model MetalLB Services for a deployment."""
    dep = make_mock_deployment(dep_id=1, name="metallb-dep")
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    fake_yaml = "apiVersion: v1\nkind: Service\n---\napiVersion: v1\nkind: Service\n"
    fake_manager = make_mock_manager(generate_model_specific_metallb_services=fake_yaml)

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get(
            "/api/v1/ollama/deployments/1/metallb-model-services", headers=auth_headers
        )

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert "models-metallb" in resp.headers.get("Content-Disposition", "")


async def test_export_metallb_model_services_not_found(client, app_mock_db, auth_headers):
    """Returns 404 if deployment not found."""
    app_mock_db.return_value.select.return_value = make_select_result([])

    resp = await client.get(
        "/api/v1/ollama/deployments/999/metallb-model-services", headers=auth_headers
    )

    assert resp.status_code == 404


async def test_export_metallb_model_services_no_models(client, app_mock_db, auth_headers):
    """Returns 400 if the deployment has no models assigned (empty manager output)."""
    dep = make_mock_deployment(dep_id=1)
    app_mock_db.return_value.select.return_value = make_select_result([dep])

    fake_manager = make_mock_manager(generate_model_specific_metallb_services="")

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get(
            "/api/v1/ollama/deployments/1/metallb-model-services", headers=auth_headers
        )

    assert resp.status_code == 400


async def test_export_metallb_model_services_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get(
        "/api/v1/ollama/deployments/1/metallb-model-services", headers=user_auth_headers
    )

    assert resp.status_code == 403


# ============================================================================
# GET /ollama/export/metallb-all - Export All MetalLB Services
# ============================================================================


async def test_export_all_metallb_services_success(client, app_mock_db, auth_headers):
    """Admin can export MetalLB config for all active deployments."""
    fake_yaml = "apiVersion: v1\nkind: Service\n"
    fake_manager = make_mock_manager(export_metallb_config=fake_yaml)

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get("/api/v1/ollama/export/metallb-all", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.mimetype == "text/yaml"
    assert "metallb-all" in resp.headers.get("Content-Disposition", "")


async def test_export_all_metallb_services_empty(client, app_mock_db, auth_headers):
    """Returns 404 if no active deployments have models (empty manager output)."""
    fake_manager = make_mock_manager(export_metallb_config="")

    with patch(
        "services.management.app.services.ollama_manager.OllamaDeploymentManager",
        return_value=fake_manager,
    ):
        resp = await client.get("/api/v1/ollama/export/metallb-all", headers=auth_headers)

    assert resp.status_code == 404
    assert "no active" in (await resp.get_json())["error"].lower()


async def test_export_all_metallb_services_not_admin(client, app_mock_db, user_auth_headers):
    """Non-admin users get 403."""
    resp = await client.get("/api/v1/ollama/export/metallb-all", headers=user_auth_headers)

    assert resp.status_code == 403
