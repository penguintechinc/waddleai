"""Unit tests for /api/v1/cache-configs CRUD (spec §6.4)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from tests.unit.management.conftest import make_select_result


def make_mock_cache_config(
    config_id: int = 1,
    scope_type: str = "global",
    scope_ref=None,
    exact_enabled: bool = True,
    semantic_enabled: bool = False,
    semantic_threshold: float = 0.95,
    ttl_seconds: int = 86400,
    max_entry_kb: int = 256,
    anthropic_cache_control: bool = True,
) -> MagicMock:
    """Make mock cache config."""
    row = MagicMock()
    row.id = config_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.exact_enabled = exact_enabled
    row.semantic_enabled = semantic_enabled
    row.semantic_threshold = semantic_threshold
    row.ttl_seconds = ttl_seconds
    row.max_entry_kb = max_entry_kb
    row.anthropic_cache_control = anthropic_cache_control
    row.created_at = datetime(2026, 1, 1, 0, 0, 0)
    row.updated_at = datetime(2026, 1, 1, 0, 0, 0)
    return row


class TestListAndGet:
    """Tests for list and get."""

    async def test_list_returns_rows(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """List returns rows."""
        rows = [make_mock_cache_config(1, "global"), make_mock_cache_config(2, "org", "1")]
        app_mock_db.return_value.select.side_effect = [make_select_result(rows)]

        resp = await client.get("/api/v1/cache-configs", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["data"]) == 2

    async def test_get_missing_returns_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Get missing returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]
        resp = await client.get("/api/v1/cache-configs/999", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateValidation:
    """Tests for create validation."""

    async def test_missing_body_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Missing body 400."""
        resp = await client.post("/api/v1/cache-configs", headers=auth_headers, json=None)
        assert resp.status_code == 400

    async def test_invalid_scope_type_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Invalid scope type 400."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=auth_headers, json={"scope_type": "bogus"}
        )
        assert resp.status_code == 400

    async def test_threshold_out_of_range_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Threshold out of range 400."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "semantic_threshold": 0.2},
        )
        assert resp.status_code == 400

    async def test_ttl_not_positive_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Ttl not positive 400."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1", "ttl_seconds": 0},
        )
        assert resp.status_code == 400

    async def test_org_scope_requires_scope_ref_400(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Org scope requires scope ref 400."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=auth_headers, json={"scope_type": "org"}
        )
        assert resp.status_code == 400


class TestCreateHappyPath:
    """Tests for create happy path."""

    async def test_admin_creates_global_config(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin creates global config."""
        created_row = make_mock_cache_config(5, "global")
        # 1st select().first(): uniqueness check -> None. 2nd: post-insert fetch.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([]),
            make_select_result([created_row]),
        ]
        app_mock_db.cache_configs.insert.return_value = 5

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=auth_headers,
                json={"scope_type": "global", "semantic_threshold": 0.97},
            )

        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["data"]["scope_type"] == "global"

    async def test_create_conflict_returns_409(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Create conflict returns 409."""
        existing_row = make_mock_cache_config(1, "org", "1")
        app_mock_db.return_value.select.side_effect = [make_select_result([existing_row])]

        resp = await client.post(
            "/api/v1/cache-configs",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "1"},
        )
        assert resp.status_code == 409

    async def test_resource_manager_cannot_write_another_orgs_row(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        # rm_auth_headers user is org_id=1 (see conftest.make_token default org_id=1)
        """Resource manager cannot write another orgs row."""
        resp = await client.post(
            "/api/v1/cache-configs",
            headers=rm_auth_headers,
            json={"scope_type": "org", "scope_ref": "999"},
        )
        assert resp.status_code == 403

    async def test_resource_manager_cannot_write_global(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager cannot write global."""
        resp = await client.post(
            "/api/v1/cache-configs", headers=rm_auth_headers, json={"scope_type": "global"}
        )
        assert resp.status_code == 403

    async def test_resource_manager_can_write_own_org_row(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Resource manager can write own org row."""
        created_row = make_mock_cache_config(7, "org", "1")
        app_mock_db.return_value.select.side_effect = [
            make_select_result([]),
            make_select_result([created_row]),
        ]
        app_mock_db.cache_configs.insert.return_value = 7

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.post(
                "/api/v1/cache-configs",
                headers=rm_auth_headers,
                json={"scope_type": "org", "scope_ref": "1"},
            )
        assert resp.status_code == 201


class TestUpdateAndDelete:
    """Tests for update and delete."""

    async def test_update_missing_config_404(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Update missing config 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]
        resp = await client.put(
            "/api/v1/cache-configs/999", headers=auth_headers, json={"ttl_seconds": 100}
        )
        assert resp.status_code == 404

    async def test_update_invalidates_scope_and_returns_row(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Update invalidates scope and returns row."""
        existing_row = make_mock_cache_config(1, "global", ttl_seconds=86400)
        updated_row = make_mock_cache_config(1, "global", ttl_seconds=100)
        app_mock_db.return_value.select.side_effect = [
            make_select_result([existing_row]),
            make_select_result([updated_row]),
        ]
        fake_redis = MagicMock()

        with patch("services.management.app.api.v1.cache_configs.redis_client", fake_redis):
            resp = await client.put(
                "/api/v1/cache-configs/1", headers=auth_headers, json={"ttl_seconds": 100}
            )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["ttl_seconds"] == 100
        fake_redis.delete.assert_called_once()

    async def test_delete_returns_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Delete returns success."""
        existing_row = make_mock_cache_config(1, "global")
        app_mock_db.return_value.select.side_effect = [make_select_result([existing_row])]

        with patch("services.management.app.api.v1.cache_configs.redis_client", MagicMock()):
            resp = await client.delete("/api/v1/cache-configs/1", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["deleted"] is True
