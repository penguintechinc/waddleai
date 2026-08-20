"""Unit tests for virtual key management routes: /api/v1/keys/*."""

from datetime import datetime
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_key

# ---------------------------------------------------------------------------
# GET /api/v1/keys
# ---------------------------------------------------------------------------


class TestListKeys:
    """Tests for GET /api/v1/keys."""

    async def test_list_keys_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin gets all keys."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = await client.get("/api/v1/keys", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "keys" in data

    async def test_list_keys_resource_manager(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager sees own org keys."""
        key = make_mock_key(org_id=1)
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = await client.get("/api/v1/keys", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_list_keys_regular_user(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Regular user sees own keys only."""
        key = make_mock_key(user_id=2)
        app_mock_db.return_value.select.return_value = make_select_result([key])

        resp = await client.get("/api/v1/keys", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_list_keys_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/keys")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/keys/<id>
# ---------------------------------------------------------------------------


class TestGetKey:
    """Tests for GET /api/v1/keys/<key_id>."""

    async def test_get_key_admin(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """Admin can retrieve any key."""
        key = make_mock_key()
        # First call returns key; subsequent calls return empty
        key_sel = make_select_result([key])
        empty_sel = make_select_result([])
        app_mock_db.return_value.select.side_effect = [key_sel, empty_sel, empty_sel]

        resp = await client.get("/api/v1/keys/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["name"] == "Test Key"

    async def test_get_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/keys/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_key_user_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Regular user can view own key."""
        key = make_mock_key(key_id=5, user_id=2, org_id=1)
        key_sel = make_select_result([key])
        empty_sel = make_select_result([])
        app_mock_db.return_value.select.side_effect = [key_sel, empty_sel, empty_sel]

        resp = await client.get("/api/v1/keys/5", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_get_key_user_other_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Regular user cannot view another user's key → 403."""
        key = make_mock_key(key_id=10, user_id=99, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.get("/api/v1/keys/10", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_get_key_rm_other_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager cannot view key from another org → 403."""
        key = make_mock_key(key_id=10, user_id=1, org_id=99)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.get("/api/v1/keys/10", headers=rm_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/keys
# ---------------------------------------------------------------------------


class TestCreateKey:
    """Tests for POST /api/v1/keys."""

    async def test_create_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can create a key."""
        app_mock_db.virtual_keys.insert.return_value = 20

        resp = await client.post(
            "/api/v1/keys",
            headers=auth_headers,
            json={"name": "My New Key"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert "api_key" in data
        assert data["api_key"].startswith("wa-")

    async def test_create_key_missing_name(self, client, auth_headers: dict) -> None:
        """Missing name returns 400."""
        resp = await client.post(
            "/api/v1/keys",
            headers=auth_headers,
            json={"description": "no name"},
        )
        assert resp.status_code == 400

    async def test_create_key_no_body(self, client, auth_headers: dict) -> None:
        """No body returns 400."""
        resp = await client.post(
            "/api/v1/keys",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_create_key_no_expires(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Key with expires_days=0 creates no expiry."""
        app_mock_db.virtual_keys.insert.return_value = 21

        resp = await client.post(
            "/api/v1/keys",
            headers=auth_headers,
            json={"name": "No Expiry Key", "expires_days": 0},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["expires_at"] is None

    async def test_create_key_for_other_user_non_admin_forbidden(
        self, client, user_auth_headers: dict
    ) -> None:
        """Regular user cannot create key for another user → 403."""
        resp = await client.post(
            "/api/v1/keys",
            headers=user_auth_headers,
            json={"name": "SomeKey", "user_id": 999, "organization_id": 1},
        )
        assert resp.status_code == 403

    async def test_create_key_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.post("/api/v1/keys", json={"name": "Key"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# PUT /api/v1/keys/<id>
# ---------------------------------------------------------------------------


class TestUpdateKey:
    """Tests for PUT /api/v1/keys/<key_id>."""

    async def test_update_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can update a key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/1",
            headers=auth_headers,
            json={"name": "Updated Key Name"},
        )
        assert resp.status_code == 200

    async def test_update_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/keys/999",
            headers=auth_headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_update_key_no_body(self, client, auth_headers: dict) -> None:
        """No body returns 400."""
        resp = await client.put(
            "/api/v1/keys/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_update_key_user_access_denied(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Regular user cannot update another user's key → 403."""
        key = make_mock_key(key_id=10, user_id=99)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"name": "Hijack"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/v1/keys/<id>
# ---------------------------------------------------------------------------


class TestDeleteKey:
    """Tests for DELETE /api/v1/keys/<key_id>."""

    async def test_delete_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can revoke a key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/keys/1", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/keys/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_key_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.delete("/api/v1/keys/1")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/keys/<id>/rotate
# ---------------------------------------------------------------------------


class TestRotateKey:
    """Tests for POST /api/v1/keys/<key_id>/rotate."""

    async def test_rotate_key_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can rotate a key, receiving a new api_key."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.post("/api/v1/keys/1/rotate", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "api_key" in data
        assert data["api_key"].startswith("wa-")

    async def test_rotate_key_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post("/api/v1/keys/999/rotate", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/keys/<id>/sync -- removed (AILB retired, migration 007)
# ---------------------------------------------------------------------------


class TestSyncKeyRemoved:
    """The AILB sync endpoint had no successor and is gone; guard against reintroduction."""

    async def test_sync_key_endpoint_no_longer_exists(self, client, auth_headers: dict) -> None:
        """The route is unregistered -- Quart returns 404, not 200/403."""
        resp = await client.post("/api/v1/keys/1/sync", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/keys/<id>/usage
# ---------------------------------------------------------------------------


class TestGetKeyUsage:
    """Tests for GET /api/v1/keys/<key_id>/usage."""

    async def test_get_key_usage_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can get key usage stats."""
        key = make_mock_key()
        usage_record = MagicMock()
        usage_record.date = datetime(2025, 1, 1).date()
        usage_record.waddleai_tokens = 500
        usage_record.tokens_input_total = 200
        usage_record.tokens_output_total = 300
        usage_record.request_count = 10
        usage_record.cost_usd_total = 0.05

        key_sel = make_select_result([key])
        usage_sel = make_select_result([usage_record])
        app_mock_db.return_value.select.side_effect = [key_sel, usage_sel]

        resp = await client.get("/api/v1/keys/1/usage", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "totals" in data

    async def test_get_key_usage_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/keys/999/usage", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_key_usage_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/keys/1/usage")
        assert resp.status_code == 401
