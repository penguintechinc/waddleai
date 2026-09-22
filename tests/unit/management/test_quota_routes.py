"""Unit tests for quota management routes: /api/v1/quotas/*."""

from unittest.mock import MagicMock

from tests.unit.management.conftest import (
    make_mock_key,
    make_mock_org,
    make_mock_user,
    make_select_result,
)

# ---------------------------------------------------------------------------
# GET /api/v1/quotas
# ---------------------------------------------------------------------------


class TestListQuotas:
    """Tests for GET /api/v1/quotas."""

    async def test_list_quotas_admin_all_entities(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_list_quotas_empty(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_list_quotas_invalid_role(self, client, user_auth_headers: dict) -> None:
        """Regular user (non-admin, non-resource_manager) returns 403."""
        resp = await client.get("/api/v1/quotas", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/quotas/user/<user_id>
# ---------------------------------------------------------------------------


class TestSetUserQuota:
    """Tests for PUT /api/v1/quotas/user/<user_id>."""

    async def test_set_user_quota_admin_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_set_user_quota_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/user/999",
            headers=auth_headers,
            json={"token_quota_daily": 50000},
        )
        assert resp.status_code == 404

    async def test_set_user_quota_no_body(self, client, auth_headers: dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_user_quota_daily_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Can update only daily quota."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/1",
            headers=auth_headers,
            json={"token_quota_daily": 75000},
        )
        assert resp.status_code == 200

    async def test_set_user_quota_monthly_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
    """Tests for PUT /api/v1/quotas/org/<org_id>."""

    async def test_set_org_quota_admin_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_set_org_quota_resource_manager_forbidden(
        self, client, rm_auth_headers: dict
    ) -> None:
        """Resource manager cannot set org quota (admin only)."""
        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=rm_auth_headers,
            json={"token_quota_daily": 500000},
        )
        assert resp.status_code == 403

    async def test_set_org_quota_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/org/999",
            headers=auth_headers,
            json={"token_quota_daily": 500000},
        )
        assert resp.status_code == 404

    async def test_set_org_quota_no_body(self, client, auth_headers: dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_org_quota_daily_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Can update only daily quota."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/1",
            headers=auth_headers,
            json={"token_quota_daily": 750000},
        )
        assert resp.status_code == 200

    async def test_set_org_quota_monthly_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
    """Tests for PUT /api/v1/quotas/key/<key_id>."""

    async def test_set_key_quota_admin_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_set_key_quota_regular_user_own_key_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Regular user cannot raise the rate limit on their OWN key.

        regression: audit-2026-09-14 -- this test previously asserted 200,
        encoding the privilege-escalation finding as intended behaviour.
        Every column this route writes is privileged; owning the key is not
        enough. See keys.PRIVILEGED_KEY_FIELDS.
        """
        # user_auth_headers has user_id=2 (from conftest)
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=user_auth_headers,
            json={"rpm_limit": 90},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_set_key_quota_regular_user_other_key_forbidden(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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

    async def test_set_key_quota_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/quotas/key/999",
            headers=auth_headers,
            json={"tpm_limit": 10000},
        )
        assert resp.status_code == 404

    async def test_set_key_quota_no_body(self, client, auth_headers: dict) -> None:
        """Missing request body returns 400."""
        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_set_key_quota_budget_daily_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Can update only budget_limit_daily."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            json={"budget_limit_daily": 500},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_budget_monthly_only(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Can update only budget_limit_monthly."""
        key = make_mock_key()
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/1",
            headers=auth_headers,
            json={"budget_limit_monthly": 5000},
        )
        assert resp.status_code == 200

    async def test_set_key_quota_tpm_rpm_limits(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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

    async def test_get_key_quota_status_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_get_key_quota_status_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent key returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=key", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_key_quota_status_includes_rate_limits(
        self, client, app_mock_db: MagicMock, auth_headers: dict
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

    async def test_get_user_quota_status_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_get_user_quota_status_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent user returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=user", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_user_quota_status_includes_usage(
        self, client, app_mock_db: MagicMock, auth_headers: dict
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

    async def test_get_org_quota_status_admin(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
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
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
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

    async def test_get_org_quota_status_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Non-existent org returns 404."""
        app_mock_db.return_value.select.side_effect = [make_select_result([])]

        resp = await client.get("/api/v1/quotas/status/999?type=org", headers=auth_headers)
        assert resp.status_code == 404

    # --- Invalid Type and No Auth Tests ---

    async def test_get_quota_status_invalid_type(self, client, auth_headers: dict) -> None:
        """Invalid entity type returns 400."""
        resp = await client.get("/api/v1/quotas/status/1?type=invalid", headers=auth_headers)
        assert resp.status_code == 400
        data = await resp.get_json()
        assert "error" in data

    async def test_get_quota_status_default_type_key(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
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


# ---------------------------------------------------------------------------
# PUT /api/v1/quotas/key/<key_id> -- privilege split + request validation
#
# regression: audit-2026-09-14
#
# HIGH: the route carried only @require_auth and an ownership-only in-handler
# check, so a Role.USER could raise budget_limit_daily/monthly and
# tpm_limit/rpm_limit on their own key -- self-service privilege escalation.
# Role.USER does not hold Permission.QUOTA_UPDATE (shared/auth/rbac.py); the
# sibling PUT /quotas/user/<id> already required it, this route never checked.
#
# MEDIUM (same audit): the body was read with request.get_json() and written
# straight to the DB with no type or bounds checking.
# ---------------------------------------------------------------------------


class TestSetKeyQuotaPrivilegeSplit:
    """Privilege gating for PUT /api/v1/quotas/key/<key_id>."""

    async def test_user_cannot_set_budget_limit_daily_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user raising their own daily budget is refused. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=user_auth_headers,
            json={"budget_limit_daily": 999999.0},
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body["required_scope"] == "quota:update"
        assert body["denied_fields"] == ["budget_limit_daily"]
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_denied_fields_lists_every_privileged_field(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """The 403 names all refused fields -- none are silently dropped.

        regression: audit-2026-09-14 -- dropping the fields and returning
        200 would be a silent no-op, the second failure mode the fix had to
        avoid.
        """
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=user_auth_headers,
            json={
                "budget_limit_daily": 1.0,
                "budget_limit_monthly": 2.0,
                "tpm_limit": 3,
                "rpm_limit": 4,
            },
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body["denied_fields"] == [
            "budget_limit_daily",
            "budget_limit_monthly",
            "rpm_limit",
            "tpm_limit",
        ]
        app_mock_db.return_value.update.assert_not_called()

    async def test_admin_can_set_budget(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin holds quota:update, so the budget write succeeds. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, name="AdminKey")
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"budget_limit_daily": 500.0},
        )
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once_with(budget_limit_daily=500.0)

    async def test_resource_manager_can_set_budget_in_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager holds quota:update for its own org -- not over-restricted.

        regression: audit-2026-09-14.
        """
        key = make_mock_key(key_id=10, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=rm_auth_headers,
            json={"budget_limit_monthly": 4200.0},
        )
        assert resp.status_code == 200

    async def test_resource_manager_cannot_set_budget_out_of_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """Org scoping still refuses a resource_manager on another org's key.

        regression: audit-2026-09-14.
        """
        key = make_mock_key(key_id=10, org_id=2)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=rm_auth_headers,
            json={"budget_limit_daily": 100.0},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()


class TestSetKeyQuotaRequestValidation:
    """Type and bounds validation for PUT /api/v1/quotas/key/<key_id>.

    regression: audit-2026-09-14 -- values previously went from raw JSON
    into the DB unchecked.
    """

    async def test_non_numeric_budget_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A string budget is refused by the schema, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"budget_limit_daily": "not-a-number"},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_non_integer_tpm_limit_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A list where an int is expected is refused, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"tpm_limit": ["nope"]},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_negative_budget_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A negative budget is out of bounds, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"budget_limit_daily": -5.0},
        )
        assert resp.status_code == 400
        body = await resp.get_json()
        assert "budget_limit_daily" in body["error"]
        app_mock_db.return_value.update.assert_not_called()

    async def test_negative_rpm_limit_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A negative rate limit is out of bounds, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"rpm_limit": -1},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_absurd_tpm_limit_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A rate limit above the sanity ceiling is refused, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"tpm_limit": 99_999_999_999},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_absurd_budget_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A budget above the sanity ceiling is refused, not persisted."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"budget_limit_monthly": 1e12},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_valid_bounds_accepted(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A well-formed in-range body still writes -- validation is not over-tight."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/quotas/key/10",
            headers=auth_headers,
            json={"budget_limit_daily": 0.0, "tpm_limit": 0},
        )
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once_with(budget_limit_daily=0.0, tpm_limit=0)


# ---------------------------------------------------------------------------
# GET /api/v1/quotas -- bounded list window
# PUT /api/v1/quotas/{user,org} -- typed + bounded request bodies
# regression: audit-2026-09-14-wave2
# ---------------------------------------------------------------------------


class TestListQuotasPagination:
    """The three unbounded entity selects are now bounded and echo their window."""

    async def test_list_quotas_response_includes_pagination(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A `pagination` block reflecting ?page=&limit= is returned.

        regression: audit-2026-09-14-wave2 -- absent before the bounded-select
        change, so the assertion fails pre-change.
        """
        empty = make_select_result([])
        app_mock_db.return_value.select.side_effect = [empty, empty, empty]

        resp = await client.get("/api/v1/quotas?page=3&limit=10", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["pagination"]["page"] == 3
        assert body["pagination"]["limit"] == 10


class TestSetUserQuotaRequestValidation:
    """PUT /api/v1/quotas/user/<id> now type-checks and range-checks its body."""

    async def test_non_numeric_quota_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A non-numeric token quota is refused with 400, never persisted.

        regression: audit-2026-09-14-wave2 -- pre-change the raw JSON value went
        straight to db.update() and returned 200, so this fails before
        @validate_request was added.
        """
        user = make_mock_user(user_id=5, role="user", org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            json={"token_quota_daily": "not-a-number"},
        )
        assert resp.status_code == 400

    async def test_negative_quota_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A negative token quota is refused with 400. regression: audit-2026-09-14-wave2."""
        user = make_mock_user(user_id=5, role="user", org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            json={"token_quota_daily": -5},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_absurd_quota_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An out-of-range token quota is refused with 400. regression: audit-2026-09-14-wave2."""
        user = make_mock_user(user_id=5, role="user", org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            json={"token_quota_monthly": 10**15},
        )
        assert resp.status_code == 400

    async def test_valid_quota_response_has_exact_fields(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A valid update returns exactly {user_id, username, message}.

        regression: audit-2026-09-14-wave2 -- @validate_response pins the shape.
        """
        user = make_mock_user(user_id=5, username="quotauser", role="user", org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/quotas/user/5",
            headers=auth_headers,
            json={"token_quota_daily": 50000},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert set(body.keys()) == {"user_id", "username", "message"}


class TestSetOrgQuotaRequestValidation:
    """PUT /api/v1/quotas/org/<id> now type-checks and range-checks its body."""

    async def test_non_numeric_quota_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A non-numeric org quota is refused with 400.

        regression: audit-2026-09-14-wave2 -- pre-change it reached db.update()
        and returned 200.
        """
        org = make_mock_org(org_id=2)
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/2",
            headers=auth_headers,
            json={"token_quota_daily": "lots"},
        )
        assert resp.status_code == 400

    async def test_absurd_quota_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An out-of-range org quota is refused with 400. regression: audit-2026-09-14-wave2."""
        org = make_mock_org(org_id=2)
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/2",
            headers=auth_headers,
            json={"token_quota_monthly": -1},
        )
        assert resp.status_code == 400
        app_mock_db.return_value.update.assert_not_called()

    async def test_valid_quota_response_has_exact_fields(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A valid update returns exactly {organization_id, organization_name, message}.

        regression: audit-2026-09-14-wave2.
        """
        org = make_mock_org(org_id=2, name="Acme")
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.put(
            "/api/v1/quotas/org/2",
            headers=auth_headers,
            json={"token_quota_daily": 500000},
        )
        assert resp.status_code == 200
        body = await resp.get_json()
        assert set(body.keys()) == {"organization_id", "organization_name", "message"}
