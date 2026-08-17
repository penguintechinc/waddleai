"""
Unit tests for quota management routes: /api/v1/quotas/*
"""

from typing import Dict
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_mock_key, make_mock_org, make_mock_user, make_select_result

# ---------------------------------------------------------------------------
# GET /api/v1/quotas
# ---------------------------------------------------------------------------


class TestListQuotas:
    """Tests for GET /api/v1/quotas"""

    async def test_list_quotas_admin_all_entities(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin gets all orgs, users, and keys."""
        org = make_mock_org()
        user = make_mock_user()
        key = make_mock_key()

        # Three separate select() calls for orgs, users, keys
        orgs_result = make_select_result([org])
        users_result = make_select_result([user])
        keys_result = make_select_result([key])

        app_mock_db.return_value.select.side_effect = [orgs_result, users_result, keys_result]

        resp = await client.get("/api/v1/quotas", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "quotas" in data
        assert "total" in data
        assert len(data["quotas"]) == 3  # 1 org + 1 user + 1 key

        # Verify quota types
        types = [q["type"] for q in data["quotas"]]
        assert "organization" in types
        assert "user" in types
        assert "key" in types

    async def test_list_quotas_resource_manager_org_scoped(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager gets only their org's entities."""
        org = make_mock_org(org_id=1)
        user = make_mock_user(org_id=1)
        key = make_mock_key(org_id=1)

        orgs_result = make_select_result([org])
        users_result = make_select_result([user])
        keys_result = make_select_result([key])

        app_mock_db.return_value.select.side_effect = [orgs_result, users_result, keys_result]

        resp = await client.get("/api/v1/quotas", headers=rm_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["total"] == 3

    async def test_list_quotas_empty(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Empty quota list returns 200 with empty list."""
        empty = make_select_result([])
        app_mock_db.return_value.select.side_effect = [empty, empty, empty]

        resp = await client.get("/api/v1/quotas", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["quotas"] == []
        assert data["total"] == 0

    async def test_list_quotas_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/quotas")
        assert resp.status_code == 401

    async def test_list_quotas_invalid_role(self, client, user_auth_headers: Dict) -> None:
        """Regular user (non-admin, non-resource_manager) returns 403."""
        resp = await client.get("/api/v1/quotas", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/quotas/user/<user_id>
# ---------------------------------------------------------------------------


class TestSetUserQuota:
    """Tests for PUT /api/v1/quotas/user/<user_id>"""

    async def test_set_user_quota_admin_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can set user quota."""
        user = make_mock_user(user_id=5, username="testuser")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            json={"token_quota_daily": 50000, "token_quota_monthly": 500000},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["user_id"] == 5
        assert data["username"] == "testuser"
        assert "message" in data

    async def test_set_user_quota_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can set user quota in their org."""
        user = make_mock_user(user_id=5, org_id=1, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=rm_auth_headers,
            json={"token_quota_daily": 25000},
        )
        assert resp.status_code == 200

    async def test_set_user_quota_resource_manager_other_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot set user quota for user in different org."""
        user = make_mock_user(user_id=5, org_id=2)  # Different org
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=rm_auth_headers,
            json={"token_quota_daily": 25000},
        )
        assert resp.status_code == 403

    async def test_set_user_quota_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/user/999",
            headers=auth_headers,
            json={"token_quota_daily": 50000},
        )
        assert resp.status_code == 404

    async def test_set_user_quota_no_body(self, client, auth_headers: Dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_user_quota_daily_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only daily quota."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/1",
            headers=auth_headers,
            json={"token_quota_daily": 75000},
        )
        assert resp.status_code == 200

    async def test_set_user_quota_monthly_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only monthly quota."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/1",
            headers=auth_headers,
            json={"token_quota_monthly": 1500000},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/quotas/org/<org_id>
# ---------------------------------------------------------------------------


class TestSetOrganizationQuota:
    """Tests for PUT /api/v1/quotas/org/<org_id>"""

    async def test_set_org_quota_admin_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can set organization quota."""
        org = make_mock_org(org_id=2, name="TestOrg")
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/2",
            headers=auth_headers,
            json={"token_quota_daily": 500000, "token_quota_monthly": 5000000},
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["organization_id"] == 2
        assert data["organization_name"] == "TestOrg"
        assert "message" in data

    async def test_set_org_quota_resource_manager_forbidden(self, client, rm_auth_headers: Dict) -> None:
        """Resource manager cannot set org quota (admin only)."""
        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=rm_auth_headers,
            json={"token_quota_daily": 500000},
        )
        assert resp.status_code == 403

    async def test_set_org_quota_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/org/999",
            headers=auth_headers,
            json={"token_quota_daily": 500000},
        )
        assert resp.status_code == 404

    async def test_set_org_quota_no_body(self, client, auth_headers: Dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_org_quota_daily_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only daily quota."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=auth_headers,
            json={"token_quota_daily": 750000},
        )
        assert resp.status_code == 200

    async def test_set_org_quota_monthly_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only monthly quota."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=auth_headers,
            json={"token_quota_monthly": 7500000},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/quotas/key/<key_id>
# ---------------------------------------------------------------------------


class TestSetKeyQuota:
    """Tests for PUT /api/v1/quotas/key/<key_id>"""

    async def test_set_key_quota_admin_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can set key quota."""
        key = make_mock_key(key_id=10, name="AdminKey")
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={
                "budget_limit_daily": 1000,
                "budget_limit_monthly": 10000,
                "tpm_limit": 20000,
                "rpm_limit": 120,
            },
        )
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["key_id"] == 10
        assert data["key_name"] == "AdminKey"
        assert "updated successfully" in data["message"]

    async def test_set_key_quota_resource_manager_own_key(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can set key quota in their org."""
        key = make_mock_key(key_id=10, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=rm_auth_headers,
            json={"tpm_limit": 15000},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_resource_manager_other_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot set key quota in different org."""
        key = make_mock_key(key_id=10, org_id=2)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=rm_auth_headers,
            json={"tpm_limit": 15000},
        )
        assert resp.status_code == 403

    async def test_set_key_quota_regular_user_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can set quota for own key."""
        # user_auth_headers has user_id=2 (from conftest)
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=user_auth_headers,
            json={"rpm_limit": 90},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_regular_user_other_key_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot set quota for another user's key."""
        key = make_mock_key(key_id=10, user_id=3, org_id=1)  # Different user
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=user_auth_headers,
            json={"rpm_limit": 90},
        )
        assert resp.status_code == 403

    async def test_set_key_quota_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/key/999",
            headers=auth_headers,
            json={"tpm_limit": 10000},
        )
        assert resp.status_code == 404

    async def test_set_key_quota_no_body(self, client, auth_headers: Dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_key_quota_budget_daily_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only budget_limit_daily."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            json={"budget_limit_daily": 500},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_budget_monthly_only(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update only budget_limit_monthly."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            json={"budget_limit_monthly": 5000},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_tpm_rpm_limits(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Can update TPM and RPM limits."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            json={"tpm_limit": 25000, "rpm_limit": 150},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/quotas/status/<entity_id>?type=key|user|org
# ---------------------------------------------------------------------------


class TestGetQuotaStatus:
    """Tests for GET /api/v1/quotas/status/<entity_id>?type=..."""

    def _make_usage(self, tokens: int = 0, cost: float = 0.0) -> MagicMock:
        """Create a usage record mock with numeric attributes for JSON safety."""
        u = MagicMock()
        u.waddleai_tokens = tokens
        u.cost_usd_total = cost
        return u

    # --- Key Status Tests ---

    async def test_get_key_quota_status_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get quota status for any key."""
        key = make_mock_key(key_id=10)
        daily = self._make_usage(tokens=5000, cost=0.10)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),  # key lookup → .first()
            make_select_result([daily]),  # daily_usage → .first()
            make_select_result([]),  # monthly_usage → iterable
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["type"] == "key"
        assert data["id"] == 10
        assert "quotas" in data
        assert "usage" in data

    async def test_get_key_quota_status_regular_user_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can get status for own key (user_id=2)."""
        key = make_mock_key(key_id=10, user_id=2)
        daily = self._make_usage(tokens=1000)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([daily]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=user_auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["id"] == 10

    async def test_get_key_quota_status_regular_user_other_key_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot get status for another user's key."""
        key = make_mock_key(key_id=10, user_id=3)
        # get_quota_status() unconditionally fetches key + daily + monthly usage
        # (via asyncio.to_thread) before the permission check runs -- an
        # under-provisioned side_effect list here would raise StopIteration
        # inside the thread, which asyncio.to_thread cannot propagate as a
        # normal exception (StopIteration cannot be set on a Future), hanging
        # the test forever instead of failing cleanly.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_get_key_quota_status_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can get status for keys in their org."""
        key = make_mock_key(key_id=10, org_id=1)
        daily = self._make_usage(tokens=2000)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([daily]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_get_key_quota_status_resource_manager_other_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot get status for keys in different org."""
        key = make_mock_key(key_id=10, org_id=2)
        # See test_get_key_quota_status_regular_user_other_key_forbidden for
        # why all 3 select() results must be provided even on the 403 path.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_get_key_quota_status_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=key", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_key_quota_status_includes_rate_limits(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """Key status includes rate limit info."""
        key = make_mock_key(key_id=10, user_id=1)
        key.tpm_limit = 20000
        key.rpm_limit = 120
        daily = self._make_usage(tokens=1500)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([daily]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10?type=key", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["quotas"]["rate_limits"]["tpm_limit"] == 20000
        assert data["quotas"]["rate_limits"]["rpm_limit"] == 120

    # --- User Status Tests ---

    async def test_get_user_quota_status_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get quota status for any user."""
        user = make_mock_user(user_id=5)
        user.token_quota_daily = 50000
        user.token_quota_monthly = 500000

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),  # user lookup → .first()
            make_select_result([]),  # daily usage → iterable
            make_select_result([]),  # monthly usage → iterable
        ]

        resp = await client.get("/api/v1/quotas/status/5?type=user", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["type"] == "user"
        assert data["id"] == 5
        assert "quotas" in data

    async def test_get_user_quota_status_regular_user_self(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can get status for themselves (user_id=2)."""
        user = make_mock_user(user_id=2)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/2?type=user", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_get_user_quota_status_regular_user_other_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot get status for another user."""
        user = make_mock_user(user_id=5)
        # See test_get_key_quota_status_regular_user_other_key_forbidden for
        # why all 3 select() results must be provided even on the 403 path.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/5?type=user", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_get_user_quota_status_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can get status for users in their org."""
        user = make_mock_user(user_id=5, org_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/5?type=user", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_get_user_quota_status_resource_manager_other_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot get status for users in different org."""
        user = make_mock_user(user_id=5, org_id=2)
        # See test_get_key_quota_status_regular_user_other_key_forbidden for
        # why all 3 select() results must be provided even on the 403 path.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/5?type=user", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_get_user_quota_status_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent user returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=user", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_user_quota_status_includes_usage(
        self, client, app_mock_db: MagicMock, auth_headers: Dict
    ) -> None:
        """User status includes daily and monthly usage."""
        user = make_mock_user(user_id=5)
        user.token_quota_daily = 50000
        user.token_quota_monthly = 500000
        daily = self._make_usage(tokens=15000)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([user]),
            make_select_result([daily]),
            make_select_result([daily]),
        ]

        resp = await client.get("/api/v1/quotas/status/5?type=user", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "daily" in data["quotas"]
        assert "monthly" in data["quotas"]

    # --- Organization Status Tests ---

    async def test_get_org_quota_status_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get quota status for any org."""
        org = make_mock_org(org_id=2)
        org.token_quota_daily = 500000
        org.token_quota_monthly = 5000000

        app_mock_db.return_value.select.side_effect = [
            make_select_result([org]),  # org lookup → .first()
            make_select_result([]),  # daily usage → iterable
            make_select_result([]),  # monthly usage → iterable
        ]

        resp = await client.get("/api/v1/quotas/status/2?type=org", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["type"] == "organization"
        assert data["id"] == 2

    async def test_get_org_quota_status_regular_user_own_org(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can get status for own org (org_id=1)."""
        org = make_mock_org(org_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([org]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/1?type=org", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_get_org_quota_status_regular_user_other_org_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot get status for another org."""
        org = make_mock_org(org_id=2)
        # See test_get_key_quota_status_regular_user_other_key_forbidden for
        # why all 3 select() results must be provided even on the 403 path.
        app_mock_db.return_value.select.side_effect = [
            make_select_result([org]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/2?type=org", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_get_org_quota_status_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can get status for own org."""
        org = make_mock_org(org_id=1)

        app_mock_db.return_value.select.side_effect = [
            make_select_result([org]),
            make_select_result([]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/1?type=org", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_get_org_quota_status_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Non-existent org returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=org", headers=auth_headers)
        assert resp.status_code == 404

    # --- Invalid Type and No Auth Tests ---

    async def test_get_quota_status_invalid_type(self, client, auth_headers: Dict) -> None:
        """Invalid entity type returns 400."""
        resp = await client.get("/api/v1/quotas/status/1?type=invalid", headers=auth_headers)
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "error" in data

    async def test_get_quota_status_default_type_key(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Default entity type is 'key' if not specified."""
        key = make_mock_key(key_id=10)
        daily = self._make_usage()

        app_mock_db.return_value.select.side_effect = [
            make_select_result([key]),
            make_select_result([daily]),
            make_select_result([]),
        ]

        resp = await client.get("/api/v1/quotas/status/10", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["type"] == "key"

    async def test_get_quota_status_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/quotas/status/1?type=key")
        assert resp.status_code == 401
