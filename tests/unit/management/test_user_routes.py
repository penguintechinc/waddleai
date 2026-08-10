"""
Unit tests for user management routes: /api/v1/users/*
"""

from datetime import datetime
from typing import Dict
from unittest.mock import MagicMock

from tests.unit.management.conftest import make_select_result
from tests.unit.management.route_conftest import make_mock_org, make_mock_user

# ---------------------------------------------------------------------------
# GET /api/v1/users
# ---------------------------------------------------------------------------


class TestListUsers:
    """Tests for GET /api/v1/users"""

    async def test_list_users_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin gets all users."""
        user = make_mock_user()
        user.created_at = datetime(2025, 1, 1)
        user.last_login_at = datetime(2025, 1, 2)
        app_mock_db.return_value.select.return_value = make_select_result([user])

        resp = await client.get("/api/v1/users", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "users" in data
        assert data["total"] >= 0

    async def test_list_users_resource_manager(self, client, app_mock_db: MagicMock, rm_auth_headers: Dict) -> None:
        """Resource manager gets own org users only."""
        user = make_mock_user(role="resource_manager")
        user.created_at = None
        user.last_login_at = None
        app_mock_db.return_value.select.return_value = make_select_result([user])

        resp = await client.get("/api/v1/users", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_list_users_regular_user(self, client, app_mock_db: MagicMock, user_auth_headers: Dict) -> None:
        """Regular user gets only own record."""
        user = make_mock_user(user_id=2, role="user")
        user.created_at = None
        user.last_login_at = None
        app_mock_db.return_value.select.return_value = make_select_result([user])

        resp = await client.get("/api/v1/users", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_list_users_no_auth(self, client) -> None:
        """Missing auth returns 401."""
        resp = await client.get("/api/v1/users")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/users/<id>
# ---------------------------------------------------------------------------


class TestGetUser:
    """Tests for GET /api/v1/users/<user_id>"""

    async def test_get_user_admin(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can get any user."""
        user = make_mock_user()
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.side_effect = [user, org]

        resp = await client.get("/api/v1/users/1", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["username"] == "admin"

    async def test_get_user_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/users/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_user_resource_manager_own_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager can view user in own org."""
        user = make_mock_user(user_id=5, org_id=1)
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.side_effect = [user, org]

        resp = await client.get("/api/v1/users/5", headers=rm_auth_headers)
        assert resp.status_code == 200

    async def test_get_user_resource_manager_different_org(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot view user in another org → 403."""
        user = make_mock_user(user_id=10, org_id=99)  # Different org
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.get("/api/v1/users/10", headers=rm_auth_headers)
        assert resp.status_code == 403

    async def test_get_user_plain_user_own_record(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user can view their own record."""
        user = make_mock_user(user_id=2, role="user")
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.side_effect = [user, org]

        resp = await client.get("/api/v1/users/2", headers=user_auth_headers)
        assert resp.status_code == 200

    async def test_get_user_plain_user_other_record(
        self, client, app_mock_db: MagicMock, user_auth_headers: Dict
    ) -> None:
        """Regular user cannot view someone else's record → 403."""
        other_user = make_mock_user(user_id=99, role="user")
        app_mock_db.return_value.select.return_value.first.return_value = other_user

        resp = await client.get("/api/v1/users/99", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/users
# ---------------------------------------------------------------------------


class TestCreateUser:
    """Tests for POST /api/v1/users"""

    async def test_create_user_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can create a user when all fields are present."""
        org = make_mock_org()
        # org lookup → success; duplicate check → None (no existing)
        app_mock_db.return_value.select.return_value.first.side_effect = [org, None]
        app_mock_db.users.insert.return_value = 42

        resp = await client.post(
            "/api/v1/users",
            headers=auth_headers,
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "Secure!123",
            },
        )
        assert resp.status_code == 201
        data = await resp.get_json()
        assert "id" in data
        assert isinstance(data["id"], int)

    async def test_create_user_missing_required_field(self, client, auth_headers: Dict) -> None:
        """Missing password field returns 400."""
        resp = await client.post(
            "/api/v1/users",
            headers=auth_headers,
            json={"username": "newuser", "email": "new@example.com"},
        )
        assert resp.status_code == 400

    async def test_create_user_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.post(
            "/api/v1/users",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_create_user_org_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Org not found returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/users",
            headers=auth_headers,
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "Secure!123",
                "organization_id": 999,
            },
        )
        assert resp.status_code == 404

    async def test_create_user_duplicate(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Duplicate username/email returns 409."""
        org = make_mock_org()
        existing = make_mock_user()
        app_mock_db.return_value.select.return_value.first.side_effect = [org, existing]

        resp = await client.post(
            "/api/v1/users",
            headers=auth_headers,
            json={
                "username": "admin",
                "email": "admin@example.com",
                "password": "Secure!123",
            },
        )
        assert resp.status_code == 409

    async def test_create_user_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot create users → 403."""
        resp = await client.post(
            "/api/v1/users",
            headers=user_auth_headers,
            json={
                "username": "newuser",
                "email": "new@example.com",
                "password": "Secure!123",
            },
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PUT /api/v1/users/<id>
# ---------------------------------------------------------------------------


class TestUpdateUser:
    """Tests for PUT /api/v1/users/<user_id>"""

    async def test_update_user_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can update a user."""
        user = make_mock_user()
        # email uniqueness check → None (no conflict)
        app_mock_db.return_value.select.return_value.first.side_effect = [user, None]

        resp = await client.put(
            "/api/v1/users/1",
            headers=auth_headers,
            json={"email": "new@example.com"},
        )
        assert resp.status_code == 200

    async def test_update_user_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.put(
            "/api/v1/users/999",
            headers=auth_headers,
            json={"email": "new@example.com"},
        )
        assert resp.status_code == 404

    async def test_update_user_no_body(self, client, auth_headers: Dict) -> None:
        """No body returns 400."""
        resp = await client.put(
            "/api/v1/users/1",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_update_user_email_conflict(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Email already taken by another user returns 409."""
        user = make_mock_user()
        existing_other = make_mock_user(user_id=99, email="taken@example.com")
        app_mock_db.return_value.select.return_value.first.side_effect = [user, existing_other]

        resp = await client.put(
            "/api/v1/users/1",
            headers=auth_headers,
            json={"email": "taken@example.com"},
        )
        assert resp.status_code == 409

    async def test_update_user_rm_admin_role_forbidden(
        self, client, app_mock_db: MagicMock, rm_auth_headers: Dict
    ) -> None:
        """Resource manager cannot promote to admin → 403."""
        user = make_mock_user(user_id=5, org_id=1)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.put(
            "/api/v1/users/5",
            headers=rm_auth_headers,
            json={"role": "admin"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/v1/users/<id>
# ---------------------------------------------------------------------------


class TestDeleteUser:
    """Tests for DELETE /api/v1/users/<user_id>"""

    async def test_delete_user_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can soft-delete a different user."""
        user = make_mock_user(user_id=99)  # Different from token user_id=1
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.delete("/api/v1/users/99", headers=auth_headers)
        assert resp.status_code == 200

    async def test_delete_user_self(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin cannot delete own account → 400."""
        user = make_mock_user(user_id=1)  # Same as token user_id=1
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.delete("/api/v1/users/1", headers=auth_headers)
        assert resp.status_code == 400

    async def test_delete_user_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.delete("/api/v1/users/999", headers=auth_headers)
        assert resp.status_code == 404

    async def test_delete_user_non_admin_forbidden(self, client, user_auth_headers: Dict) -> None:
        """Regular user cannot delete → 403."""
        resp = await client.delete("/api/v1/users/99", headers=user_auth_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/v1/users/<id>/enable
# ---------------------------------------------------------------------------


class TestEnableUser:
    """Tests for POST /api/v1/users/<user_id>/enable"""

    async def test_enable_user_success(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Admin can enable a user."""
        user = make_mock_user(enabled=False)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/1/enable", headers=auth_headers)
        assert resp.status_code == 200

    async def test_enable_user_not_found(self, client, app_mock_db: MagicMock, auth_headers: Dict) -> None:
        """Missing user returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post("/api/v1/users/999/enable", headers=auth_headers)
        assert resp.status_code == 404

    async def test_enable_user_rm_different_org(self, client, app_mock_db: MagicMock, rm_auth_headers: Dict) -> None:
        """Resource manager cannot enable user from another org → 403."""
        user = make_mock_user(user_id=10, org_id=99)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post("/api/v1/users/10/enable", headers=rm_auth_headers)
        assert resp.status_code == 403
