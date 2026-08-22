"""Unit tests for AI provider management routes: /api/v1/providers/*."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_provider


def make_mock_credential(
    cred_id: int = 1,
    provider_id: int = 1,
    label: str = "Primary",
    api_key: str | None = "sk-existing12345",
    org_id: str | None = None,
    weight: int = 100,
    enabled: bool = True,
) -> MagicMock:
    """Return a MagicMock representing a db provider_credentials row.

    Local to this test module (not route_conftest.py) since no other
    route-test module needs a provider_credentials row.
    """
    cred = MagicMock()
    cred.id = cred_id
    cred.provider_id = provider_id
    cred.label = label
    cred.api_key = api_key
    cred.org_id = org_id
    cred.account_meta = None
    cred.weight = weight
    cred.enabled = enabled
    cred.request_count = 0
    cred.token_count = 0
    cred.last_used_at = None
    cred.created_at = datetime(2025, 1, 1, 12, 0, 0)
    return cred


# ---------------------------------------------------------------------------
# GET /api/v1/providers/types
# ---------------------------------------------------------------------------


class TestListProviderTypes:
    """Tests for GET /api/v1/providers/types."""

    async def test_list_provider_types_success(self, client, auth_headers: dict) -> None:
        """Authenticated request returns supported provider types."""
        resp = await client.get("/api/v1/providers/types", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "provider_types" in data
        types = [p["type"] for p in data["provider_types"]]
        assert "openai" in types
        assert "anthropic" in types
        assert "ollama" in types

    async def test_list_provider_types_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/providers/types")
        assert resp.status_code == 401

    async def test_list_provider_types_enterprise_disabled(
        self, client, flask_app, auth_headers: dict
    ) -> None:
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
    """Tests for GET /api/v1/providers."""

    async def test_list_providers_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can list all providers."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value = make_select_result([provider])

        resp = await client.get("/api/v1/providers", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "providers" in data

    async def test_list_providers_non_admin_forbidden(
        self, client, user_auth_headers: dict
    ) -> None:
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
    """Tests for GET /api/v1/providers/<provider_id>."""

    async def test_get_provider_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can get a provider by ID."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.get("/api/v1/providers/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["name"] == "Test OpenAI"

    async def test_get_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/providers/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_provider_non_admin_forbidden(self, client, user_auth_headers: dict) -> None:
        """Regular user cannot access → 403."""
        resp = await client.get("/api/v1/providers/1", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/providers
# ---------------------------------------------------------------------------


class TestCreateProvider:
    """Tests for POST /api/v1/providers."""

    async def test_create_provider_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        data = await resp.get_json()
        assert isinstance(data.get("id"), int)

    async def test_create_provider_missing_required_field(self, client, auth_headers: dict) -> None:
        """Missing endpoint_url returns 400."""
        resp = await client.post(
            "/api/v1/providers",
            headers=auth_headers,
            json={"name": "Test", "provider_type": "openai"},
        )
        assert resp.status_code == 400

    async def test_create_provider_unsupported_type(self, client, auth_headers: dict) -> None:
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

    async def test_create_provider_missing_api_key_for_required_type(
        self, client, auth_headers: dict
    ) -> None:
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

    async def test_create_provider_duplicate_name(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_create_provider_ollama_no_key_required(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_create_provider_non_admin_forbidden(
        self, client, user_auth_headers: dict
    ) -> None:
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

    async def test_create_provider_no_body(self, client, auth_headers: dict) -> None:
        """No body returns 400."""
        resp = await client.post("/api/v1/providers", headers=auth_headers, data="")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/v1/providers/<id>
# ---------------------------------------------------------------------------


class TestUpdateProvider:
    """Tests for PUT /api/v1/providers/<provider_id>."""

    async def test_update_provider_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can update provider fields."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.put(
            "/api/v1/providers/1",
            headers=auth_headers,
            json={"priority": 50},
        )
        assert resp.status_code == 200

    async def test_update_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/providers/999",
            headers=auth_headers,
            json={"priority": 50},
        )
        assert resp.status_code == 404

    async def test_update_provider_no_body(self, client, auth_headers: dict) -> None:
        """No body returns 400."""
        resp = await client.put("/api/v1/providers/1", headers=auth_headers, data="")
        assert resp.status_code == 400

    async def test_update_provider_name_conflict(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
    """Tests for DELETE /api/v1/providers/<provider_id>."""

    async def test_delete_provider_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can soft-delete a provider."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.delete("/api/v1/providers/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/providers/999", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/providers/<id>/test
# ---------------------------------------------------------------------------


class TestTestProvider:
    """Tests for POST /api/v1/providers/<provider_id>/test."""

    async def test_test_provider_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin gets connectivity test result."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.post("/api/v1/providers/1/test", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "connected"

    async def test_test_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_sync_provider_endpoint_no_longer_exists(
        self, client, auth_headers: dict
    ) -> None:
        """The route is unregistered -- Quart returns 404, not 200/400/404-with-body."""
        resp = await client.post("/api/v1/providers/1/sync", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_sync_status_endpoint_no_longer_exists(
        self, client, auth_headers: dict
    ) -> None:
        """The route is unregistered -- Quart returns 404."""
        resp = await client.get("/api/v1/providers/1/sync-status", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/providers/<id>/models
# ---------------------------------------------------------------------------


class TestGetProviderModels:
    """Tests for GET /api/v1/providers/<provider_id>/models."""

    async def test_get_provider_models_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Returns configured model list."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.get("/api/v1/providers/1/models", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "models" in data

    async def test_get_provider_models_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/providers/999/models", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/providers/types -- enterprise-gating branches beyond Gemini
# ---------------------------------------------------------------------------


class TestListProviderTypesMoreGating:
    """Covers the bedrock/azure_openai/cohere disable branches (Gemini already covered above)."""

    @pytest.mark.parametrize(
        "config_key,provider_type",
        [
            ("ENABLE_BEDROCK", "bedrock"),
            ("ENABLE_AZURE_OPENAI", "azure_openai"),
            ("ENABLE_COHERE", "cohere"),
        ],
    )
    async def test_disabled_provider_type_excluded(
        self, client, flask_app, auth_headers: dict, config_key: str, provider_type: str
    ) -> None:
        """When a licensed provider type is disabled it is excluded from the list."""
        original = flask_app.config.get(config_key, True)
        flask_app.config[config_key] = False
        try:
            resp = await client.get("/api/v1/providers/types", headers=auth_headers)
            assert resp.status_code == 200
            types = [p["type"] for p in (await resp.get_json())["provider_types"]]
            assert provider_type not in types
        finally:
            flask_app.config[config_key] = original


# ---------------------------------------------------------------------------
# Role-scope isolation.
#
# ai_providers has no org_id column -- it is a platform-wide resource, not
# org-scoped, so there is no org-A-vs-org-B row to test here (confirmed by
# reading services/management/app/models_sqlalchemy.py and every query in
# providers.py: all filter on ai_providers.id only). The security boundary
# this module actually enforces is role/scope: PROVIDER_ADMIN is granted to
# Role.ADMIN only (shared/auth/rbac.py ROLE_PERMISSIONS) -- resource_manager
# holds most other admin-tier route-scoped permissions but NOT this one, so
# it must be rejected identically to a plain user.
# ---------------------------------------------------------------------------


class TestProviderResourceManagerScopeIsolation:
    """resource_manager lacks PROVIDER_ADMIN -- verify it is rejected on every provider route."""

    async def test_resource_manager_cannot_list_providers(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager gets 403, not the admin-only provider list."""
        resp = await client.get("/api/v1/providers", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_resource_manager_cannot_get_provider(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager gets 403 reading a single provider."""
        resp = await client.get("/api/v1/providers/1", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_resource_manager_cannot_create_provider(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager gets 403 creating a provider."""
        resp = await client.post(
            "/api/v1/providers",
            headers=rm_auth_headers,
            json={
                "name": "x",
                "provider_type": "openai",
                "endpoint_url": "https://api.openai.com/v1",
                "api_key": "sk-x",
            },
        )
        assert resp.status_code == 403

    async def test_resource_manager_cannot_list_provider_credentials(
        self, client, rm_auth_headers: dict
    ) -> None:
        """resource_manager gets 403 on the credentials sub-resource too."""
        resp = await client.get("/api/v1/providers/1/credentials", headers=rm_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/providers/<id> -- every optional field branch
# ---------------------------------------------------------------------------


class TestUpdateProviderAllFields:
    """Covers every optional-field branch in update_provider not hit by the single-field tests."""

    async def test_update_provider_all_fields_except_priority(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Every optional field but priority takes its 'provided' branch; priority stays unset."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.put(
            "/api/v1/providers/1",
            headers=auth_headers,
            json={
                "name": "Renamed Provider",
                "endpoint_url": "https://api.openai.com/v2",
                "api_key": "sk-rotated",
                "model_list": ["gpt-4o"],
                "rate_limits": {"tpm_limit": 5000},
                "enabled": False,
                "extra_config": {"region": "us-east-1"},
                "tls_config": {"verify": True},
                "ailb_sync_enabled": False,
                "ailb_route_config": {"weight": 10},
            },
        )
        assert resp.status_code == 200

        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert update_kwargs["name"] == "Renamed Provider"
        assert update_kwargs["endpoint_url"] == "https://api.openai.com/v2"
        assert update_kwargs["api_key"] == "sk-rotated"
        assert update_kwargs["model_list"] == ["gpt-4o"]
        assert update_kwargs["rate_limits"] == {"tpm_limit": 5000}
        assert update_kwargs["enabled"] is False
        assert update_kwargs["extra_config"] == {"region": "us-east-1"}
        assert update_kwargs["tls_config"] == {"verify": True}
        assert update_kwargs["ailb_sync_enabled"] is False
        assert update_kwargs["ailb_route_config"] == {"weight": 10}
        assert "priority" not in update_kwargs

    async def test_update_provider_empty_payload_updates_nothing(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty PUT body finds the provider but performs no field update."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.put("/api/v1/providers/1", headers=auth_headers, json={})
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/v1/providers/<id>/credentials
# ---------------------------------------------------------------------------


class TestListProviderCredentials:
    """Tests for GET /api/v1/providers/<provider_id>/credentials."""

    async def test_list_credentials_success_masks_every_secret(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Every credential's api_key never appears raw; the four masking branches all fire."""
        provider = make_mock_provider()
        creds = [
            make_mock_credential(cred_id=1, api_key=None),  # falsy -> ""
            make_mock_credential(cred_id=2, api_key="short12"),  # <=8 chars raw -> "****"
            make_mock_credential(cred_id=3, api_key="enc:" + "z" * 40),  # enc: prefix stripped
            make_mock_credential(cred_id=4, api_key="sk-plaintext-long-key-1234"),  # normal mask
        ]
        sel = make_select_result(creds)
        sel.first.return_value = provider
        app_mock_db.return_value.select.return_value = sel

        resp = await client.get("/api/v1/providers/1/credentials", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        assert body["meta"]["provider_id"] == 1
        assert body["meta"]["total"] == 4

        masks = [c["api_key_masked"] for c in body["data"]]
        assert masks[0] == ""
        assert masks[1] == "****"
        assert masks[2] == "zzzz****zzzz"
        assert masks[3] == "sk-p****1234"
        for cred in body["data"]:
            assert "api_key" not in cred

    async def test_list_credentials_provider_not_found_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404 before any credential query runs."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/providers/999/credentials", headers=auth_headers)
        assert resp.status_code == 404
        body = await resp.get_json()
        assert body["status"] == "error"


# ---------------------------------------------------------------------------
# POST /api/v1/providers/<id>/credentials
# ---------------------------------------------------------------------------


class TestCreateProviderCredential:
    """Tests for POST /api/v1/providers/<provider_id>/credentials."""

    async def test_create_provider_credential_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404 before any validation runs."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/providers/999/credentials",
            headers=auth_headers,
            json={"label": "Backup", "api_key": "sk-new"},
        )
        assert resp.status_code == 404

    async def test_create_provider_credential_empty_label_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A whitespace-only label is rejected with 400, house error envelope."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "   ", "api_key": "sk-new"},
        )
        assert resp.status_code == 400
        body = await resp.get_json()
        assert body["status"] == "error"

    async def test_create_provider_credential_label_too_long_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A label over 255 characters is rejected with 400."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "x" * 256, "api_key": "sk-new"},
        )
        assert resp.status_code == 400

    async def test_create_provider_credential_missing_api_key_required_type(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Openai requires an api_key -- omitting it returns 400."""
        provider = make_mock_provider(provider_type="openai")
        app_mock_db.return_value.select.return_value.first.return_value = provider

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "Backup"},
        )
        assert resp.status_code == 400
        body = await resp.get_json()
        assert body["status"] == "error"

    async def test_create_provider_credential_ollama_no_api_key_required(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Ollama does not require an api_key -- credential is created with a null key."""
        provider = make_mock_provider(provider_type="ollama")
        new_cred = make_mock_credential(cred_id=7, provider_id=1, label="Local", api_key=None)
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None, new_cred]
        app_mock_db.provider_credentials.insert.return_value = 7

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "Local"},
        )
        assert resp.status_code == 201
        insert_kwargs = app_mock_db.provider_credentials.insert.call_args.kwargs
        assert insert_kwargs["api_key"] is None

    async def test_create_provider_credential_label_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A duplicate label within the same provider returns 409."""
        provider = make_mock_provider()
        existing = make_mock_credential(cred_id=2, label="Backup")
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, existing]

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "Backup", "api_key": "sk-new"},
        )
        assert resp.status_code == 409

    async def test_create_provider_credential_invalid_weight_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A weight outside 1-10000 returns 400."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "Backup", "api_key": "sk-new", "weight": 20000},
        )
        assert resp.status_code == 400

    async def test_create_provider_credential_success_returns_masked_secret(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A created credential is encrypted before insert and the response never carries it raw."""
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")
        provider = make_mock_provider()
        new_cred = make_mock_credential(
            cred_id=9,
            provider_id=1,
            label="Backup",
            api_key="enc:whatever-ciphertext",
            weight=50,
        )
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None, new_cred]
        app_mock_db.provider_credentials.insert.return_value = 9

        resp = await client.post(
            "/api/v1/providers/1/credentials",
            headers=auth_headers,
            json={"label": "Backup", "api_key": "sk-real-secret-value", "weight": 50},
        )
        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["status"] == "success"
        assert body["meta"]["action"] == "created"
        assert body["data"]["id"] == 9
        assert "api_key" not in body["data"]
        assert body["data"]["api_key_masked"] != ""
        assert "sk-real-secret-value" not in body["data"]["api_key_masked"]

        insert_kwargs = app_mock_db.provider_credentials.insert.call_args.kwargs
        stored_key = insert_kwargs["api_key"]
        assert stored_key.startswith("enc:")
        assert "sk-real-secret-value" not in stored_key


# ---------------------------------------------------------------------------
# PATCH /api/v1/providers/<id>/credentials/<cred_id>
# ---------------------------------------------------------------------------


class TestUpdateProviderCredential:
    """Tests for PATCH /api/v1/providers/<provider_id>/credentials/<cred_id>."""

    async def test_update_provider_credential_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.patch(
            "/api/v1/providers/999/credentials/1", headers=auth_headers, json={"weight": 50}
        )
        assert resp.status_code == 404

    async def test_update_provider_credential_rejects_cross_org_id(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A cred_id belonging to a different provider_id is treated as not found (no IDOR leak)."""
        provider = make_mock_provider(provider_id=1)
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/999", headers=auth_headers, json={"weight": 50}
        )
        assert resp.status_code == 404
        body = await resp.get_json()
        assert body["status"] == "error"
        assert body["error"] == "Credential not found"

    async def test_update_provider_credential_invalid_label_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An over-long label is rejected with 400 before any write."""
        provider = make_mock_provider()
        cred = make_mock_credential()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1", headers=auth_headers, json={"label": "x" * 300}
        )
        assert resp.status_code == 400

    async def test_update_provider_credential_invalid_weight_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A weight outside 1-10000 is rejected with 400."""
        provider = make_mock_provider()
        cred = make_mock_credential()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1", headers=auth_headers, json={"weight": -1}
        )
        assert resp.status_code == 400

    async def test_update_provider_credential_empty_api_key_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A whitespace-only api_key is rejected with 400 -- never stored, never rotated."""
        provider = make_mock_provider()
        cred = make_mock_credential()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1", headers=auth_headers, json={"api_key": "   "}
        )
        assert resp.status_code == 400

    async def test_update_provider_credential_no_fields_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An empty PATCH body has nothing to update and returns 400."""
        provider = make_mock_provider()
        cred = make_mock_credential()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1", headers=auth_headers, json={}
        )
        assert resp.status_code == 400
        body = await resp.get_json()
        assert body["error"] == "No valid fields to update"

    async def test_update_provider_credential_label_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Renaming to a label already used by a sibling credential returns 409."""
        provider = make_mock_provider()
        cred = make_mock_credential(cred_id=1, label="Primary")
        other = make_mock_credential(cred_id=2, label="Taken")
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred, other]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1", headers=auth_headers, json={"label": "Taken"}
        )
        assert resp.status_code == 409

    async def test_update_provider_credential_success_rotates_secret(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Rotating only api_key encrypts the new value; the response never carries it raw."""
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")
        provider = make_mock_provider()
        cred = make_mock_credential(cred_id=1, label="Primary")
        updated = make_mock_credential(cred_id=1, label="Primary", api_key="enc:rotated-ciphertext")
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, cred, updated]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1",
            headers=auth_headers,
            json={"api_key": "sk-brand-new-secret"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        assert "api_key" not in body["data"]
        assert "sk-brand-new-secret" not in body["data"]["api_key_masked"]

        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert set(update_kwargs) == {"api_key"}
        assert update_kwargs["api_key"].startswith("enc:")
        assert "sk-brand-new-secret" not in update_kwargs["api_key"]

    async def test_update_provider_credential_success_all_optional_fields(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """label/weight/enabled/org_id/account_meta all update together with no label conflict."""
        provider = make_mock_provider()
        cred = make_mock_credential(cred_id=1, label="Primary")
        updated = make_mock_credential(cred_id=1, label="Renamed")
        app_mock_db.return_value.select.return_value.first.side_effect = [
            provider,
            cred,
            None,  # label-conflict check: no conflict
            updated,
        ]

        resp = await client.patch(
            "/api/v1/providers/1/credentials/1",
            headers=auth_headers,
            json={
                "label": "Renamed",
                "weight": 250,
                "enabled": False,
                "org_id": "acct-99",
                "account_meta": {"region": "eu-west-1"},
            },
        )
        assert resp.status_code == 200

        update_kwargs = app_mock_db.return_value.update.call_args.kwargs
        assert update_kwargs == {
            "label": "Renamed",
            "weight": 250,
            "enabled": False,
            "org_id": "acct-99",
            "account_meta": {"region": "eu-west-1"},
        }


# ---------------------------------------------------------------------------
# DELETE /api/v1/providers/<id>/credentials/<cred_id>
# ---------------------------------------------------------------------------


class TestDeleteProviderCredential:
    """Tests for DELETE /api/v1/providers/<provider_id>/credentials/<cred_id>."""

    async def test_delete_provider_credential_provider_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing provider returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/providers/999/credentials/1", headers=auth_headers)
        assert resp.status_code == 404
        body = await resp.get_json()
        assert body["error"] == "Provider not found"

    async def test_delete_provider_credential_not_found_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing credential (or one belonging to a different provider) returns 404."""
        provider = make_mock_provider()
        app_mock_db.return_value.select.return_value.first.side_effect = [provider, None]

        resp = await client.delete("/api/v1/providers/1/credentials/999", headers=auth_headers)
        assert resp.status_code == 404
        body = await resp.get_json()
        assert body["error"] == "Credential not found"

    async def test_delete_provider_credential_last_one_refused(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Deleting the last remaining credential for a provider is refused with 409."""
        provider = make_mock_provider()
        cred = make_mock_credential(cred_id=1)
        sel = app_mock_db.return_value.select.return_value
        sel.first.side_effect = [provider, cred]
        sel.__len__ = MagicMock(return_value=1)

        resp = await client.delete("/api/v1/providers/1/credentials/1", headers=auth_headers)
        assert resp.status_code == 409
        app_mock_db.return_value.delete.assert_not_called()

    async def test_delete_provider_credential_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """With more than one credential remaining, delete succeeds and returns the deleted id."""
        provider = make_mock_provider()
        cred = make_mock_credential(cred_id=1)
        sel = app_mock_db.return_value.select.return_value
        sel.first.side_effect = [provider, cred]
        sel.__len__ = MagicMock(return_value=2)

        resp = await client.delete("/api/v1/providers/1/credentials/1", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        assert body["data"]["id"] == 1
        assert body["meta"]["action"] == "deleted"
        app_mock_db.return_value.delete.assert_called_once()
