"""Unit tests for the §10.1/§10.4 fleet_backends registry management routes.

`/api/v1/fleet/backends` CRUD (org-scoped, admin only, credentials
encrypted/never echoed, two-layer Pro gating on vertex_ai/bedrock), plus
`/health` surfaced through the InferenceFleetBackend interface.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from shared.fleet.base import FleetHealth
from tests.unit.management.conftest import make_select_result

ENDPOINT_PATH = "/api/v1/fleet/backends"


def _enable_flag(monkeypatch) -> None:
    """Turn `waddleai.fleet_v2` on for the duration of one test."""
    monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "1")


def make_mock_backend(
    backend_id: int = 1,
    org_id: int = 1,
    name: str = "primary-ollama",
    backend_type: str = "ollama",
    mode: str | None = None,
    management_scope: str = "full_lifecycle",
    config: dict | None = None,
    credentials_ref: str | None = None,
    status: str = "pending",
) -> MagicMock:
    """Return a MagicMock representing a `fleet_backends` row."""
    row = MagicMock()
    row.id = backend_id
    row.org_id = org_id
    row.name = name
    row.type = backend_type
    row.mode = mode
    row.management_scope = management_scope
    row.config = config if config is not None else {}
    row.credentials_ref = credentials_ref
    row.status = status
    row.created_at = datetime(2026, 1, 1, 12, 0, 0)
    row.updated_at = datetime(2026, 1, 1, 12, 0, 0)
    return row


def _entitled(monkeypatch, entitled: bool) -> None:
    """Patch the hybrid_targets license-entitlement check for one test."""
    mock_client = MagicMock()
    mock_client.check_feature.return_value = entitled
    monkeypatch.setattr(
        "services.management.app.api.v1.fleet._get_license_client", lambda: mock_client
    )


class TestListFleetBackends:
    """GET /api/v1/fleet/backends."""

    async def test_requires_admin(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict, monkeypatch
    ) -> None:
        """A non-admin caller is rejected with 403."""
        _enable_flag(monkeypatch)
        resp = await client.get(ENDPOINT_PATH, headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_requires_auth(self, client) -> None:
        """An unauthenticated caller is rejected with 401."""
        resp = await client.get(ENDPOINT_PATH)
        assert resp.status_code == 401

    async def test_flag_off_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """With `waddleai.fleet_v2` off, the endpoint is inert (404)."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "0")
        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 404

    async def test_lists_org_scoped_backends(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Listing returns the org's fleet backends."""
        _enable_flag(monkeypatch)
        backend = make_mock_backend()
        app_mock_db.return_value.select.return_value = make_select_result([backend])

        resp = await client.get(ENDPOINT_PATH, headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["status"] == "success"
        assert len(body["data"]) == 1
        assert body["data"][0]["name"] == "primary-ollama"
        assert body["data"][0]["management_scope"] == "full_lifecycle"


class TestCreateFleetBackend:
    """POST /api/v1/fleet/backends."""

    def _payload(self, **overrides) -> dict:
        """A valid ollama fleet-backend creation payload, with field overrides."""
        payload = {
            "name": "primary-ollama",
            "type": "ollama",
            "management_scope": "full_lifecycle",
            "config": {"pool": "gpu-a"},
        }
        payload.update(overrides)
        return payload

    async def test_requires_admin(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict, monkeypatch
    ) -> None:
        """A non-admin caller is rejected with 403."""
        _enable_flag(monkeypatch)
        resp = await client.post(ENDPOINT_PATH, headers=user_auth_headers, json=self._payload())
        assert resp.status_code == 403

    async def test_flag_off_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """With `waddleai.fleet_v2` off, the endpoint is inert (404)."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "0")
        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, json=self._payload())
        assert resp.status_code == 404

    async def test_creates_ollama_backend_at_any_tier(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A non-Pro-gated type (ollama/llamacpp/exo) needs no license entitlement."""
        _enable_flag(monkeypatch)
        empty_sel = make_select_result([])
        created_row = make_mock_backend()
        created_sel = make_select_result([created_row])
        app_mock_db.return_value.select.side_effect = [empty_sel, created_sel]
        app_mock_db.fleet_backends.insert.return_value = 1

        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, json=self._payload())
        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["status"] == "success"
        assert body["data"]["type"] == "ollama"

        insert_kwargs = app_mock_db.fleet_backends.insert.call_args.kwargs
        assert insert_kwargs["org_id"] == 1  # from the token, never the request body

    async def test_rejects_invalid_type(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An unrecognized backend type is rejected with 400."""
        _enable_flag(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload(type="not-a-real-backend")
        )
        assert resp.status_code == 400

    async def test_rejects_invalid_management_scope(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An unrecognized management_scope value is rejected with 400."""
        _enable_flag(monkeypatch)
        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json=self._payload(management_scope="not-a-real-scope"),
        )
        assert resp.status_code == 400

    async def test_name_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Registering a name already used by this org returns 409."""
        _enable_flag(monkeypatch)
        existing = make_mock_backend()
        app_mock_db.return_value.select.return_value = make_select_result([existing])

        resp = await client.post(ENDPOINT_PATH, headers=auth_headers, json=self._payload())
        assert resp.status_code == 409

    async def test_encrypts_credentials_before_storage(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A created backend's `credentials` field is encrypted before insert, never echoed."""
        _enable_flag(monkeypatch)
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-encryption-key")
        empty_sel = make_select_result([])
        created_row = make_mock_backend(credentials_ref="enc:whatever")
        created_sel = make_select_result([created_row])
        app_mock_db.return_value.select.side_effect = [empty_sel, created_sel]
        app_mock_db.fleet_backends.insert.return_value = 1

        resp = await client.post(
            ENDPOINT_PATH,
            headers=auth_headers,
            json=self._payload(credentials="plaintext-service-account-key"),
        )
        assert resp.status_code == 201

        insert_kwargs = app_mock_db.fleet_backends.insert.call_args.kwargs
        assert insert_kwargs["credentials_ref"].startswith("enc:")
        assert insert_kwargs["credentials_ref"] != "plaintext-service-account-key"

        body = await resp.get_json()
        assert body["data"]["credentials_ref"] != "plaintext-service-account-key"


class TestVertexBedrockProGating:
    """Two-layer gate (flag AND check_feature) on vertex_ai/bedrock creation, spec §14.6."""

    def _payload(self, backend_type: str) -> dict:
        """A valid vertex_ai/bedrock fleet-backend creation payload."""
        return {
            "name": f"{backend_type}-backend",
            "type": backend_type,
            "management_scope": "register_and_route",
            "config": {"project_id": "waddleai-prod"},
        }

    async def test_community_tier_blocked_even_with_flag_on(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """No hybrid_targets entitlement -> 403 naming the tier requirement, flag on or not."""
        _enable_flag(monkeypatch)
        _entitled(monkeypatch, entitled=False)

        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload("vertex_ai")
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert "professional" in body["error"].lower() or "license" in body["error"].lower()

    async def test_professional_tier_with_flag_on_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Flag on + hybrid_targets entitled -> creation succeeds."""
        _enable_flag(monkeypatch)
        _entitled(monkeypatch, entitled=True)
        empty_sel = make_select_result([])
        created_row = make_mock_backend(
            backend_type="bedrock", management_scope="register_and_route"
        )
        created_sel = make_select_result([created_row])
        app_mock_db.return_value.select.side_effect = [empty_sel, created_sel]
        app_mock_db.fleet_backends.insert.return_value = 1

        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload("bedrock")
        )
        assert resp.status_code == 201

    async def test_flag_off_blocks_before_entitlement_is_even_checked(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Flag off short-circuits to 404 without ever calling check_feature."""
        monkeypatch.setenv("WADDLEAI_FLAG_FLEET_V2", "0")
        mock_client = MagicMock()
        monkeypatch.setattr(
            "services.management.app.api.v1.fleet._get_license_client", lambda: mock_client
        )

        resp = await client.post(
            ENDPOINT_PATH, headers=auth_headers, json=self._payload("vertex_ai")
        )
        assert resp.status_code == 404
        mock_client.check_feature.assert_not_called()


class TestGetUpdateDeleteFleetBackend:
    """GET/PUT/DELETE /api/v1/fleet/backends/<id>."""

    async def test_get_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A non-existent backend id returns 404."""
        _enable_flag(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get(f"{ENDPOINT_PATH}/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A backend belonging to another org returns 403, not 404."""
        _enable_flag(monkeypatch)
        foreign = make_mock_backend(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.get(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 403

    async def test_get_own_org_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A backend belonging to the caller's own org is fetched successfully."""
        _enable_flag(monkeypatch)
        backend = make_mock_backend(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([backend])
        resp = await client.get(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_update_own_org_persists_and_echoes_management_scope(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Updating management_scope persists and is reflected back in the response."""
        _enable_flag(monkeypatch)
        backend = make_mock_backend(org_id=1, management_scope="full_lifecycle")
        updated = make_mock_backend(org_id=1, management_scope="register_and_route")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([backend]),
            make_select_result([updated]),
        ]
        resp = await client.put(
            f"{ENDPOINT_PATH}/1",
            headers=auth_headers,
            json={"management_scope": "register_and_route"},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["management_scope"] == "register_and_route"

    async def test_update_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Updating another org's backend returns 403."""
        _enable_flag(monkeypatch)
        foreign = make_mock_backend(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.put(
            f"{ENDPOINT_PATH}/1", headers=auth_headers, json={"name": "hijacked"}
        )
        assert resp.status_code == 403

    async def test_update_rejects_invalid_management_scope(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """An unrecognized management_scope value on update is rejected with 400."""
        _enable_flag(monkeypatch)
        backend = make_mock_backend(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([backend])
        resp = await client.put(
            f"{ENDPOINT_PATH}/1", headers=auth_headers, json={"management_scope": "bogus"}
        )
        assert resp.status_code == 400

    async def test_delete_own_org_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Deleting the caller's own backend succeeds."""
        _enable_flag(monkeypatch)
        backend = make_mock_backend(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([backend])
        resp = await client.delete(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_foreign_org_returns_403(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """Deleting another org's backend returns 403."""
        _enable_flag(monkeypatch)
        foreign = make_mock_backend(org_id=99)
        app_mock_db.return_value.select.return_value = make_select_result([foreign])
        resp = await client.delete(f"{ENDPOINT_PATH}/1", headers=auth_headers)
        assert resp.status_code == 403


class TestFleetBackendHealth:
    """GET /api/v1/fleet/backends/<id>/health -- via the InferenceFleetBackend interface."""

    async def test_health_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A non-existent backend id's health check returns 404."""
        _enable_flag(monkeypatch)
        app_mock_db.return_value.select.return_value = make_select_result([])
        resp = await client.get(f"{ENDPOINT_PATH}/999/health", headers=auth_headers)
        assert resp.status_code == 404

    async def test_health_reports_backend_status(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A healthy backend's health() result is surfaced through the response."""
        _enable_flag(monkeypatch)
        backend_row = make_mock_backend(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([backend_row])

        fake_backend = MagicMock()
        fake_backend.health = AsyncMock(
            return_value=FleetHealth(backend_id=1, healthy=True, node_count=3, detail={"ok": True})
        )
        with patch(
            "services.management.app.api.v1.fleet.build_backend", return_value=fake_backend
        ):
            resp = await client.get(f"{ENDPOINT_PATH}/1/health", headers=auth_headers)

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["healthy"] is True
        assert body["data"]["node_count"] == 3

    async def test_health_construction_failure_reports_unhealthy_not_500(
        self, client, app_mock_db: MagicMock, auth_headers: dict, monkeypatch
    ) -> None:
        """A build_backend failure (e.g. bad credentials) reports unhealthy, not a 500."""
        _enable_flag(monkeypatch)
        backend_row = make_mock_backend(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([backend_row])

        with patch(
            "services.management.app.api.v1.fleet.build_backend",
            side_effect=RuntimeError("bad credentials"),
        ):
            resp = await client.get(f"{ENDPOINT_PATH}/1/health", headers=auth_headers)

        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["data"]["healthy"] is False
