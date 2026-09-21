"""Unit tests for auth routes: /api/v1/auth/*."""

from unittest.mock import MagicMock

import jwt as _jwt
import pytest

from services.management.app.services.login_throttle import (
    LoginThrottle,
    ThrottleConfig,
    reset_login_throttle,
)
from services.management.app.services.token_denylist import reset_token_denylist
from tests.unit.management.route_conftest import make_mock_org, make_mock_user, make_token


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset the process-wide throttle and denylist around every test.

    Both are module-level singletons, so without this a lockout tripped by
    one test leaks into the next and the suite's outcome depends on test
    order.
    """
    reset_login_throttle()
    reset_token_denylist()
    yield
    reset_login_throttle()
    reset_token_denylist()


def _install_throttle(max_failures: int = 3, lockout_seconds: int = 900) -> LoginThrottle:
    """Install a throttle with known thresholds and no shared cache backend."""
    throttle = LoginThrottle(
        config=ThrottleConfig(
            max_failures=max_failures,
            window_seconds=900,
            base_lockout_seconds=lockout_seconds,
            max_lockout_seconds=3600,
        ),
        client_provider=lambda: None,
    )
    reset_login_throttle(throttle)
    return throttle


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
        """A disabled account must not be distinguishable from a bad password.

        regression: audit-2026-09-14 -- this previously returned the distinct
        message "Account disabled", confirming the account exists.
        """
        user = make_mock_user(enabled=False)
        app_mock_db.return_value.select.return_value.first.return_value = user

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Invalid credentials"
        assert "disabled" not in (await resp.get_data(as_text=True)).lower()

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


# ---------------------------------------------------------------------------
# audit-2026-09-14 regression suite
# ---------------------------------------------------------------------------


class TestLoginBruteForceThrottle:
    """Per-account failed-login throttling.

    regression: audit-2026-09-14 (HIGH -- no brute-force protection on login).
    """

    @staticmethod
    async def _fail(client, username: str = "admin"):
        """Submit one wrong-password login for *username*."""
        return await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "wrong-password"},
        )

    async def test_lockout_after_n_failures(self, client, app_mock_db: MagicMock) -> None:
        """The Nth consecutive failure locks the account with a 429 + Retry-After.

        regression: audit-2026-09-14
        """
        _install_throttle(max_failures=3)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        assert (await self._fail(client)).status_code == 401
        assert (await self._fail(client)).status_code == 401

        locked = await self._fail(client)
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) > 0

        # Still locked on the next attempt, before any password work happens.
        assert (await self._fail(client)).status_code == 429

    async def test_lockout_applies_to_the_correct_password_too(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A locked account is refused even when the password is right.

        regression: audit-2026-09-14 -- a lockout that only blocked *wrong*
        passwords would not stop an attacker who has just guessed correctly.
        """
        _install_throttle(max_failures=2)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        await self._fail(client)
        assert (await self._fail(client)).status_code == 429

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 429

    async def test_successful_login_resets_the_counter(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A success clears accumulated failures, so the next run starts from zero.

        regression: audit-2026-09-14
        """
        _install_throttle(max_failures=3)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        assert (await self._fail(client)).status_code == 401
        assert (await self._fail(client)).status_code == 401

        ok = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert ok.status_code == 200

        # Without the reset these two would be failures 3 and 4 and would lock.
        assert (await self._fail(client)).status_code == 401
        assert (await self._fail(client)).status_code == 401

    async def test_counter_is_per_account_not_per_ip(self, client, app_mock_db: MagicMock) -> None:
        """Locking one account leaves every other account reachable.

        The whole test runs over one client (one source address), so a
        counter keyed on the IP would lock the second account out too.

        regression: audit-2026-09-14
        """
        _install_throttle(max_failures=2)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        await self._fail(client, "victim")
        assert (await self._fail(client, "victim")).status_code == 429

        # Same client, same source IP, different account -> ordinary 401.
        other = await self._fail(client, "bystander")
        assert other.status_code == 401

    async def test_unknown_usernames_are_throttled_too(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """Failures against a non-existent account still count.

        regression: audit-2026-09-14 -- if only real accounts were counted,
        "429 vs 401" would itself disclose which usernames exist.
        """
        _install_throttle(max_failures=2)
        app_mock_db.return_value.select.return_value.first.return_value = None

        await self._fail(client, "ghost")
        assert (await self._fail(client, "ghost")).status_code == 429


class TestTokenRevocationOnLogout:
    """Server-side token revocation.

    regression: audit-2026-09-14 (HIGH -- logout was a no-op stub).
    """

    async def test_token_is_rejected_after_logout(self, client, auth_headers: dict) -> None:
        """The very token used to log out stops working immediately after.

        regression: audit-2026-09-14
        """
        before = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert before.status_code == 200

        logout = await client.post("/api/v1/auth/logout", headers=auth_headers)
        assert logout.status_code == 200

        after = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert after.status_code == 401

    async def test_logout_does_not_revoke_unrelated_tokens(
        self, client, auth_headers: dict, user_auth_headers: dict
    ) -> None:
        """Revoking one token leaves every other live token working.

        Both tokens come from fixtures rather than a module-level
        ``make_token`` import: the direct import resolves a second copy of
        route_conftest whose OIDC keypair the app does not recognise, so
        every token it mints fails signature verification.

        regression: audit-2026-09-14
        """
        assert (await client.post("/api/v1/auth/logout", headers=auth_headers)).status_code == 200

        assert (await client.get("/api/v1/auth/verify", headers=auth_headers)).status_code == 401
        survivor = await client.get("/api/v1/auth/verify", headers=user_auth_headers)
        assert survivor.status_code == 200
        assert (await survivor.get_json())["user"]["id"] == 2

    async def test_revoked_token_is_refused_on_every_protected_route(
        self, client, auth_headers: dict
    ) -> None:
        """Revocation is enforced in require_auth, not just on one endpoint.

        regression: audit-2026-09-14
        """
        assert (await client.post("/api/v1/auth/logout", headers=auth_headers)).status_code == 200

        for path in ("/api/v1/auth/me", "/api/v1/auth/verify"):
            resp = await client.get(path, headers=auth_headers)
            assert resp.status_code == 401, path
        refresh = await client.post("/api/v1/auth/refresh", headers=auth_headers)
        assert refresh.status_code == 401


class TestTokenLifetime:
    """Issued-token lifetime policy.

    regression: audit-2026-09-14 (HIGH -- 24h advertised default).
    """

    async def test_login_token_expires_in_one_hour(self, client, app_mock_db: MagicMock) -> None:
        """Both the advertised expires_in and the signed exp claim are 1h.

        The advertised value used to be a hardcoded 86400 while penguin-aaa
        signed a 3600s token, so clients believed a dead session was alive
        for another eleven hours.

        regression: audit-2026-09-14
        """
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200
        data = await resp.get_json()

        assert data["expires_in"] == 3600

        claims = _jwt.decode(data["access_token"], options={"verify_signature": False})
        assert claims["exp"] - claims["iat"] == 3600
        assert claims["jti"]

    async def test_refresh_reports_the_real_lifetime(self, client, auth_headers: dict) -> None:
        """Refresh advertises the same 1h it actually signs.

        regression: audit-2026-09-14
        """
        resp = await client.post("/api/v1/auth/refresh", headers=auth_headers)
        assert resp.status_code == 200
        data = await resp.get_json()

        assert data["expires_in"] == 3600
        claims = _jwt.decode(data["access_token"], options={"verify_signature": False})
        assert claims["exp"] - claims["iat"] == 3600


class TestLoginUserEnumeration:
    """Login must not disclose account state to an unauthenticated caller.

    regression: audit-2026-09-14 (MEDIUM -- user enumeration on login).
    """

    async def test_all_failure_modes_return_an_identical_response(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """Unknown user, disabled user and wrong password are indistinguishable.

        regression: audit-2026-09-14
        """
        results = []

        # Unknown user.
        _install_throttle(max_failures=99)
        app_mock_db.return_value.select.return_value.first.return_value = None
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "password123"}
        )
        results.append((resp.status_code, await resp.get_json()))

        # Disabled user, correct password.
        _install_throttle(max_failures=99)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user(
            enabled=False
        )
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "password123"}
        )
        results.append((resp.status_code, await resp.get_json()))

        # Enabled user, wrong password.
        _install_throttle(max_failures=99)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "nope"}
        )
        results.append((resp.status_code, await resp.get_json()))

        assert len({(status, repr(body)) for status, body in results}) == 1, results
        assert results[0][0] == 401
        assert results[0][1] == {"error": "Invalid credentials"}

    async def test_password_is_verified_even_when_the_user_is_absent(
        self, client, app_mock_db: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The password is verified for unknown users too, erasing the timing signal.

        Asserting on wall-clock timing would be flaky in CI, so this asserts
        the mechanism instead: the verification helper is invoked on the
        unknown-user path, against the dummy hash rather than a real one.

        regression: audit-2026-09-14
        """
        import services.management.app.api.v1.auth as auth_mod

        calls: list[tuple[str, str]] = []

        def _spy(password: str, password_hash: str) -> bool:
            calls.append((password, password_hash))
            return False

        monkeypatch.setattr(auth_mod, "_verify_password", _spy)
        app_mock_db.return_value.select.return_value.first.return_value = None

        resp = await client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "password123"}
        )

        assert resp.status_code == 401
        assert len(calls) == 1
        assert calls[0][0] == "password123"
        assert calls[0][1] == auth_mod._dummy_password_hash()
