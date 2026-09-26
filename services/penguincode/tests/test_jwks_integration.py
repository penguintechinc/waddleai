"""Integration-style tests for ``WaddleAIJWTValidator``'s JWKS mode (headless-auth H4).

Unlike ``test_auth_middleware.py``'s ``_FakeJWKClient`` (which proves the
validator wires JWKS in at all), these drive a real, stoppable background
HTTP server so ``jwt.PyJWKClient``'s actual ``urllib``-based fetch/cache
runs end to end -- the same code path penguincode uses in production.

# regression: headless-auth
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from jwt.algorithms import RSAAlgorithm

from penguincode_cli.auth.middleware import (
    JWTValidatorConfig,
    TokenValidationError,
    WaddleAIJWTValidator,
)

ISSUER = "https://waddleai.test"
AUDIENCE = "waddleai-api-test"


def _rsa_keypair() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_for(private_key: RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def _make_token(
    private_key: RSAPrivateKey,
    kid: str,
    *,
    tenant: str = "tenant-abc",
    exp_delta: timedelta = timedelta(hours=1),
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "user-123",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + exp_delta,
        "tenant": tenant,
        "org": "org-xyz",
        "teams": [],
        "scope": ["widgets:read"],
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


class _JWKSTestServer:
    """A real, mutable, stoppable HTTP server serving a JSON JWKS document.

    See shared/auth/jwks_verifier.py's proxy-side twin
    (tests/unit/proxy/test_jwks_verifier.py::_JWKSTestServer) -- duplicated
    rather than imported: penguincode stays standalone (see
    penguincode_cli/auth/middleware.py's module docstring), including in
    its tests, so it never imports across the service boundary either.
    """

    def __init__(self, keys: dict[str, Any]) -> None:
        self._keys = keys
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"keys": list(server._keys.values())}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # noqa: D102
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/.well-known/jwks.json"

    def set_keys(self, keys: dict[str, Any]) -> None:
        self._keys.clear()
        self._keys.update(keys)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def jwks_server() -> Iterator[_JWKSTestServer]:
    server = _JWKSTestServer({})
    yield server
    server.stop()


def _validator(jwks_url: str, **overrides: Any) -> WaddleAIJWTValidator:
    config = JWTValidatorConfig(
        public_key=None,
        jwks_url=jwks_url,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        **overrides,
    )
    return WaddleAIJWTValidator(config)


class TestJWKSHappyPath:
    def test_matching_kid_validates(self, jwks_server: _JWKSTestServer) -> None:
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        validator = _validator(jwks_server.url)

        claims = validator.validate(_make_token(key, "kid-a"))

        assert claims["sub"] == "user-123"
        assert claims["tenant"] == "tenant-abc"


class TestUnknownKid:
    def test_unknown_kid_refetches_then_rejects_if_still_absent(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        known_key = _rsa_keypair()
        unknown_key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(known_key, "kid-a")})
        validator = _validator(jwks_server.url)

        token = _make_token(unknown_key, "kid-b")
        with pytest.raises(TokenValidationError):
            validator.validate(token)


class TestRotationOverlap:
    def test_old_and_new_kid_both_validate_during_rotation(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        old_key = _rsa_keypair()
        new_key = _rsa_keypair()
        jwks_server.set_keys(
            {"kid-old": _jwk_for(old_key, "kid-old"), "kid-new": _jwk_for(new_key, "kid-new")}
        )
        validator = _validator(jwks_server.url)

        assert validator.validate(_make_token(old_key, "kid-old"))["sub"] == "user-123"
        assert validator.validate(_make_token(new_key, "kid-new"))["sub"] == "user-123"


class TestFailClosed:
    def test_expired_token_rejected(self, jwks_server: _JWKSTestServer) -> None:
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        validator = _validator(jwks_server.url)

        token = _make_token(key, "kid-a", exp_delta=timedelta(hours=-1))
        with pytest.raises(TokenValidationError):
            validator.validate(token)

    def test_bad_signature_rejected(self, jwks_server: _JWKSTestServer) -> None:
        published_key = _rsa_keypair()
        forger_key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(published_key, "kid-a")})
        validator = _validator(jwks_server.url)

        token = _make_token(forger_key, "kid-a")
        with pytest.raises(TokenValidationError):
            validator.validate(token)


class TestJWKSAvailability:
    def test_transiently_unreachable_but_cached_still_validates(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        validator = _validator(jwks_server.url, jwks_cache_ttl_seconds=300.0)

        # Warm the cache.
        assert validator.validate(_make_token(key, "kid-a"))["sub"] == "user-123"

        jwks_server.stop()

        # Same, already-cached kid, still within the TTL window.
        assert validator.validate(_make_token(key, "kid-a"))["sub"] == "user-123"

    def test_unreachable_and_uncached_fails_closed(self, jwks_server: _JWKSTestServer) -> None:
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        validator = _validator(jwks_server.url)

        jwks_server.stop()  # never warmed

        with pytest.raises(TokenValidationError):
            validator.validate(_make_token(key, "kid-a"))
