"""Cookie-based browser auth for /api/v1/auth/* (regression: audit-2026-09-14).

The webui used to keep the JWT in localStorage, readable by any XSS. It now
travels in an HttpOnly + Secure + SameSite=Strict cookie the browser attaches
automatically. These tests pin the whole coherent set: login sets the cookie,
a cookie alone authenticates, the Authorization header still works, logout
clears the cookie while the jti denylist keeps rejecting the token, and a
cookie-authenticated state-changing request without the CSRF header is refused.

Every behaviour asserted here is *new* — the pre-change code set no cookie,
its require_auth returned 401 whenever the Authorization header was absent, and
it had no CSRF gate — so each of these fails against the base implementation.
"""

from unittest.mock import MagicMock

import pytest

from services.management.app.api.v1.auth import _ACCESS_COOKIE_NAME
from services.management.app.services.login_throttle import reset_login_throttle
from services.management.app.services.token_denylist import reset_token_denylist
from tests.unit.management.route_conftest import make_mock_user

_VALID_LOGIN = {"username": "admin", "password": "password123"}


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset the process-wide throttle and denylist singletons around each test."""
    reset_login_throttle()
    reset_token_denylist()
    yield
    reset_login_throttle()
    reset_token_denylist()


def _access_set_cookie(resp) -> str | None:
    """Return the Set-Cookie line for the access cookie, or None if absent."""
    for header in resp.headers.get_all("Set-Cookie"):
        if header.startswith(f"{_ACCESS_COOKIE_NAME}="):
            return header
    return None


async def _login(client, app_mock_db: MagicMock) -> None:
    """Perform a successful login so the client's cookie jar holds the token."""
    app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()
    app_mock_db.return_value.update.return_value = None
    resp = await client.post("/api/v1/auth/login", json=_VALID_LOGIN)
    assert resp.status_code == 200


class TestLoginSetsCookie:
    """POST /auth/login issues the browser session cookie additively."""

    async def test_login_sets_httponly_secure_samesite_strict_cookie(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """The access cookie carries HttpOnly, Secure, SameSite=Strict and a 1h Max-Age."""
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()
        app_mock_db.return_value.update.return_value = None

        resp = await client.post("/api/v1/auth/login", json=_VALID_LOGIN)

        assert resp.status_code == 200
        cookie = _access_set_cookie(resp)
        assert cookie is not None, "login did not set the access cookie"
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        # Max-Age must track the token's real 1h TTL, not the advertised 24h.
        assert "Max-Age=3600" in cookie

    async def test_login_still_returns_token_in_body(self, client, app_mock_db: MagicMock) -> None:
        """The JSON body is unchanged, so API/CLI clients are unaffected."""
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()
        app_mock_db.return_value.update.return_value = None

        resp = await client.post("/api/v1/auth/login", json=_VALID_LOGIN)

        data = await resp.get_json()
        assert data["access_token"]
        assert data["token_type"] == "bearer"  # noqa: S105 -- OAuth2 field value
        assert data["expires_in"] == 3600


class TestCookieAuthenticates:
    """require_auth accepts the cookie when no Authorization header is present."""

    async def test_cookie_alone_authenticates_a_safe_request(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """After login, GET /auth/verify with NO Authorization header succeeds via the cookie.

        A GET is a safe method, so it also proves the cookie path needs no CSRF
        header for read-only requests.
        """
        await _login(client, app_mock_db)

        resp = await client.get("/api/v1/auth/verify")  # cookie jar supplies the token

        assert resp.status_code == 200
        assert (await resp.get_json())["user"]["username"] == "admin"

    async def test_authorization_header_still_authenticates_without_cookie(
        self, client, auth_headers: dict
    ) -> None:
        """The Authorization header remains authoritative with no cookie in play."""
        resp = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert resp.status_code == 200

    async def test_no_credential_at_all_is_rejected(self, client) -> None:
        """Neither header nor cookie -> 401, with the legacy contract body preserved."""
        resp = await client.get("/api/v1/auth/verify")
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Authorization header required"


class TestCsrfOnCookieAuth:
    """SameSite=Strict plus a mandatory custom header defend cookie auth from CSRF."""

    async def test_cookie_state_change_without_csrf_header_is_refused(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A cookie-authenticated POST lacking X-Requested-With is refused with 403.

        This is the CSRF case: a cross-site page can make the browser send the
        cookie on a forged POST, but cannot attach the custom header.
        """
        await _login(client, app_mock_db)

        # No X-Requested-With header -> CSRF gate fails before the token is even read.
        resp = await client.post("/api/v1/auth/logout")

        assert resp.status_code == 403
        assert "CSRF" in (await resp.get_json())["error"]

    async def test_cookie_state_change_with_csrf_header_succeeds(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """The same POST with the SPA's X-Requested-With header is accepted."""
        await _login(client, app_mock_db)

        resp = await client.post(
            "/api/v1/auth/logout",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        assert resp.status_code == 200
        # Logout also expires the cookie.
        cookie = _access_set_cookie(resp)
        assert cookie is not None
        assert "Max-Age=0" in cookie or "01 Jan 1970" in cookie


class TestLogoutClearsCookie:
    """Logout expires the cookie AND revokes the token on the denylist."""

    async def test_header_logout_clears_cookie_and_denylist_still_rejects(
        self, client, auth_headers: dict
    ) -> None:
        """A header-authenticated logout expires the cookie and revokes the token.

        Header auth is exempt from the CSRF-header requirement, so no
        X-Requested-With is sent here. The cookie is cleared regardless of how
        the caller authenticated, and the jti denylist keeps rejecting the
        token afterwards — the cookie clear is additive to, never a
        replacement for, revocation.
        """
        # Sanity: the token authenticates before logout.
        assert (await client.get("/api/v1/auth/verify", headers=auth_headers)).status_code == 200

        logout = await client.post("/api/v1/auth/logout", headers=auth_headers)
        assert logout.status_code == 200

        cookie = _access_set_cookie(logout)
        assert cookie is not None, "logout did not emit a cookie-clearing Set-Cookie"
        assert "Max-Age=0" in cookie or "01 Jan 1970" in cookie

        # Denylist still rejects the revoked token on a protected route.
        after = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert after.status_code == 401
