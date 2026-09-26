"""Tests for the public OIDC discovery/JWKS blueprint and admin key rotation.

# regression: headless-auth (H3) -- WaddleAI management must publish its
RS256 public signing keys so any validator (including the proxy's future
relying party, H4) can verify a token without the shared ``SIGNING_KEY_FILE``
mounted locally. Covers: unauthenticated JWKS + discovery reachability and
shape, a signed token's ``kid`` round-tripping into the published JWKS,
rotation overlap (old + new ``kid`` both published mid-rotation), and the
rotate endpoint's admin-only, ``@require_auth``-gated access.
"""

from __future__ import annotations

import os

import jwt as _jwt
import pytest

from services.management.app.api.v1 import auth as auth_mod

ROTATE_PATH = "/api/v1/system/signing-key/rotate"


@pytest.fixture
def _restore_keystore():
    """Snapshot/restore the shared test provider's keystore around a rotating test.

    ``auth._get_oidc_provider()`` is a process-wide ``lru_cache``d singleton
    shared by every test module under ``tests/unit/management/`` (see
    conftest.py's ``_test_oidc_provider``) -- a test that rotates it without
    restoring would change which ``kid`` signs subsequent tokens for every
    OTHER test file in the same pytest session, not just this one.
    """
    provider = auth_mod._get_oidc_provider()
    keystore = provider._keystore
    saved = list(keystore._keys)
    yield provider
    keystore._keys[:] = saved


class TestManagementOptsIntoStrictKeystoreGuard:
    """create_app() must opt this service into the hard-fail keystore guard."""

    def test_strict_keystore_env_var_set(self, flask_app) -> None:
        """OIDC_REQUIRE_DURABLE_KEYSTORE=true after create_app() has run.

        ``flask_app`` (module fixture) already invoked ``create_app()``, so
        this asserts the process-wide side effect it must have produced --
        proxy's own boot path never sets this var, which is what keeps
        proxy's (out-of-scope, still self-contained) behaviour unchanged.
        """
        assert os.environ.get("OIDC_REQUIRE_DURABLE_KEYSTORE") == "true"


class TestJwksEndpoint:
    """GET /.well-known/jwks.json -- unauthenticated, public keys only."""

    async def test_reachable_without_auth(self, client) -> None:
        """No Authorization header is required."""
        resp = await client.get("/.well-known/jwks.json")
        assert resp.status_code == 200

    async def test_body_is_a_valid_jwks(self, client) -> None:
        """At least one RSA public key, with kid/kty/n/e and no private fields."""
        resp = await client.get("/.well-known/jwks.json")
        body = await resp.get_json()

        assert "keys" in body
        assert len(body["keys"]) >= 1

        key = body["keys"][0]
        assert key["kty"] == "RSA"
        assert "kid" in key
        assert "n" in key
        assert "e" in key
        for private_field in ("d", "p", "q", "dp", "dq", "qi"):
            assert private_field not in key

    async def test_cache_control_is_short_and_public(self, client) -> None:
        """Cache-Control carries a bounded max-age, not no-store/private."""
        resp = await client.get("/.well-known/jwks.json")
        cache_control = resp.headers.get("Cache-Control", "")
        assert "max-age" in cache_control
        assert "public" in cache_control


class TestDiscoveryEndpoint:
    """GET /.well-known/openid-configuration -- unauthenticated, correct URLs."""

    async def test_reachable_without_auth(self, client) -> None:
        """No Authorization header is required."""
        resp = await client.get("/.well-known/openid-configuration")
        assert resp.status_code == 200

    async def test_issuer_and_jwks_uri_are_consistent(self, client) -> None:
        """jwks_uri and the OAuth2 endpoints all resolve under the discovery issuer."""
        resp = await client.get("/.well-known/openid-configuration")
        body = await resp.get_json()

        assert body["issuer"]
        assert body["jwks_uri"] == f"{body['issuer']}/.well-known/jwks.json"
        assert body["authorization_endpoint"].startswith(body["issuer"])
        assert body["token_endpoint"].startswith(body["issuer"])
        assert body["id_token_signing_alg_values_supported"] == ["RS256"]


class TestTokenKidRoundTrip:
    """A token signed by this process's provider carries a kid published in its own JWKS."""

    async def test_admin_token_kid_is_in_published_jwks(self, client, admin_token: str) -> None:
        """The signing kid embedded in the token header round-trips into JWKS."""
        header = _jwt.get_unverified_header(admin_token)
        assert "kid" in header

        resp = await client.get("/.well-known/jwks.json")
        body = await resp.get_json()
        published_kids = {k["kid"] for k in body["keys"]}

        assert header["kid"] in published_kids


class TestRotationOverlap:
    """After rotate_key(), JWKS carries both the retiring and the new kid."""

    async def test_rotation_overlap_visible_in_jwks(
        self, client, auth_headers: dict[str, str], _restore_keystore
    ) -> None:
        """A validator mid-rotation still finds the pre-rotation kid published."""
        provider = _restore_keystore
        _, old_kid = provider._keystore.get_signing_key()

        resp = await client.post(ROTATE_PATH, headers=auth_headers)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["rotated"] is True
        new_kid = body["active_kid"]
        assert new_kid != old_kid

        jwks_resp = await client.get("/.well-known/jwks.json")
        jwks_body = await jwks_resp.get_json()
        published_kids = {k["kid"] for k in jwks_body["keys"]}

        assert old_kid in published_kids
        assert new_kid in published_kids


class TestRotationAuthzGate:
    """POST /api/v1/system/signing-key/rotate is @require_auth + admin-scope gated."""

    async def test_no_auth_header_refused(self, client) -> None:
        """No credential at all -> 401, never a pass-through."""
        resp = await client.post(ROTATE_PATH)
        assert resp.status_code == 401

    async def test_plain_user_refused(
        self, client, user_auth_headers: dict[str, str], _restore_keystore
    ) -> None:
        """An authenticated caller lacking system:config scope -> 403, not 200."""
        resp = await client.post(ROTATE_PATH, headers=user_auth_headers)
        assert resp.status_code == 403

    async def test_admin_allowed(
        self, client, auth_headers: dict[str, str], _restore_keystore
    ) -> None:
        """An admin (system:config scope) may trigger rotation."""
        resp = await client.post(ROTATE_PATH, headers=auth_headers)
        assert resp.status_code == 200
