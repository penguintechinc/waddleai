"""Unit tests for auth routes: /api/v1/auth/*."""

from unittest.mock import MagicMock

from tests.unit.management.route_conftest import make_mock_org, make_mock_user, make_token

# ---------------------------------------------------------------------------
# POST /api/v1/auth/login
# ---------------------------------------------------------------------------


class TestLogin:
    """Tests for POST /api/v1/auth/login."""

    async def test_login_success(self, client, app_mock_db: MagicMock) -> None:
        """Valid credentials return a JWT access token."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user
        app_mock_db.return_value.update.return_value = None

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"  # noqa: S105 -- OAuth2 field value, not a credential
        assert data["user"]["username"] == "admin"

    async def test_login_missing_fields(self, client, app_mock_db: MagicMock) -> None:
        """Missing username/password returns 400."""
        resp = await client.post("/api/v1/auth/login", json={"username": "admin"})
        assert resp.status_code == 400
        assert "required" in (await resp.get_json())["error"].lower()

    async def test_login_no_body(self, client) -> None:
        """No JSON body returns 400."""
        resp = await client.post(
            "/api/v1/auth/login",
            data="",
        )
        assert resp.status_code == 400

    async def test_login_user_not_found(self, client, app_mock_db: MagicMock) -> None:
        """Unknown username returns 401."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "password123"},
        )
        assert resp.status_code == 401

    async def test_login_disabled_user(self, client, app_mock_db: MagicMock) -> None:
        """Disabled account returns 401 with 'Account disabled' message."""
        user = make_mock_user(enabled=False)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 401
        assert "disabled" in (await resp.get_json())["error"].lower()

    async def test_login_wrong_password(self, client, app_mock_db: MagicMock) -> None:
        """Wrong password returns 401 with 'Invalid credentials'."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrongpass"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/auth/logout
# ---------------------------------------------------------------------------


class TestLogout:
    """Tests for POST /api/v1/auth/logout."""

    async def test_logout_success(self, client, auth_headers: dict) -> None:
        """Authenticated logout returns 200."""
        resp = await client.post("/api/v1/auth/logout", headers=auth_headers)
        assert resp.status_code == 200
        assert "Logged out" in (await resp.get_json())["message"]

    async def test_logout_no_auth(self, client) -> None:
        """Missing auth header returns 401."""
        resp = await client.post("/api/v1/auth/logout")
        assert resp.status_code == 401

    async def test_logout_invalid_token(self, client) -> None:
        """Invalid token returns 401."""
        resp = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": "Bearer bad.token.here"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/auth/refresh
# ---------------------------------------------------------------------------


class TestRefreshToken:
    """Tests for POST /api/v1/auth/refresh."""

    async def test_refresh_success(self, client, auth_headers: dict) -> None:
        """Authenticated refresh returns a new access token."""
        resp = await client.post("/api/v1/auth/refresh", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert "access_token" in data

    async def test_refresh_no_auth(self, client) -> None:
        """Missing auth header returns 401."""
        resp = await client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401

    async def test_refresh_expired_token(self, client) -> None:
        """Expired token returns 401."""
        expired = make_token(expires_hours=-1)
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {expired}"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/auth/me
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Tests for GET /api/v1/auth/me."""

    async def test_get_me_success(self, client, app_mock_db: MagicMock, auth_headers: dict) -> None:
        """Authenticated request returns current user details."""
        user = make_mock_user()
        org = make_mock_org()
        app_mock_db.return_value.select.return_value.first.side_effect = [user, org]

        resp = await client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["username"] == "admin"

    async def test_get_me_no_auth(self, client) -> None:
        """Missing auth header returns 401."""
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_get_me_user_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """DB returns no user row → 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/auth/change-password
# ---------------------------------------------------------------------------


class TestChangePassword:
    """Tests for POST /api/v1/auth/change-password."""

    async def test_change_password_success(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Valid old + new password returns 200."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "password123", "new_password": "NewSecure!9"},
        )
        assert resp.status_code == 200
        assert "changed" in (await resp.get_json())["message"].lower()

    async def test_change_password_no_body(self, client, auth_headers: dict) -> None:
        """Missing body returns 400."""
        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            data="",
        )
        assert resp.status_code == 400

    async def test_change_password_missing_fields(self, client, auth_headers: dict) -> None:
        """Missing new_password field returns 400."""
        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "password123"},
        )
        assert resp.status_code == 400

    async def test_change_password_too_short(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """New password under 8 chars returns 400."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "password123", "new_password": "short"},
        )
        assert resp.status_code == 400

    async def test_change_password_wrong_current(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """Wrong current password returns 401."""
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "wrongpass", "new_password": "NewSecure!9"},
        )
        assert resp.status_code == 401

    async def test_change_password_user_not_found(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """User row missing in DB returns 404."""
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "password123", "new_password": "NewSecure!9"},
        )
        assert resp.status_code == 404

    async def test_change_password_no_auth(self, client) -> None:
        """Missing auth header returns 401."""
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "password123", "new_password": "NewSecure!9"},
        )
        assert resp.status_code == 401
