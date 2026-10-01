"""Tests for the ``shared.auth.penguin_auth`` factory/conversion helpers.

Complements ``test_penguin_auth_keystore_guard.py`` (keystore guard +
rotation) and ``tests/unit/proxy/test_jwks_verifier.py`` (JWKS mechanics):
this file covers the OIDC relying-party factories, the JWKS-backed verify
path's exception translation, ``build_rbac_enforcer``, the UserContext <->
Claims conversion helpers (both directions, both the dict and non-dict
shapes), and the synchronous keystore-backed ``verify_token``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from penguin_aaa.authn import Claims, OIDCRelyingParty
from penguin_aaa.authz.rbac import RBACEnforcer

from shared.auth.jwks_verifier import JWKSVerificationError, JWKSVerifier
from shared.auth.penguin_auth import (
    JWKSOIDCRelyingParty,
    build_rbac_enforcer,
    claims_dict_to_user_context,
    claims_to_user_context,
    create_jwks_oidc_rp,
    create_oidc_provider,
    create_oidc_rp,
    issue_token,
    user_context_to_claims,
    user_context_to_claims_dict,
    verify_token,
    verify_token_via_jwks,
)
from shared.auth.rbac import AuthenticationError, Permission, Role, UserContext


def _claims(**overrides: object) -> Claims:
    """Build a minimal, valid Claims object, overridable per test."""
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "sub": "42",
        "iss": "https://waddleai.localhost.local",
        "aud": ["waddleai-api"],
        "iat": now,
        "exp": now + timedelta(hours=1),
        "scope": ["proxy:use"],
        "roles": ["user"],
        "tenant": "7",
        "teams": [],
        "ext": {},
    }
    defaults.update(overrides)
    return Claims(**defaults)


def _user_context(**overrides: object) -> UserContext:
    """Build a minimal UserContext, overridable per test."""
    defaults: dict[str, Any] = {
        "user_id": 42,
        "username": "alice",
        "role": Role.USER,
        "organization_id": 7,
        "managed_orgs": [],
        "permissions": {Permission.PROXY_USE},
        "api_key_id": None,
    }
    defaults.update(overrides)
    return UserContext(**defaults)  # type: ignore[arg-type]


class TestCreateOidcRp:
    """``create_oidc_rp()`` builds an OIDCRelyingParty from env configuration."""

    def test_builds_from_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no env overrides, the relying party uses the documented defaults."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OIDC_REDIRECT_URL", raising=False)

        rp = create_oidc_rp()

        assert isinstance(rp, OIDCRelyingParty)

    def test_builds_from_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit env vars are threaded through to the OIDCRPConfig."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://issuer.example")
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "s3cret")  # noqa: S105 -- fixture value
        monkeypatch.setenv("OIDC_REDIRECT_URL", "https://issuer.example/cb")

        rp = create_oidc_rp()

        assert isinstance(rp, OIDCRelyingParty)


class TestVerifyTokenViaJwks:
    """``verify_token_via_jwks`` translates JWKS failures into ``AuthenticationError``."""

    def test_successful_verification_returns_user_context(self) -> None:
        """A verifier that resolves claims successfully yields a UserContext."""
        verifier = Mock(spec=JWKSVerifier)
        verifier.verify_token.return_value = _claims()

        result = verify_token_via_jwks("some.jwt.token", verifier)

        assert isinstance(result, UserContext)
        assert result.user_id == 42
        assert result.organization_id == 7

    def test_jwks_verification_error_becomes_authentication_error(self) -> None:
        """A JWKSVerificationError is never allowed to leak past this boundary unwrapped."""
        verifier = Mock(spec=JWKSVerifier)
        verifier.verify_token.side_effect = JWKSVerificationError("kid unknown")

        with pytest.raises(AuthenticationError, match="kid unknown"):
            verify_token_via_jwks("some.jwt.token", verifier)


class TestJwksOidcRelyingParty:
    """``JWKSOIDCRelyingParty`` wraps verify_token_via_jwks for the ASGI middleware."""

    async def test_verify_token_returns_claims_dict_on_success(self) -> None:
        """A successful verification returns the plain claims-dict shape."""
        verifier = Mock(spec=JWKSVerifier)
        verifier.verify_token.return_value = _claims()
        rp = JWKSOIDCRelyingParty(verifier)

        claims_dict = await rp.verify_token("some.jwt.token")

        assert claims_dict["sub"] == "42"
        assert claims_dict["tenant"] == "7"

    async def test_verify_token_propagates_authentication_error(self) -> None:
        """A JWKS failure surfaces as AuthenticationError to the middleware, not silently."""
        verifier = Mock(spec=JWKSVerifier)
        verifier.verify_token.side_effect = JWKSVerificationError("unreachable")
        rp = JWKSOIDCRelyingParty(verifier)

        with pytest.raises(AuthenticationError):
            await rp.verify_token("some.jwt.token")


class TestCreateJwksOidcRp:
    """``create_jwks_oidc_rp`` default-builds or accepts an injected verifier."""

    def test_default_builds_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no verifier argument, one is built from env via create_jwks_verifier."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://waddleai.test")

        rp = create_jwks_oidc_rp()

        assert isinstance(rp, JWKSOIDCRelyingParty)
        assert isinstance(rp._verifier, JWKSVerifier)

    def test_injected_verifier_is_used_as_is(self) -> None:
        """An explicitly passed verifier is bound directly, no env lookup involved."""
        injected = Mock(spec=JWKSVerifier)

        rp = create_jwks_oidc_rp(injected)

        assert rp._verifier is injected


class TestBuildRbacEnforcer:
    """``build_rbac_enforcer`` registers every WaddleAI role as a penguin-aaa Role."""

    def test_registers_every_role_with_its_scopes(self) -> None:
        """Each Role.value is registered with the scopes from ROLE_PERMISSIONS."""
        enforcer = build_rbac_enforcer()

        assert isinstance(enforcer, RBACEnforcer)
        assert enforcer.has_scope(Role.ADMIN.value, Permission.SYSTEM_CONFIG.value)
        assert not enforcer.has_scope(Role.USER.value, Permission.SYSTEM_CONFIG.value)


class TestUserContextToClaims:
    """``user_context_to_claims`` handles both set- and list-shaped permissions."""

    def test_set_shaped_permissions(self) -> None:
        """A `set[Permission]` permissions field extracts each member's `.value`."""
        uc = _user_context(permissions={Permission.PROXY_USE, Permission.USER_READ})

        claims = user_context_to_claims(uc)

        assert set(claims.scope) == {"proxy:use", "user:read"}
        assert claims.tenant == "7"
        assert claims.roles == ["user"]

    def test_list_shaped_permissions(self) -> None:
        """A list-shaped permissions field (already-stringified scopes) is used as-is."""
        uc = _user_context(permissions=["proxy:use", "user:read"])

        claims = user_context_to_claims(uc)

        assert set(claims.scope) == {"proxy:use", "user:read"}

    def test_managed_orgs_become_teams(self) -> None:
        """managed_orgs is stringified into the `teams` claim."""
        uc = _user_context(managed_orgs=[1, 2, 3])

        claims = user_context_to_claims(uc)

        assert claims.teams == ["1", "2", "3"]


class TestClaimsToUserContext:
    """``claims_to_user_context`` is the inverse conversion, including its fallbacks."""

    def test_round_trips_a_well_formed_claims_object(self) -> None:
        """A Claims object built from a real UserContext round-trips cleanly."""
        uc = _user_context(api_key_id=99)
        claims = user_context_to_claims(uc)

        rebuilt = claims_to_user_context(claims)

        assert rebuilt.user_id == 42
        assert rebuilt.role == Role.USER
        assert rebuilt.organization_id == 7
        assert rebuilt.api_key_id == 99

    def test_unknown_role_falls_back_to_user(self) -> None:
        """A role name Role() doesn't recognise falls back to Role.USER, not a crash."""
        claims = _claims(roles=["totally-not-a-role"])

        rebuilt = claims_to_user_context(claims)

        assert rebuilt.role == Role.USER

    def test_empty_roles_defaults_to_user(self) -> None:
        """An empty roles list also falls back to Role.USER."""
        claims = _claims(roles=[])

        rebuilt = claims_to_user_context(claims)

        assert rebuilt.role == Role.USER

    def test_non_digit_sub_yields_zero_user_id(self) -> None:
        """A non-numeric `sub` (e.g. a SPIFFE ID) maps to user_id 0 rather than raising."""
        claims = _claims(sub="spiffe://penguintech.io/prod/some-service")

        rebuilt = claims_to_user_context(claims)

        assert rebuilt.user_id == 0

    def test_non_digit_api_key_id_in_ext_is_ignored(self) -> None:
        """A non-numeric `api_key_id` in `ext` is treated as absent, not a crash."""
        claims = _claims(ext={"api_key_id": "not-a-number"})

        rebuilt = claims_to_user_context(claims)

        assert rebuilt.api_key_id is None


class TestUserContextToClaimsDict:
    """``user_context_to_claims_dict`` for both permission-field shapes."""

    def test_set_shaped_permissions(self) -> None:
        """Set-shaped permissions are stringified via `.value`."""
        uc = _user_context(permissions={Permission.PROXY_USE})

        d = user_context_to_claims_dict(uc)

        assert d["scope"] == ["proxy:use"]
        assert d["sub"] == "42"
        assert d["tenant"] == "7"

    def test_list_shaped_permissions(self) -> None:
        """List-shaped permissions also go through the `.value`-or-str branch."""
        uc = _user_context(permissions=[Permission.PROXY_USE])

        d = user_context_to_claims_dict(uc)

        assert d["scope"] == ["proxy:use"]


class TestClaimsDictToUserContext:
    """``claims_dict_to_user_context`` rebuilds a UserContext from a plain dict."""

    def test_round_trips_user_context_to_claims_dict(self) -> None:
        """The dict round-trip (used by AuditMiddleware/get_current_user) is lossless."""
        uc = _user_context(api_key_id=5)

        rebuilt = claims_dict_to_user_context(user_context_to_claims_dict(uc))

        assert rebuilt.user_id == 42
        assert rebuilt.role == Role.USER
        assert rebuilt.api_key_id == 5

    def test_missing_keys_use_safe_defaults(self) -> None:
        """A sparse dict (missing roles/tenant/teams/sub) still produces a usable context."""
        rebuilt = claims_dict_to_user_context({})

        assert rebuilt.user_id == 0
        assert rebuilt.role == Role.USER
        assert rebuilt.organization_id == 0
        assert rebuilt.managed_orgs == []
        assert rebuilt.api_key_id is None

    def test_unknown_role_in_dict_falls_back_to_user(self) -> None:
        """An unrecognised role string in the dict falls back to Role.USER."""
        rebuilt = claims_dict_to_user_context({"roles": ["nonsense"], "sub": "1"})

        assert rebuilt.role == Role.USER

    def test_non_digit_tenant_and_teams_are_dropped(self) -> None:
        """Non-numeric tenant/teams entries are treated as absent rather than raising."""
        rebuilt = claims_dict_to_user_context(
            {"sub": "1", "tenant": "not-a-number", "teams": ["also-not", "3"]}
        )

        assert rebuilt.organization_id == 0
        assert rebuilt.managed_orgs == [3]


class TestSyncVerifyToken:
    """The synchronous, process-keystore-backed ``verify_token``/``issue_token`` pair."""

    def test_issue_then_verify_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A token issued by this process's own provider verifies successfully."""
        monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
        monkeypatch.setenv("FLASK_ENV", "testing")
        provider = create_oidc_provider()
        uc = _user_context()

        token = issue_token(uc, provider)
        rebuilt = verify_token(token, provider)

        assert rebuilt.user_id == 42
        assert rebuilt.organization_id == 7

    def test_expired_token_raises_authentication_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired token is rejected with AuthenticationError, not a raw jwt exception."""
        monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
        monkeypatch.setenv("FLASK_ENV", "testing")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://waddleai.localhost.local")
        monkeypatch.setenv("OIDC_CLIENT_ID", "waddleai-api")
        provider = create_oidc_provider()

        import jwt as _jwt

        private_key, kid = provider._keystore.get_signing_key()
        now = datetime.now(UTC)
        payload = {
            "sub": "42",
            "iss": "https://waddleai.localhost.local",
            "aud": ["waddleai-api"],
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
            "scope": [],
            "roles": ["user"],
            "tenant": "7",
            "teams": [],
        }
        token = _jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})

        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(token, provider)

    def test_invalid_signature_raises_authentication_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token signed by a different keypair is rejected with AuthenticationError."""
        monkeypatch.delenv("SIGNING_KEY_FILE", raising=False)
        monkeypatch.setenv("FLASK_ENV", "testing")
        provider = create_oidc_provider()
        other_provider = create_oidc_provider()
        uc = _user_context()

        token = issue_token(uc, other_provider)

        with pytest.raises(AuthenticationError, match="Invalid token"):
            verify_token(token, provider)
