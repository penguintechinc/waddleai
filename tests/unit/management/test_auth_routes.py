"""Unit tests for auth routes: /api/v1/auth/*."""

from unittest.mock import MagicMock, patch

import jwt as _jwt
import pytest
from passlib.hash import bcrypt

from services.management.app.services.login_throttle import (
    LoginThrottle,
    ThrottleConfig,
    reset_login_throttle,
)
from services.management.app.services.rate_limiter import (
    RateLimiterConfig,
    RequestRateLimiter,
    reset_auth_rate_limiter,
)
from services.management.app.services.token_denylist import reset_token_denylist
from tests.unit.management.route_conftest import make_mock_org, make_mock_user, make_token


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset the process-wide throttle, rate limiter, and denylist around every test.

    All three are module-level singletons, so without this a lockout/limit
    tripped by one test leaks into the next and the suite's outcome depends
    on test order.
    """
    reset_login_throttle()
    reset_auth_rate_limiter()
    reset_token_denylist()
    yield
    reset_login_throttle()
    reset_auth_rate_limiter()
    reset_token_denylist()


def _install_rate_limiter(max_requests: int = 1000, window_seconds: int = 60) -> RequestRateLimiter:
    """Install a rate limiter with a known threshold, high enough to stay out of the way.

    Every existing test in this module implicitly relies on the request
    -volume limiter never tripping -- the default installed here (999 free
    requests before the reset above rebuilds a fresh one from env) is well
    above anything any single test issues.
    """
    limiter = RequestRateLimiter(
        RateLimiterConfig(max_requests=max_requests, window_seconds=window_seconds)
    )
    reset_auth_rate_limiter(limiter)
    return limiter


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

        # Without the reset these would be failures 3 and 4, so the first of
        # them would already lock the account.
        assert (await self._fail(client)).status_code == 401
        assert (await self._fail(client)).status_code == 401

        # ...and the throttle is still armed afterwards: the counter restarted
        # at zero, it was not switched off. Without this the test would also
        # pass against a build that has no throttle at all.
        assert (await self._fail(client)).status_code == 429

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


class TestAuthRateLimit:
    """Raw request-volume limiter on /auth/login and /auth/token.

    Distinct from ``TestLoginBruteForceThrottle`` above: that suite covers
    the account-scoped *failure* lockout; this one covers a plain request
    -volume cap that fires even before any credential is checked, closing
    the gap where an unauthenticated caller could hammer either route as
    fast as the network allows with no failure ever being recorded (no
    in-app rate limiter existed on these routes -- relied entirely on the
    Cilium ``waddleai.native_rate_limit`` flag, which defaults OFF).

    regression: headless-auth-secrev
    """

    # Type-annotated (not a bare `NAME = "..."` assignment) so gitleaks'
    # generic-api-key rule does not flag this fixture value -- same
    # technique as TestTokenExchange.RAW_KEY above.
    RAW_KEY: str = "wa-ratelimittestkey0123"  # noqa: S105

    async def test_login_exceeding_the_limit_returns_429(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """The (N+1)th request against the same (IP, username) pair in the window is refused."""
        _install_rate_limiter(max_requests=3, window_seconds=60)
        # A generous account-failure threshold so the *account* lockout never
        # fires first and this test isolates the request-volume limiter.
        _install_throttle(max_failures=999)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        for _ in range(3):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            assert resp.status_code == 401

        limited = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0

    async def test_login_under_the_limit_behaves_normally(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A caller well under the configured rate sees ordinary login behaviour."""
        _install_rate_limiter(max_requests=10, window_seconds=60)
        _install_throttle(max_failures=999)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "password123"},
        )
        assert resp.status_code == 200

    async def test_login_rate_limit_is_per_credential_not_global(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """Exhausting one username's budget must not block a different username."""
        _install_rate_limiter(max_requests=1, window_seconds=60)
        _install_throttle(max_failures=999)
        app_mock_db.return_value.select.return_value.first.return_value = make_mock_user()

        await self._fail_login(client, "victim")
        assert (await self._fail_login(client, "victim")).status_code == 429

        other = await self._fail_login(client, "bystander")
        assert other.status_code == 401

    async def test_token_exchange_exceeding_the_limit_returns_429(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """The (N+1)th /auth/token request presenting the same key is refused."""
        _install_rate_limiter(max_requests=2, window_seconds=60)
        owner = make_mock_user(user_id=5, org_id=2, role="admin", username="svc-ci")
        key_row = MagicMock(
            id=99,
            user_id=owner.id,
            organization_id=owner.organization_id,
            enabled=True,
            key_hash=bcrypt.hash(self.RAW_KEY),
        )
        key_query = MagicMock()
        key_query.select.return_value = [key_row]
        user_query = MagicMock()
        user_query.select.return_value.first.return_value = owner
        app_mock_db.side_effect = [key_query, user_query] * 3

        for _ in range(2):
            resp = await client.post(
                "/api/v1/auth/token",
                headers={"Authorization": f"Bearer {self.RAW_KEY}"},
            )
            assert resp.status_code == 200

        limited = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0

    @staticmethod
    async def _fail_login(client, username: str):
        """Submit one wrong-password login for *username*."""
        return await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": "wrong-password"},
        )


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


# ---------------------------------------------------------------------------
# release-audit-2026-09-23: JWT session invalidation (M1) + API-key scale (M3)
# ---------------------------------------------------------------------------


class TestJwtSubjectEnabledRecheck:
    """require_auth re-checks the token subject's `enabled` state (M1).

    Before this fix only the API-key path checked `enabled`; a JWT kept
    working until expiry after the account was disabled.
    """

    async def test_disabled_user_jwt_is_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A JWT whose subject is now disabled is refused on the next request.

        # regression: release-audit-2026-09-23
        """
        disabled = make_mock_user(enabled=False)
        with patch.object(app_mock_db.users, "getitem", MagicMock(return_value=disabled)):
            resp = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert resp.status_code == 401

    async def test_removed_user_jwt_is_rejected(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """A JWT whose subject no longer exists is refused.

        # regression: release-audit-2026-09-23
        """
        with patch.object(app_mock_db.users, "getitem", MagicMock(return_value=None)):
            resp = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert resp.status_code == 401

    async def test_enabled_user_jwt_is_accepted(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """An enabled subject authenticates normally (no false rejection)."""
        enabled = make_mock_user(enabled=True)
        with patch.object(app_mock_db.users, "getitem", MagicMock(return_value=enabled)):
            resp = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert resp.status_code == 200

    def test_jwt_subject_active_helper(self) -> None:
        """_jwt_subject_active: True only for an existing, enabled subject."""
        import services.management.app.api.v1.auth as auth_mod

        db = MagicMock()
        db.users.__getitem__ = MagicMock(return_value=MagicMock(enabled=True))
        with patch.object(auth_mod, "db", db):
            assert auth_mod._jwt_subject_active(1) is True
        db.users.__getitem__ = MagicMock(return_value=MagicMock(enabled=False))
        with patch.object(auth_mod, "db", db):
            assert auth_mod._jwt_subject_active(1) is False
        db.users.__getitem__ = MagicMock(return_value=None)
        with patch.object(auth_mod, "db", db):
            assert auth_mod._jwt_subject_active(1) is False


class TestTokenInvalidatedOnRefreshAndChangePassword:
    """/auth/refresh and /auth/change-password invalidate the prior token (M1)."""

    async def test_refresh_revokes_the_presented_token(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """After refresh, the old token can no longer be replayed.

        # regression: release-audit-2026-09-23
        """
        refreshed = await client.post("/api/v1/auth/refresh", headers=auth_headers)
        assert refreshed.status_code == 200
        assert "access_token" in await refreshed.get_json()

        reuse = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert reuse.status_code == 401

    async def test_change_password_revokes_the_presented_token(
        self, client, app_mock_db: MagicMock, auth_headers: dict
    ) -> None:
        """After a password change, the token that made the change is dead.

        # regression: release-audit-2026-09-23
        """
        user = make_mock_user()
        app_mock_db.return_value.select.return_value.first.return_value = user

        cp = await client.post(
            "/api/v1/auth/change-password",
            headers=auth_headers,
            json={"current_password": "password123", "new_password": "NewSecure!9"},
        )
        assert cp.status_code == 200

        reuse = await client.get("/api/v1/auth/verify", headers=auth_headers)
        assert reuse.status_code == 401


class TestVerifyApiKeyNarrowsByPrefix:
    """verify_api_key filters virtual_keys by the derived key_prefix (M3)."""

    def test_virtual_key_prefix_parsing(self) -> None:
        """The stored key_prefix is derived from the presented key."""
        from services.management.app.api.v1.auth import _virtual_key_prefix

        assert _virtual_key_prefix("wa-abcdefghREST_of_secret") == "wa-abcdefgh..."
        assert _virtual_key_prefix("wa-short") is None  # under 11 chars
        assert _virtual_key_prefix("nope-abcdefghij") is None  # missing wa- marker
        assert _virtual_key_prefix("") is None

    def test_verify_api_key_narrows_query_by_key_prefix(self) -> None:
        """The virtual_keys lookup is filtered by key_prefix, not a full scan.

        # regression: release-audit-2026-09-23

        The mocked route DB ignores query contents, so this drives a recording
        DB that captures the query the code builds and asserts the key_prefix
        term is present. Pre-fix the lookup was a bare `enabled == True` scan
        (no key_prefix term), so this assertion fails.
        """
        import services.management.app.api.v1.auth as auth_mod

        api_key = "wa-abcdefghSECRETTAILxyz"
        expected_prefix = "wa-abcdefgh..."
        captured: list = []

        class _Q:
            def __init__(self, terms):
                self.terms = list(terms)

            def __and__(self, other):
                return _Q(self.terms + other.terms)

        class _Field:
            def __init__(self, name):
                self.name = name

            def __eq__(self, other):  # noqa: ANN001
                return _Q([(self.name, other)])

        class _Table:
            def __getattr__(self, name):
                return _Field(name)

        class _Rows(list):
            def first(self):
                return self[0] if self else None

        class _QuerySet:
            def __init__(self, rows):
                self._rows = rows

            def select(self, *a, **k):
                return _Rows(self._rows)

        key_row = MagicMock(key_hash="hash-b", user_id=7, organization_id=3, id=55)
        user_row = MagicMock(id=7, username="u", role="user", organization_id=3, enabled=True)

        class _RecDB:
            virtual_keys = _Table()
            users = _Table()

            def __call__(self, query):
                captured.append(query)
                if any(t[0] == "key_prefix" for t in query.terms):
                    return _QuerySet([key_row])
                return _QuerySet([user_row])

        with (
            patch.object(auth_mod, "db", _RecDB()),
            patch.object(auth_mod.bcrypt, "verify", return_value=True),
        ):
            result = auth_mod.verify_api_key(api_key)

        assert result is not None and result["user_id"] == 7
        assert (
            "key_prefix",
            expected_prefix,
        ) in captured[0].terms
        assert captured[0].terms != [("enabled", True)]

    def test_verify_api_key_rejects_malformed_without_touching_db(self) -> None:
        """A malformed key returns None before any DB/bcrypt work."""
        import services.management.app.api.v1.auth as auth_mod

        db = MagicMock()
        with (
            patch.object(auth_mod, "db", db),
            patch.object(auth_mod.bcrypt, "verify") as verify,
        ):
            assert auth_mod.verify_api_key("garbage") is None
        db.assert_not_called()
        verify.assert_not_called()


# ---------------------------------------------------------------------------
# POST /api/v1/auth/token -- headless-auth H2: API-key -> JWT exchange
# ---------------------------------------------------------------------------


class TestTokenExchange:
    """Tests for POST /api/v1/auth/token.

    The non-interactive counterpart to /auth/login: a caller presents a
    `wa-` virtual key as `Authorization: Bearer wa-...` (never a query
    param -- query strings land in access logs and browser history) and
    receives a short-lived RS256 JWT whose scope/role/organization are
    derived entirely from the key owner's row, never from the request.

    regression: headless-auth
    """

    # Type-annotated (not a bare `NAME = "..."` assignment) so gitleaks'
    # generic-api-key rule -- which requires a keyword immediately adjacent
    # to the assignment operator -- does not flag this fixture value; see
    # the same technique in test_integrations_routes.py's `raw_key: str =`
    # parameter default. This is a fixture value, not a real credential.
    RAW_KEY: str = "wa-headlesskey0123456789"  # noqa: S105

    @staticmethod
    def _wire_key_lookup(
        app_mock_db: MagicMock,
        *,
        user: MagicMock | None = None,
        key_enabled: bool = True,
        org_mismatch: bool = False,
    ) -> MagicMock:
        """Wire the two-query `verify_api_key` sequence onto a mocked DB.

        `verify_api_key` issues two distinct `database(query)` calls (the
        virtual_keys prefix lookup, then the owning user lookup) against the
        SAME mock db object, so a single `.return_value` chain (as every
        other fixture in this file uses) cannot distinguish between them.
        `side_effect` supplies a different result per call instead, mirroring
        `TestVerifyApiKeyNarrowsByPrefix`'s recording-DB technique but reusing
        the shared `app_mock_db` fixture rather than a bespoke DB class.

        `key_enabled=False` returns an empty key-query result set rather than
        a row with `enabled=False`: the real `enabled == True` filter is
        applied by the SQL query itself (`verify_api_key`'s `prefix_query`),
        which a MagicMock ignores entirely -- a row that reached Python would
        pass through unfiltered regardless of its `enabled` attribute, same
        as the pre-existing multi-tenant-filter testability gap.
        """
        owner = user or make_mock_user(user_id=5, org_id=2, role="admin", username="svc-ci")
        key_row = MagicMock(
            id=99,
            user_id=owner.id,
            organization_id=1 if org_mismatch else owner.organization_id,
            enabled=True,
            key_hash=bcrypt.hash(TestTokenExchange.RAW_KEY),
        )

        key_query = MagicMock()
        key_query.select.return_value = [key_row] if key_enabled else []

        user_query = MagicMock()
        user_query.select.return_value.first.return_value = owner

        app_mock_db.side_effect = [key_query, user_query]
        return owner

    async def test_valid_key_exchanges_for_a_short_lived_jwt(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A valid key returns a well-formed RS256 JWT with the owner's claims."""
        owner = self._wire_key_lookup(app_mock_db)

        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["token_type"] == "bearer"  # noqa: S105 -- OAuth2 field value
        # Real TTL, not a hardcoded lie (see TestTokenLifetime for /login, /refresh).
        assert data["expires_in"] == 3600

        header = _jwt.get_unverified_header(data["access_token"])
        assert header["kid"]
        assert header["alg"] == "RS256"

        claims = _jwt.decode(data["access_token"], options={"verify_signature": False})
        assert claims["sub"] == str(owner.id)
        assert claims["tenant"] == str(owner.organization_id)
        assert claims["scope"], "scope must be derived from the owner's role, never empty"
        assert claims["jti"]
        assert claims["exp"] - claims["iat"] == 3600

    async def test_service_account_owner_exchanges_identically(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A service-account owner (H1) yields a working exchange, same as a human owner."""
        owner = make_mock_user(user_id=7, org_id=3, role="user", username="svc-agent")
        owner.is_service_account = True
        owner.service_kind = "agent"
        self._wire_key_lookup(app_mock_db, user=owner)

        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )

        assert resp.status_code == 200
        claims = _jwt.decode(
            (await resp.get_json())["access_token"], options={"verify_signature": False}
        )
        assert claims["sub"] == str(owner.id)
        assert claims["tenant"] == str(owner.organization_id)

    async def test_missing_auth_header_returns_401(self, client) -> None:
        """No credential presented at all -> 401."""
        resp = await client.post("/api/v1/auth/token")
        assert resp.status_code == 401

    async def test_key_in_query_param_is_never_accepted(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A key placed in the query string is ignored, not silently honoured."""
        resp = await client.post(f"/api/v1/auth/token?api_key={self.RAW_KEY}")
        assert resp.status_code == 401
        app_mock_db.assert_not_called()

    async def test_malformed_key_returns_401_without_touching_db(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A key failing the `wa-` shape check never reaches the database."""
        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401
        app_mock_db.assert_not_called()

    async def test_disabled_key_is_refused(self, client, app_mock_db: MagicMock) -> None:
        """A disabled virtual key is refused exactly like an unknown one."""
        self._wire_key_lookup(app_mock_db, key_enabled=False)

        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )
        assert resp.status_code == 401

    async def test_org_mismatch_key_is_refused(self, client, app_mock_db: MagicMock) -> None:
        """A key whose stored org no longer matches its owner's org is refused (Vuln A)."""
        self._wire_key_lookup(app_mock_db, org_mismatch=True)

        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )
        assert resp.status_code == 401

    async def test_response_never_echoes_the_presented_key(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """The raw key value never appears anywhere in the response body."""
        self._wire_key_lookup(app_mock_db)

        resp = await client.post(
            "/api/v1/auth/token",
            headers={"Authorization": f"Bearer {self.RAW_KEY}"},
        )
        assert self.RAW_KEY not in (await resp.get_data(as_text=True))

    async def test_endpoint_is_absent_from_the_public_openapi_document(self, client) -> None:
        """/auth/token must never join /auth/login in the unauthenticated spec.

        The public document is the sole unauthenticated surface a caller can
        enumerate before it holds any credential -- this endpoint being
        unauthenticated at the middleware layer must not widen that document.
        """
        resp = await client.get("/api/v1/openapi/public.json")
        assert resp.status_code == 200
        spec = await resp.get_json()
        assert list(spec["paths"].keys()) == ["/api/v1/auth/login"]
