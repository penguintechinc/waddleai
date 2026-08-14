"""
Unit tests for AI provider management routes: /api/v1/providers/*
"""

from typing import Dict
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_provider

# ---------------------------------------------------------------------------
# GET /api/v1/providers/types
# ---------------------------------------------------------------------------


class TestListProviderTypes:
    """Tests for GET /api/v1/providers/types"""

    async def test_list_provider_types_success(self, client, auth_headers: Dict) -> None:
        """Authenticated request returns supported provider types."""
        resp = await client.get("/api/v1/providers/types", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())
        assert "provider_types" in data
        types = [p["type"] for p in data["provider_types"]]
        assert "openai" in types
        assert "anthropic" in types
        assert "ollama" in types

    async def test_list_provider_types_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/providers/types")
        assert resp.status_code == 401

    async def test_list_provider_types_enterprise_disabled(self, client, flask_app, auth_headers: Dict) -> None:
        """When Gemini is disabled it does not appear in the list."""
        original = flask_app.config.get("ENABLE_GEMINI", True)
        flask_app.config["ENABLE_GEMINI"] = False
        try:
            resp = await client.get("/api/v1/providers/types", headers=auth_headers)
            assert resp.status_code == 200
            types = [p["type"] for p in (await resp.get_json())["provider_types"]]
            assert "gemini" not in types
        finally:
            flask_app.config["ENABLE_GEMINI"] = original


# ---------------------------------------------------------------------------
# GET /api/v1/providers
# ---------------------------------------------------------------------------


class TestListProviders:
    """Tests for GET /api/v1/providers"""

    async def test_list_providers_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can list all providers."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value = make_select_result([provider])

        resp = await client.get("/api/v1/providers", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())
        assert "providers" in data

    async def test_list_providers_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot list providers → 403."""
        resp = await client.get("/api/v1/providers", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_list_providers_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/providers")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/providers/<id>
# ---------------------------------------------------------------------------


class TestGetProvider:
    """Tests for GET /api/v1/providers/<provider_id>"""

    async def test_get_provider_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get a provider by ID."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.get("/api/v1/providers/1", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())
        assert data["name"] == "Test OpenAI"

    async def test_get_provider_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/providers/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_provider_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot access → 403."""
        resp = await client.get("/api/v1/providers/1", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/providers
# ---------------------------------------------------------------------------


class TestCreateProvider:
    """Tests for POST /api/v1/providers"""

    async def test_create_provider_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can create a provider with all required fields."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={
                "name": "My OpenAI",
                "provider_type": "openai",
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "sk-testkey",
            },
        )
        assert resp.status_code == 201
        data = (await resp.get_json())
        assert isinstance(data.get("id"), int)

    async def test_create_provider_missing_required_field(self, client, auth_headers: Dict) -> None:
        """Missing endpoint_url returns 400."""
        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={"name": "Test", "provider_type": "openai"},
        )
        assert resp.status_code == 400

    async def test_create_provider_unsupported_type(self, client, auth_headers: Dict) -> None:
        """Invalid provider_type returns 400."""
        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={
                "name": "Bad Provider",
                "provider_type": "unsupported_llm",
                "endpoint_url": "https://example.com",
            },
        )
        assert resp.status_code == 400

    async def test_create_provider_missing_api_key_for_required_type(self, client, auth_headers: Dict) -> None:
        """OpenAI without api_key returns 400."""
        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={
                "name": "No Key OpenAI",
                "provider_type": "openai",
                "endpoint_url": "https://api.openai.com/v1",
            },
        )
        assert resp.status_code == 400

    async def test_create_provider_duplicate_name(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Duplicate provider name returns 409."""
        existing = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = existing

        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={
                "name": "Test OpenAI",
                "provider_type": "openai",
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "sk-test",
            },
        )
        assert resp.status_code == 409

    async def test_create_provider_ollama_no_key_required(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Ollama provider does not require an API key."""
        app_mock_db.return_value.select.return_value.first.return_value = None
        app_mock_db.ai_providers.insert.return_value = 6

        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={
                "name": "Local Ollama",
                "provider_type": "ollama",
                "endpoint_url": "http://localhost:11434",
            },
        )
        assert resp.status_code == 201

    async def test_create_provider_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot create providers → 403."""
        resp = await client.post(
            "/api/v1/providers",
            headers=user_auth_headers,
            json={
                "name": "x",
                "provider_type": "openai",
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "sk-x",
            },
        )
        assert resp.status_code == 403

    async def test_create_provider_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            data=""
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/v1/providers/<id>
# ---------------------------------------------------------------------------


class TestUpdateProvider:
    """Tests for PUT /api/v1/providers/<provider_id>"""

    async def test_update_provider_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can update provider fields."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.put(
            "/api/v1/providers/1",
            headers=auth_headers,
            json={"priority": 50},
        )
        assert resp.status_code == 200

    async def test_update_provider_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/providers/999",
            headers=auth_headers,
            json={"priority": 50},
        )
        assert resp.status_code == 404

    async def test_update_provider_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.put(
            "/api/v1/providers/1",
            headers=auth_headers,
            data=""
        )
        assert resp.status_code == 400

    async def test_update_provider_name_conflict(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Duplicate name returns 409."""
        provider = make_mock_provider()
        other = make_mock_provider(provider_id=99, name="Other Provider")
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, other]

        resp = await client.put(
            "/api/v1/providers/1",
            headers=auth_headers,
            json={"name": "Other Provider"},
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/v1/providers/<id>
# ---------------------------------------------------------------------------


class TestDeleteProvider:
    """Tests for DELETE /api/v1/providers/<provider_id>"""

    async def test_delete_provider_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can soft-delete a provider."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.delete("/api/v1/providers/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_provider_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/providers/999", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/providers/<id>/test
# ---------------------------------------------------------------------------


class TestTestProvider:
    """Tests for POST /api/v1/providers/<provider_id>/test"""

    async def test_test_provider_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin gets connectivity test result."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.post("/api/v1/providers/1/test", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())
        assert data["status"] == "connected"

    async def test_test_provider_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post("/api/v1/providers/999/test", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/providers/<id>/sync, GET .../sync-status
# -- removed (AILB retired, migration 007 dropped marchproxy_ailb_sync)
# ---------------------------------------------------------------------------


class TestProviderSyncEndpointsRemoved:
    """Both AILB provider-sync routes had no successor and are gone."""

    async def test_sync_provider_endpoint_no_longer_exists(self, client, auth_headers: Dict) -> None:
        """The route is unregistered -- Quart returns 404, not 200/400/404-with-body."""
        resp = await client.post("/api/v1/providers/1/sync", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_sync_status_endpoint_no_longer_exists(self, client, auth_headers: Dict) -> None:
        """The route is unregistered -- Quart returns 404."""
        resp = await client.get("/api/v1/providers/1/sync-status", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/providers/<id>/models
# ---------------------------------------------------------------------------


class TestGetProviderModels:
    """Tests for GET /api/v1/providers/<provider_id>/models"""

    async def test_get_provider_models_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Returns configured model list."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.get("/api/v1/providers/1/models", headers=auth_headers)
        assert resp.status_code == 200
        data = (await resp.get_json())
        assert "models" in data

    async def test_get_provider_models_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/providers/999/models", headers=auth_headers)
        assert resp.status_code == 404
