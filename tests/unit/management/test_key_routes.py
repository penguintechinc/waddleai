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


# ---------------------------------------------------------------------------
# PUT /api/v1/keys/<key_id> -- owner-editable vs privileged field split
#
# regression: audit-2026-09-14
#
# HIGH: the route carried only @require_auth and an ownership-only in-handler
# check, so a Role.USER could raise budget_limit_daily/monthly and
# tpm_limit/rpm_limit, widen allowed_models/allowed_providers, and clear
# expires_at on their own key -- self-service privilege escalation.
# Role.USER does not hold Permission.QUOTA_UPDATE (shared/auth/rbac.py).
#
# The same split is applied to PUT /api/v1/quotas/key/<key_id>; both routes
# share keys.PRIVILEGED_KEY_FIELDS so they cannot diverge again.
# ---------------------------------------------------------------------------


class TestUpdateKeyPrivilegeSplit:
    """Owner-editable vs privileged fields on PUT /api/v1/keys/<key_id>."""

    async def test_user_can_still_rename_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """`name` stays owner-editable -- the fix must not over-restrict.

        regression: audit-2026-09-14.
        """
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"name": "My Laptop Key"},
        )
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once_with(name="My Laptop Key")

    async def test_user_can_still_disable_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """`enabled` stays owner-editable. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"enabled": False},
        )
        assert resp.status_code == 200
        app_mock_db.return_value.update.assert_called_once_with(enabled=False)

    async def test_user_cannot_set_budget_limit_daily_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain user raising their own daily budget is refused. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"budget_limit_daily": 999999.0},
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body["required_scope"] == "quota:update"
        assert body["denied_fields"] == ["budget_limit_daily"]
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_cannot_widen_allowed_models_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Model access is not owner-editable. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"allowed_models": ["gpt-4", "claude-opus-4"]},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_cannot_widen_allowed_providers_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Provider access is not owner-editable. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"allowed_providers": ["openai"]},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_cannot_extend_expiry_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Expiry is not owner-editable. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"expires_at": "2099-01-01T00:00:00Z"},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_cannot_clear_expiry_on_own_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Clearing expiry (empty string) is refused too, and never parsed.

        regression: audit-2026-09-14 -- the privilege check runs on field
        names before any value is parsed, so an unprivileged caller cannot
        reach the `datetime.fromisoformat` call either.
        """
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"expires_at": ""},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()

    async def test_user_mixing_owner_and_privileged_fields_is_refused_whole(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A mixed body is refused outright -- the rename does not slip through.

        regression: audit-2026-09-14 -- partially applying the request would
        make the refusal invisible to the caller.
        """
        key = make_mock_key(key_id=10, user_id=2, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
            headers=user_auth_headers,
            json={"name": "Renamed", "tpm_limit": 10_000_000},
        )
        assert resp.status_code == 403
        body = await resp.get_json()
        assert body["denied_fields"] == ["tpm_limit"]
        app_mock_db.return_value.update.assert_not_called()

    async def test_admin_can_set_budget(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin holds quota:update, so the budget write succeeds. regression: audit-2026-09-14."""
        key = make_mock_key(key_id=10)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.put(
            "/api/v1/keys/10",
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
            "/api/v1/keys/10",
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
            "/api/v1/keys/10",
            headers=rm_auth_headers,
            json={"budget_limit_daily": 100.0},
        )
        assert resp.status_code == 403
        app_mock_db.return_value.update.assert_not_called()


# ---------------------------------------------------------------------------
# GET /api/v1/keys -- bounded list window
# regression: audit-2026-09-14-wave2
# ---------------------------------------------------------------------------


class TestListKeysPagination:
    """The formerly-unbounded list select is now bounded and echoes its window."""

    async def test_list_keys_response_includes_pagination(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A `pagination` block reflecting ?page=&limit= is returned.

        regression: audit-2026-09-14-wave2 -- pre-change the handler returned
        only {keys, total} with no pagination key, so this fails before the
        bounded-select change.
        """
        app_mock_db.return_value.select.return_value = make_select_result([make_mock_key()])

        resp = await client.get("/api/v1/keys?page=2&limit=25", headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["pagination"]["page"] == 2
        assert body["pagination"]["limit"] == 25


# ---------------------------------------------------------------------------
# DELETE /api/v1/keys/<id> -- the global-delete bypass is now a scope test
# regression: audit-2026-09-14-wave2
# ---------------------------------------------------------------------------


def _decoupled_token(*, role: str, scope: list[str], user_id: int, org_id: int = 1) -> str:
    """Sign a JWT whose `roles` and `scope` claims are deliberately decoupled.

    route_conftest.make_token derives scope from role, so it cannot express "a
    caller holding apikey:delete without being role=admin" -- which is exactly
    what proves the delete gate now keys on the scope claim, not the role name.
    verify_token (shared.auth.penguin_auth) reads the `scope` claim straight
    off the token, so g.user["scope"] carries whatever is set here.

    The signing key is taken from the *app's* provider (``_get_oidc_provider``,
    which the module-scoped flask_app fixture patches onto the auth module)
    rather than importing route_conftest's ``_test_oidc_provider`` directly:
    under pytest's rootdir the conftest is importable under two module names,
    each with its own ``lru_cache``d random keypair, so a direct import would
    sign with a key the app never verifies against and every request would 401.
    """
    from datetime import UTC, datetime, timedelta

    import jwt as _pyjwt

    from services.management.app.api.v1 import auth as _authmod

    provider = _authmod._get_oidc_provider()
    private_key, kid = provider._keystore.get_signing_key()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iss": "https://waddleai.localhost.local",
        "aud": ["waddleai-api"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "scope": scope,
        "roles": [role],
        "tenant": str(org_id),
        "teams": [],
        "ext": {"username": "synthetic"},
    }
    return _pyjwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


class TestDeleteKeyScopeGate:
    """The admin bypass on DELETE keys on the apikey:delete scope, not role name."""

    async def test_apikey_delete_scope_bypasses_ownership(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A non-admin role holding apikey:delete may revoke another user's key.

        regression: audit-2026-09-14-wave2 -- pre-change the gate read
        `user_role not in ["admin"]`, so this role=user caller hit the
        ownership branch and got 403. Keying on the (admin-only) apikey:delete
        scope instead returns 200, so this fails before the conversion.
        """
        key = make_mock_key(key_id=10, user_id=99, org_id=1)  # not owned by caller (id=2)
        app_mock_db.return_value.select.return_value.first.return_value = key
        token = _decoupled_token(role="user", scope=["apikey:delete"], user_id=2)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        resp = await client.delete("/api/v1/keys/10", headers=headers)
        assert resp.status_code == 200

    async def test_user_without_delete_scope_still_blocked_on_others_key(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """Ownership fallback preserved: a plain user cannot revoke another's key.

        regression: audit-2026-09-14-wave2 -- guards the conversion from
        widening access. Passes both before and after (a preservation guard).
        """
        key = make_mock_key(key_id=10, user_id=99, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/keys/10", headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_admin_scope_still_deletes_any_key(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin (holds apikey:delete) still bypasses ownership.

        regression: audit-2026-09-14-wave2 -- preservation guard.
        """
        key = make_mock_key(key_id=10, user_id=99, org_id=2)
        app_mock_db.return_value.select.return_value.first.return_value = key

        resp = await client.delete("/api/v1/keys/10", headers=auth_headers)
        assert resp.status_code == 200
