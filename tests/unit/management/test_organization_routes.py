"""
Unit tests for organization management routes: /api/v1/organizations/*
"""

from typing import Dict
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_org

# ---------------------------------------------------------------------------
# GET /api/v1/organizations
# ---------------------------------------------------------------------------


class TestListOrganizations:
    """Tests for GET /api/v1/organizations"""

    async def test_list_orgs_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin gets all orgs."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value = make_select_result([org])
        app_mock_db.return_value.count.return_value = 5

        resp = await client.get("/api/v1/organizations", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "organizations" in data

    async def test_list_orgs_regular_user(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user gets own org only."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value = make_select_result([org])
        app_mock_db.return_value.count.return_value = 2

        resp = await client.get("/api/v1/organizations", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_list_orgs_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/organizations")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/organizations/<id>
# ---------------------------------------------------------------------------


class TestGetOrganization:
    """Tests for GET /api/v1/organizations/<org_id>"""

    async def test_get_org_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get any org."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.return_value = org
        app_mock_db.return_value.count.return_value = 3

        resp = await client.get("/api/v1/organizations/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["name"] == "default"

    async def test_get_org_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin requesting non-existent org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/organizations/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_org_non_admin_own_org(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user can view own org (org_id matches token)."""
        org = make_mock_org(org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = org
        app_mock_db.return_value.count.return_value = 1

        resp = await client.get("/api/v1/organizations/1", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_get_org_non_admin_other_org(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user cannot view another org → 403 (checked before DB lookup)."""
        resp = await client.get("/api/v1/organizations/999", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/organizations
# ---------------------------------------------------------------------------


class TestCreateOrganization:
    """Tests for POST /api/v1/organizations"""

    async def test_create_org_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can create an organization."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"name": "NewOrg"},
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert isinstance(data.get("id"), int)

    async def test_create_org_missing_name(self, client, auth_headers: Dict) -> None:
        """Missing name returns 400."""
        resp = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"description": "no name here"},
        )
        assert resp.status_code == 400

    async def test_create_org_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_create_org_duplicate_name(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Duplicate org name returns 409."""
        existing = make_mock_org()
        app_mock_db.return_value.select.return_value.first.return_value = existing

        resp = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"name": "default"},
        )
        assert resp.status_code == 409

    async def test_create_org_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot create orgs → 403."""
        resp = await client.post(
            "/api/v1/organizations",
            headers=user_auth_headers,
            json={"name": "SomeOrg"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/organizations/<id>
# ---------------------------------------------------------------------------


class TestUpdateOrganization:
    """Tests for PUT /api/v1/organizations/<org_id>"""

    async def test_update_org_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can update an org."""
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.side_effect = [org, None]

        resp = await client.put(
            "/api/v1/organizations/1",
            headers=auth_headers,
            json={"description": "Updated desc"},
        )
        assert resp.status_code == 200

    async def test_update_org_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/organizations/999",
            headers=auth_headers,
            json={"description": "x"},
        )
        assert resp.status_code == 404

    async def test_update_org_name_conflict(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Duplicate name returns 409."""
        org = make_mock_org()
        other_org = make_mock_org(org_id=99, name="other")
        app_mock_db.return_value.select.return_value.first.side_effect = [org, other_org]

        resp = await client.put(
            "/api/v1/organizations/1",
            headers=auth_headers,
            json={"name": "other"},
        )
        assert resp.status_code == 409

    async def test_update_org_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.put(
            "/api/v1/organizations/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /api/v1/organizations/<id>
# ---------------------------------------------------------------------------


class TestDeleteOrganization:
    """Tests for DELETE /api/v1/organizations/<org_id>"""

    async def test_delete_org_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can soft-delete an org with no users."""
        org = make_mock_org(name="removable")
        app_mock_db.return_value.select.return_value.first.return_value = org
        app_mock_db.return_value.count.return_value = 0

        resp = await client.delete("/api/v1/organizations/2", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_default_org(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Cannot delete the 'default' org → 400."""
        org = make_mock_org(name="default")
        app_mock_db.return_value.select.return_value.first.return_value = org

        resp = await client.delete("/api/v1/organizations/1", headers=auth_headers)
        assert resp.status_code == 400

    async def test_delete_org_has_users(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Org with users returns 400."""
        org = make_mock_org(name="orgwithusers")
        app_mock_db.return_value.select.return_value.first.return_value = org
        app_mock_db.return_value.count.return_value = 3

        resp = await client.delete("/api/v1/organizations/5", headers=auth_headers)
        assert resp.status_code == 400

    async def test_delete_org_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/organizations/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_org_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot delete → 403."""
        resp = await client.delete("/api/v1/organizations/1", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/organizations/<id>/usage
# ---------------------------------------------------------------------------


class TestGetOrganizationUsage:
    """Tests for GET /api/v1/organizations/<org_id>/usage"""

    async def test_get_org_usage_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get org usage."""
        org = make_mock_org()
        org_sel = make_select_result([org])
        empty = make_select_result([])
        app_mock_db.return_value.select.side_effect = [org_sel, empty, empty, empty, empty]

        resp = await client.get("/api/v1/organizations/1/usage", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "usage" in data

    async def test_get_org_usage_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing org returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/organizations/999/usage", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_org_usage_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/organizations/1/usage")
        assert resp.status_code == 401
