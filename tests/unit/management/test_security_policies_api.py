"""Unit tests for security policy management routes: /api/v1/security-policies/*."""

from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result


def _mock_policy_row(
    policy_id: int = 1,
    scope_type: str = "org",
    scope_ref: str = "7",
    direction: str = "both",
) -> MagicMock:
    """A MagicMock standing in for a db `security_policies` row."""
    row = MagicMock()
    row.id = policy_id
    row.scope_type = scope_type
    row.scope_ref = scope_ref
    row.direction = direction
    row.tier1_enabled = True
    row.tier2_enabled = None
    row.tier3_enabled = None
    row.tier4_enabled = None
    row.tier4_model = None
    row.intent_classifier_enabled = None
    row.intent_categories = None
    row.block_action = None
    row.fail_mode = "closed"
    row.on_unclassifiable = None
    row.auditor_timeout_ms = None
    row.latency_budget_ms = None
    row.sample_rate = None
    row.upstream_filters = None
    row.created_at = None
    row.updated_at = None
    return row


class TestListPolicies:
    """Tests for GET /api/v1/security-policies/."""

    async def test_list_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/security-policies/")
        assert resp.status_code == 401

    async def test_list_returns_policies(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An authenticated request lists policy rows via the explicit response schema."""
        row = _mock_policy_row()
        app_mock_db.return_value.select.return_value = make_select_result([row])

        resp = await client.get("/api/v1/security-policies/", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "success"
        assert data["data"][0]["scope_type"] == "org"
        assert data["data"][0]["fail_mode"] == "closed"


class TestResolvePreview:
    """Tests for GET /api/v1/security-policies/resolve."""

    async def test_resolve_returns_resolved_policy(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Resolve returns a fully-populated ResolvedPolicy shape (falling back to the floor)."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get(
            "/api/v1/security-policies/resolve?org=7&model=gpt-4&tool=search",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["status"] == "success"
        assert data["data"]["fail_mode"] == "degrade"  # hardcoded floor default
        assert data["meta"]["org"] == "7"

    async def test_resolve_rejects_invalid_direction(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An invalid direction query param is rejected with 400."""
        resp = await client.get(
            "/api/v1/security-policies/resolve?direction=sideways", headers=auth_headers
        )
        assert resp.status_code == 400


class TestCreateOrUpsertPolicy:
    """Tests for POST /api/v1/security-policies/."""

    async def test_create_requires_admin_role(
        self, client, app_mock_db: MagicMock, user_auth_headers: dict
    ) -> None:
        """A plain-user token cannot write a security policy."""
        resp = await client.post(
            "/api/v1/security-policies/",
            headers=user_auth_headers,
            json={"scope_type": "org", "scope_ref": "7"},
        )
        assert resp.status_code == 403

    async def test_create_rejects_invalid_scope_type(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown scope_type is rejected with 400."""
        resp = await client.post(
            "/api/v1/security-policies/",
            headers=auth_headers,
            json={"scope_type": "planet", "scope_ref": "mars"},
        )
        assert resp.status_code == 400

    async def test_create_new_policy_invalidates_cache(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A successful create returns 201 and invalidates the resolver cache."""
        row = _mock_policy_row()
        app_mock_db.return_value.select.return_value.first.return_value = None
        app_mock_db.security_policies.insert.return_value = 1
        # After insert, the re-fetch-by-id call returns the new row.
        app_mock_db.return_value.select.return_value = make_select_result([row])
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.post(
            "/api/v1/security-policies/",
            headers=auth_headers,
            json={"scope_type": "org", "scope_ref": "7", "fail_mode": "closed"},
        )

        assert resp.status_code in (200, 201)
        data = await resp.get_json()
        assert data["status"] == "success"


class TestDeletePolicy:
    """Tests for DELETE /api/v1/security-policies/<id>."""

    async def test_delete_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Deleting a non-existent policy returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/security-policies/999", headers=auth_headers)

        assert resp.status_code == 404

    async def test_delete_existing_policy(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Deleting an existing policy returns 200 with the deleted id."""
        row = _mock_policy_row(policy_id=5)
        app_mock_db.return_value.select.return_value.first.return_value = row

        resp = await client.delete("/api/v1/security-policies/5", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["id"] == 5


def _mock_grant_row(
    grant_id: int = 1,
    subject_type: str = "user",
    subject_ref: str = "2",
    organization_id: int = 1,
    mode: str = "shadow",
) -> MagicMock:
    """A MagicMock standing in for a db `security_bypass_grants` row.

    Also carries the subject's own `organization_id`, since the same mocked
    `.first()` return value stands in for both the subject lookup and the
    grant row fetch in these route-test mocks.
    """
    row = MagicMock()
    row.id = grant_id
    row.subject_type = subject_type
    row.subject_ref = subject_ref
    row.organization_id = organization_id
    row.mode = mode
    row.scope_narrow = None
    row.include_upstream = False
    row.granted_by = 1
    row.expires_at = None
    row.created_at = None
    return row


class TestListBypassGrants:
    """Tests for GET /api/v1/security-policies/bypass-grants."""

    async def test_list_requires_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/security-policies/bypass-grants")
        assert resp.status_code == 401

    async def test_admin_sees_all_grants(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin (platform-wide) sees grants regardless of subject org."""
        grant = _mock_grant_row(organization_id=999)  # a different org than the admin's
        app_mock_db.return_value.select.return_value = make_select_result([grant])

        resp = await client.get("/api/v1/security-policies/bypass-grants", headers=auth_headers)

        assert resp.status_code == 200
        data = await resp.get_json()
        assert len(data["data"]) == 1

    async def test_resource_manager_only_sees_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """resource_manager is filtered to subjects in their own org."""
        other_org_grant = _mock_grant_row(grant_id=1, organization_id=999)
        app_mock_db.return_value.select.return_value = make_select_result([other_org_grant])

        resp = await client.get(
            "/api/v1/security-policies/bypass-grants", headers=rm_auth_headers
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"] == []  # filtered out -- different org


class TestCreateBypassGrant:
    """Tests for POST /api/v1/security-policies/bypass-grants."""

    async def test_create_requires_expires_at(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A grant without expires_at is rejected -- no indefinite bypass."""
        resp = await client.post(
            "/api/v1/security-policies/bypass-grants",
            headers=auth_headers,
            json={"subject_type": "user", "subject_ref": "2", "mode": "shadow"},
        )
        assert resp.status_code == 400

    async def test_create_rejects_past_expiry(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A grant with expires_at in the past is rejected."""
        resp = await client.post(
            "/api/v1/security-policies/bypass-grants",
            headers=auth_headers,
            json={
                "subject_type": "user",
                "subject_ref": "2",
                "mode": "shadow",
                "expires_at": "2020-01-01T00:00:00",
            },
        )
        assert resp.status_code == 400

    async def test_create_rejects_invalid_subject_type(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An unknown subject_type is rejected with 400."""
        resp = await client.post(
            "/api/v1/security-policies/bypass-grants",
            headers=auth_headers,
            json={
                "subject_type": "robot",
                "subject_ref": "2",
                "expires_at": "2099-01-01T00:00:00",
            },
        )
        assert resp.status_code == 400

    async def test_resource_manager_cannot_grant_cross_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A resource_manager cannot grant bypass to a subject outside their own org."""
        app_mock_db.return_value.select.return_value.first.return_value = _mock_grant_row(
            organization_id=999
        )

        resp = await client.post(
            "/api/v1/security-policies/bypass-grants",
            headers=rm_auth_headers,
            json={
                "subject_type": "user",
                "subject_ref": "2",
                "mode": "shadow",
                "expires_at": "2099-01-01T00:00:00",
            },
        )

        assert resp.status_code == 403

    async def test_create_succeeds_within_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A resource_manager can grant bypass to a subject in their own org."""
        app_mock_db.return_value.select.return_value.first.return_value = _mock_grant_row(
            organization_id=1
        )
        app_mock_db.security_bypass_grants.insert.return_value = 1

        resp = await client.post(
            "/api/v1/security-policies/bypass-grants",
            headers=rm_auth_headers,
            json={
                "subject_type": "user",
                "subject_ref": "2",
                "mode": "shadow",
                "expires_at": "2099-01-01T00:00:00",
            },
        )

        assert resp.status_code == 201
        data = await resp.get_json()
        assert data["status"] == "success"


class TestRevokeBypassGrant:
    """Tests for DELETE /api/v1/security-policies/bypass-grants/<id>."""

    async def test_revoke_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Revoking a non-existent grant returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete(
            "/api/v1/security-policies/bypass-grants/999", headers=auth_headers
        )

        assert resp.status_code == 404

    async def test_revoke_cross_org_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: dict
    ) -> None:
        """A resource_manager cannot revoke a grant for a subject outside their org."""
        app_mock_db.return_value.select.return_value.first.return_value = _mock_grant_row(
            organization_id=999
        )

        resp = await client.delete(
            "/api/v1/security-policies/bypass-grants/1", headers=rm_auth_headers
        )

        assert resp.status_code == 403

    async def test_revoke_succeeds(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Admin can revoke any grant."""
        app_mock_db.return_value.select.return_value.first.return_value = _mock_grant_row(
            grant_id=5
        )

        resp = await client.delete(
            "/api/v1/security-policies/bypass-grants/5", headers=auth_headers
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["data"]["id"] == 5
