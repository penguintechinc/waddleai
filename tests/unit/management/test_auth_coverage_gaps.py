"""Coverage-gap tests for ``services.management.app.api.v1.auth``.

Targets branches ``test_auth_routes.py``/``test_auth_cookie.py``/
``test_scope_authz.py`` leave unexercised: the cookie-secure env override, the
uninitialized-DB guard, the role-fallback helpers, the TTL clamp/env-fallback
paths, the second-pass JWT decode failure, the revoked-jti rejection in
``verify_token``, ``verify_api_key``'s bcrypt-mismatch and disabled-owner loop
branches, ``require_auth``'s non-Bearer-header/invalid-cookie/API-key/sync-
handler branches, and ``require_scope``'s no-``g.user``/sync-handler branches.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from passlib.hash import bcrypt
from quart import Quart, g, jsonify

import services.management.app.api.v1.auth as auth_mod
from services.management.app.services.rate_limiter import reset_auth_rate_limiter
from services.management.app.services.token_denylist import RevocationResult, reset_token_denylist
from shared.auth.rbac import Permission, Role
from tests.unit.management.route_conftest import make_mock_user


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """Reset the process-wide rate limiter and denylist singletons around every test."""
    reset_auth_rate_limiter()
    reset_token_denylist()
    yield
    reset_auth_rate_limiter()
    reset_token_denylist()


# ---------------------------------------------------------------------------
# Small pure helpers, exercised directly (no HTTP layer needed).
# ---------------------------------------------------------------------------


class TestAccessCookieSecure:
    """``_access_cookie_secure`` honours an explicit ``AUTH_COOKIE_SECURE`` override."""

    def test_explicit_false_disables_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_COOKIE_SECURE=false turns off the Secure attribute."""
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
        assert auth_mod._access_cookie_secure() is False

    def test_explicit_true_keeps_secure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit truthy value is honoured too, not just the unset default."""
        monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
        assert auth_mod._access_cookie_secure() is True


class TestDbHelper:
    """``_db()`` narrows ``extensions.db`` away from ``None``."""

    def test_raises_runtime_error_when_uninitialized(self) -> None:
        """A None db (pre-startup) is a hard failure, not a silent None return."""
        with (
            patch.object(auth_mod, "db", None),
            pytest.raises(RuntimeError, match="not initialized"),
        ):
            auth_mod._db()


class TestScopesForRole:
    """``_scopes_for_role`` falls back to Role.USER's bundle for an unknown role name."""

    def test_unknown_role_falls_back_to_user_bundle(self) -> None:
        """A role string Role() doesn't recognise still returns a (narrow) scope list."""
        from shared.auth.rbac import ROLE_PERMISSIONS

        expected = sorted(p.value for p in ROLE_PERMISSIONS.get(Role.USER, set()))
        assert sorted(auth_mod._scopes_for_role("totally-bogus-role")) == expected


class TestClampTokenTtlHours:
    """``_clamp_token_ttl_hours`` enforces the [1h, 24h] house-policy range."""

    def test_below_floor_clamps_up(self) -> None:
        """A requested lifetime below the floor is raised to the floor."""
        assert auth_mod._clamp_token_ttl_hours(0) == 1

    def test_within_range_is_unchanged(self) -> None:
        """A requested lifetime already inside the range passes through unchanged."""
        assert auth_mod._clamp_token_ttl_hours(4) == 4

    def test_above_ceiling_clamps_down(self) -> None:
        """A requested lifetime above the ceiling is capped at the ceiling."""
        assert auth_mod._clamp_token_ttl_hours(48) == 24


class TestDefaultTokenTtlHours:
    """``_default_token_ttl_hours`` reads/validates ``TOKEN_TTL_HOURS``."""

    def test_non_integer_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-numeric TOKEN_TTL_HOURS warns and falls back to the 1h default."""
        monkeypatch.setenv("TOKEN_TTL_HOURS", "not-a-number")
        with caplog.at_level("WARNING"):
            assert auth_mod._default_token_ttl_hours() == 1
        assert any("is not an integer" in r.message for r in caplog.records)

    def test_out_of_range_env_is_clamped_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TOKEN_TTL_HOURS above the ceiling is clamped, with a warning logged."""
        monkeypatch.setenv("TOKEN_TTL_HOURS", "48")
        with caplog.at_level("WARNING"):
            assert auth_mod._default_token_ttl_hours() == 24
        assert any("clamped to" in r.message for r in caplog.records)

    def test_in_range_env_is_used_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TOKEN_TTL_HOURS already inside the range is used as-is, no warning."""
        monkeypatch.setenv("TOKEN_TTL_HOURS", "2")
        with caplog.at_level("WARNING"):
            assert auth_mod._default_token_ttl_hours() == 2
        assert not any("clamped to" in r.message for r in caplog.records)


class TestIssueAccessTokenUnknownRole:
    """``issue_access_token`` falls back to Role.USER for an unrecognised role name."""

    def test_unknown_role_issues_a_user_scoped_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bogus role string does not crash issuance -- it degrades to Role.USER's scopes."""
        monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
        monkeypatch.setenv("FLASK_ENV", "testing")

        issued = auth_mod.issue_access_token(
            user_id=1, username="bob", role="not-a-real-role", organization_id=1
        )

        import jwt as _jwt

        claims = _jwt.decode(issued.access_token, options={"verify_signature": False})
        assert claims["roles"] == ["user"]


class TestCreateTokenWrapper:
    """``create_token`` is a thin wrapper returning just the access-token string."""

    def test_returns_only_the_token_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """create_token returns a plain str, not the IssuedToken dataclass."""
        monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
        monkeypatch.setenv("FLASK_ENV", "testing")

        token = auth_mod.create_token(user_id=1, username="bob", role="user", organization_id=1)

        assert isinstance(token, str)
        assert token.count(".") == 2  # looks like a JWT


def _mint_token_for_auth_mod(monkeypatch: pytest.MonkeyPatch, **uc_overrides: object) -> str:
    """Mint a token with a provider explicitly wired as ``auth_mod``'s own.

    ``auth_mod._get_oidc_provider()`` is ``@lru_cache``d, and separately,
    ``route_conftest.make_token()`` mints against its *own*, independently
    cached provider -- the two are not guaranteed to share a keypair (see
    ``test_auth_routes.py``'s ``test_logout_does_not_revoke_unrelated_tokens``
    docstring for the same gotcha). Building the provider here and patching
    it directly into ``auth_mod._get_oidc_provider`` sidesteps both caches
    so the minted token is always verifiable by the code under test.
    """
    from shared.auth.penguin_auth import create_oidc_provider, issue_token
    from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext

    monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    provider = create_oidc_provider()
    monkeypatch.setattr(auth_mod, "_get_oidc_provider", lambda: provider)

    defaults: dict[str, object] = {
        "user_id": 1,
        "username": "admin",
        "role": Role.ADMIN,
        "organization_id": 1,
        "managed_orgs": [],
        "permissions": ROLE_PERMISSIONS.get(Role.ADMIN, set()),
    }
    defaults.update(uc_overrides)
    return issue_token(UserContext(**defaults), provider)  # type: ignore[arg-type]


class TestDecodeTokenSecondPassFailure:
    """``_decode_token``'s unsigned re-decode pass can independently fail closed."""

    def test_second_decode_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure in the (jti/exp-only) unsigned re-decode pass returns None, not a crash.

        ``auth_mod._jwt`` and ``shared.auth.penguin_auth``'s own ``_jwt`` are
        the *same* imported ``jwt`` module object, so this patches only the
        unsigned, ``verify_signature=False`` call -- the first (signature-
        verifying) decode inside ``_aaa_verify_token`` is left untouched and
        still succeeds.
        """
        token = _mint_token_for_auth_mod(monkeypatch)

        real_decode = auth_mod._jwt.decode

        def _flaky_decode(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("options", {}).get("verify_signature") is False:
                raise ValueError("boom: simulated second-pass failure")
            return real_decode(*args, **kwargs)

        monkeypatch.setattr(auth_mod._jwt, "decode", _flaky_decode)

        assert auth_mod._decode_token(token) is None


class TestVerifyTokenFunction:
    """``verify_token`` (module-level) combines decode + the revocation denylist check."""

    def test_garbage_token_returns_none(self) -> None:
        """An undecodable token returns None rather than raising."""
        assert auth_mod.verify_token("not.a.jwt") is None

    def test_revoked_jti_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A structurally valid token whose jti is on the denylist is still rejected."""
        token = _mint_token_for_auth_mod(monkeypatch)

        fake_denylist = MagicMock()
        fake_denylist.is_revoked.return_value = True
        with patch.object(auth_mod, "get_token_denylist", return_value=fake_denylist):
            assert auth_mod.verify_token(token) is None

    def test_valid_non_revoked_token_returns_the_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A structurally valid, non-revoked token returns its decoded payload dict."""
        token = _mint_token_for_auth_mod(monkeypatch)

        fake_denylist = MagicMock()
        fake_denylist.is_revoked.return_value = False
        with patch.object(auth_mod, "get_token_denylist", return_value=fake_denylist):
            payload = auth_mod.verify_token(token)

        assert payload is not None
        assert payload["username"] == "admin"


# ---------------------------------------------------------------------------
# verify_api_key loop branches: bcrypt mismatch, and a matched-but-disabled owner.
# ---------------------------------------------------------------------------


class _RecordingTable:
    """Minimal stand-in for a penguin-dal table proxy, attribute access only."""

    def __getattr__(self, name: str) -> MagicMock:
        return MagicMock(name=name)


class _Rows(list):
    """A list of rows that also supports penguin-dal's ``.first()`` accessor."""

    def first(self) -> object | None:
        """Return the first row, or None when empty."""
        return self[0] if self else None


class _QuerySet:
    """Wraps a fixed row list so ``database(query).select()`` returns it."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def select(self, *args: object, **kwargs: object) -> _Rows:
        return _Rows(self._rows)


class TestVerifyApiKeyLoopBranches:
    """``verify_api_key``'s per-row loop: bcrypt mismatch, and disabled owner."""

    RAW_KEY: str = "wa-headlesskey0123456789"  # noqa: S105 -- fixture value, not a real credential

    def test_bcrypt_mismatch_falls_through_to_none(self) -> None:
        """A key row whose stored hash doesn't match the presented key is skipped, not matched."""
        key_row = MagicMock(
            id=1, user_id=1, organization_id=1, key_hash=bcrypt.hash("a-different-secret")
        )
        user_row = make_mock_user(user_id=1, org_id=1)

        responses = iter([_QuerySet([key_row]), _QuerySet([user_row])])
        fake_db = MagicMock()
        fake_db.virtual_keys = _RecordingTable()
        fake_db.users = _RecordingTable()
        fake_db.side_effect = lambda query: next(responses)  # noqa: ARG005

        with patch.object(auth_mod, "db", fake_db):
            assert auth_mod.verify_api_key(self.RAW_KEY) is None

    def test_matched_but_disabled_owner_returns_none(self) -> None:
        """A key that bcrypt-matches but whose owner is disabled is still refused."""
        key_row = MagicMock(id=1, user_id=1, organization_id=1, key_hash=bcrypt.hash(self.RAW_KEY))
        user_row = make_mock_user(user_id=1, org_id=1, enabled=False)

        responses = iter([_QuerySet([key_row]), _QuerySet([user_row])])
        fake_db = MagicMock()
        fake_db.virtual_keys = _RecordingTable()
        fake_db.users = _RecordingTable()
        fake_db.side_effect = lambda query: next(responses)  # noqa: ARG005

        with patch.object(auth_mod, "db", fake_db):
            assert auth_mod.verify_api_key(self.RAW_KEY) is None


# ---------------------------------------------------------------------------
# require_auth: header-shape, cookie-invalid, API-key-via-Bearer, sync handler.
# ---------------------------------------------------------------------------


class TestRequireAuthBranches:
    """``require_auth`` branches not already covered by the route-level test modules."""

    async def test_header_present_but_not_bearer_is_401(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """An Authorization header present but not `Bearer `-prefixed is refused outright."""
        resp = await client.get(
            "/api/v1/auth/verify", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Invalid or expired token"

    async def test_invalid_cookie_with_csrf_header_is_401(self, client) -> None:
        """An unrecognised cookie value (CSRF gate passed) still fails JWT auth -> 401."""
        client.set_cookie("localhost", auth_mod._ACCESS_COOKIE_NAME, "not-a-real-token")
        resp = await client.get(
            "/api/v1/auth/verify", headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Invalid or expired token"

    async def test_bearer_api_key_authenticates_and_has_no_jti_to_revoke(
        self, client, app_mock_db: MagicMock
    ) -> None:
        """A `wa-` API key presented as a Bearer token authenticates via the API-key branch.

        Exercised through POST /auth/refresh specifically because its
        handler also calls ``_revoke_presented_token``, which must no-op
        (rather than crash) when ``g.user`` carries no ``jti``/``exp`` -- the
        API-key path never sets either.
        """
        owner = make_mock_user(user_id=5, org_id=2, role="admin", username="svc-ci")
        key_row = MagicMock(
            id=99,
            user_id=owner.id,
            organization_id=owner.organization_id,
            enabled=True,
            key_hash=bcrypt.hash("wa-headlesskey0123456789"),
        )
        key_query = MagicMock()
        key_query.select.return_value = [key_row]
        user_query = MagicMock()
        user_query.select.return_value.first.return_value = owner
        app_mock_db.side_effect = [key_query, user_query]

        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": "Bearer wa-headlesskey0123456789"},
        )

        assert resp.status_code == 200
        data = await resp.get_json()
        assert data["access_token"]


class TestRequireAuthAndRequireScopeSyncHandlers:
    """Both decorators support a synchronous (non-async-def) wrapped handler."""

    async def test_require_auth_invokes_a_sync_handler(self, auth_headers: dict) -> None:
        """A sync handler wrapped by require_auth is still invoked (the non-coroutine branch)."""
        probe_app = Quart("require_auth_sync_probe")

        @probe_app.route("/probe", methods=["GET"])
        @auth_mod.require_auth
        def probe():  # noqa: ANN202 -- deliberately sync, that's what's under test
            return jsonify({"user_id": g.user.get("user_id")})

        async with probe_app.test_client() as c:
            resp = await c.get("/probe", headers=auth_headers)

        assert resp.status_code == 200

    async def test_require_scope_invokes_a_sync_handler(self, auth_headers: dict) -> None:
        """A sync handler wrapped by require_scope is still invoked (the non-coroutine branch)."""
        probe_app = Quart("require_scope_sync_probe")

        @probe_app.route("/probe", methods=["GET"])
        @auth_mod.require_auth
        @auth_mod.require_scope(Permission.ORG_CREATE)
        def probe():  # noqa: ANN202 -- deliberately sync, that's what's under test
            return jsonify({"ok": True})

        async with probe_app.test_client() as c:
            resp = await c.get("/probe", headers=auth_headers)

        assert resp.status_code == 200


class TestRequireScopeWithoutAuth:
    """``require_scope`` used (unusually) without ``require_auth`` ahead of it."""

    async def test_no_g_user_at_all_is_401(self) -> None:
        """With no require_auth to populate g.user, require_scope's own guard fires."""
        probe_app = Quart("require_scope_no_auth_probe")

        @probe_app.route("/probe", methods=["GET"])
        @auth_mod.require_scope(Permission.ORG_CREATE)
        async def probe():
            return jsonify({"ok": True})

        async with probe_app.test_client() as c:
            resp = await c.get("/probe")

        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Authentication required"


# ---------------------------------------------------------------------------
# Logout / refresh revocation-result branches: jti-less credential, durable result.
# ---------------------------------------------------------------------------


class TestLogoutAndRefreshRevocationBranches:
    """The ``not result.durable`` warning branch, and the no-jti early return."""

    async def test_logout_with_no_jti_logs_and_still_reports_success(
        self, client, app_mock_db: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Logging out on an API-key-authenticated credential has nothing to revoke."""
        owner = make_mock_user(user_id=5, org_id=2, role="admin", username="svc-ci")
        key_row = MagicMock(
            id=99,
            user_id=owner.id,
            organization_id=owner.organization_id,
            enabled=True,
            key_hash=bcrypt.hash("wa-headlesskey0123456789"),
        )
        key_query = MagicMock()
        key_query.select.return_value = [key_row]
        user_query = MagicMock()
        user_query.select.return_value.first.return_value = owner
        app_mock_db.side_effect = [key_query, user_query]

        with caplog.at_level("WARNING"):
            resp = await client.post(
                "/api/v1/auth/logout",
                headers={
                    "Authorization": "Bearer wa-headlesskey0123456789",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

        assert resp.status_code == 200
        assert (await resp.get_json())["message"] == "Logged out successfully"
        assert any("nothing was revoked" in r.message for r in caplog.records)

    async def test_logout_durable_revocation_skips_the_non_durable_warning(
        self, client, auth_headers: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A durable (shared-cache) revocation result does not log the per-process warning."""
        durable_denylist = MagicMock()
        durable_denylist.is_revoked.return_value = False
        durable_denylist.revoke.return_value = RevocationResult(revoked=True, durable=True)

        with (
            patch.object(auth_mod, "get_token_denylist", return_value=durable_denylist),
            caplog.at_level("WARNING"),
        ):
            resp = await client.post("/api/v1/auth/logout", headers=auth_headers)

        assert resp.status_code == 200
        assert not any("this process only" in r.message for r in caplog.records)

    async def test_refresh_durable_revocation_skips_the_non_durable_warning(
        self, client, auth_headers: dict, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same durable-result branch, exercised via /auth/refresh's own revoke call."""
        durable_denylist = MagicMock()
        durable_denylist.is_revoked.return_value = False
        durable_denylist.revoke.return_value = RevocationResult(revoked=True, durable=True)

        with (
            patch.object(auth_mod, "get_token_denylist", return_value=durable_denylist),
            caplog.at_level("WARNING"),
        ):
            resp = await client.post("/api/v1/auth/refresh", headers=auth_headers)

        assert resp.status_code == 200
        assert not any("this process only" in r.message for r in caplog.records)
