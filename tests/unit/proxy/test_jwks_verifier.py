"""Tests for ``shared.auth.jwks_verifier`` (headless-auth H4).

TDD, failing-first against a real background HTTP server (not a mocked
``PyJWKClient``) so the actual fetch/cache/kid-selection/rotation/fail-closed
behavior of ``jwt.PyJWKClient`` -- the library primitive ``JWKSVerifier``
wraps -- is exercised end to end, the same way it runs in production.

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

from shared.auth.jwks_verifier import (
    JWKSVerificationError,
    JWKSVerifier,
    JWKSVerifierConfig,
    create_jwks_verifier,
)

ISSUER = "https://waddleai.test"
AUDIENCE = "waddleai-api-test"


def _rsa_keypair() -> RSAPrivateKey:
    """Generate a fresh 2048-bit RSA keypair."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_for(private_key: RSAPrivateKey, kid: str) -> dict[str, Any]:
    """Serialise *private_key*'s public half to a signing JWK dict tagged with *kid*."""
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def _make_token(
    private_key: RSAPrivateKey,
    kid: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    tenant: str = "tenant-abc",
    exp_delta: timedelta = timedelta(hours=1),
) -> str:
    """Sign an RS256 token with *private_key*, tagged with *kid* in its header."""
    now = datetime.now(UTC)
    claims = {
        "sub": "user-123",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_delta,
        "tenant": tenant,
        "scope": ["widgets:read"],
        "teams": [],
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


class _JWKSTestServer:
    """A real, mutable, stoppable HTTP server serving a JSON JWKS document.

    Backed by ``http.server`` (stdlib, no new test dependency) so
    ``jwt.PyJWKClient``'s real ``urllib``-based fetch runs against an actual
    socket -- ``stop()`` closes the listening socket entirely, producing a
    genuine connection-refused error for the "JWKS unreachable" scenarios
    rather than a mocked exception.
    """

    def __init__(self, keys: dict[str, Any]) -> None:
        """Start serving *keys* (mutated in place via ``set_keys``) on 127.0.0.1."""
        self._keys = keys
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's required name
                body = json.dumps({"keys": list(server._keys.values())}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # noqa: D102 -- silence test noise
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The JWKS document's URL."""
        # Always bound to "127.0.0.1" explicitly (see __init__) -- avoid
        # round-tripping through server_address[0], whose stub type admits
        # bytes (AF_UNIX sockets), which the mypy-gate flags on f-string use.
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/.well-known/jwks.json"

    def set_keys(self, keys: dict[str, Any]) -> None:
        """Replace the served key set (simulates a rotation)."""
        self._keys.clear()
        self._keys.update(keys)

    def stop(self) -> None:
        """Shut down the listening socket -- further requests get connection-refused."""
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def jwks_server() -> Iterator[_JWKSTestServer]:
    """A JWKS server started empty; tests populate it via ``set_keys``."""
    server = _JWKSTestServer({})
    yield server
    server.stop()


def _config(jwks_url: str, **overrides: Any) -> JWKSVerifierConfig:
    defaults: dict[str, Any] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": jwks_url,
        "http_timeout_seconds": 2.0,
    }
    defaults.update(overrides)
    return JWKSVerifierConfig(**defaults)


class TestJWKSVerifierConfig:
    """Construction-time validation of JWKSVerifierConfig."""

    def test_rejects_non_https_non_localhost_jwks_url(self) -> None:
        """A plaintext-HTTP jwks_url against a non-local host is rejected."""
        with pytest.raises(ValueError, match="HTTPS"):
            JWKSVerifierConfig(
                issuer=ISSUER, audience=AUDIENCE, jwks_url="http://evil.example/jwks"
            )

    def test_rejects_disallowed_algorithm(self) -> None:
        """HS256 (and other non-allowlisted algorithms) are rejected."""
        with pytest.raises(ValueError):
            JWKSVerifierConfig(
                issuer=ISSUER, audience=AUDIENCE, jwks_url=ISSUER, algorithms=["HS256"]
            )

    def test_rejects_non_positive_cache_ttl(self) -> None:
        """A zero or negative cache TTL is rejected."""
        with pytest.raises(ValueError, match="cache_ttl_seconds"):
            JWKSVerifierConfig(
                issuer=ISSUER, audience=AUDIENCE, jwks_url=ISSUER, cache_ttl_seconds=0
            )


class TestJWKSVerifierHappyPath:
    """A token signed with a kid published in the JWKS validates."""

    def test_matching_kid_validates(self, jwks_server: _JWKSTestServer) -> None:
        """A token signed with a kid published in the JWKS validates."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        token = _make_token(key, "kid-a")
        claims = verifier.verify_token(token)

        assert claims.sub == "user-123"
        assert claims.tenant == "tenant-abc"
        assert claims.scope == ["widgets:read"]


class TestUnknownKid:
    """A kid the JWKS has never published forces one refetch, then is rejected."""

    def test_unknown_kid_refetches_then_rejects_if_still_absent(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        """An unknown kid triggers a forced refetch, then rejects if still absent."""
        known_key = _rsa_keypair()
        unknown_key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(known_key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        # kid-b was never published -- PyJWKClient forces one refetch, finds
        # the same document (still only kid-a), and gives up.
        token = _make_token(unknown_key, "kid-b")
        with pytest.raises(JWKSVerificationError):
            verifier.verify_token(token)


class TestRotationOverlap:
    """Old and new signing keys both validate while a rotation is in progress."""

    def test_old_and_new_kid_both_validate_during_rotation(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        """Both the retiring and incoming kid validate from the same JWKS fetch."""
        old_key = _rsa_keypair()
        new_key = _rsa_keypair()
        jwks_server.set_keys(
            {
                "kid-old": _jwk_for(old_key, "kid-old"),
                "kid-new": _jwk_for(new_key, "kid-new"),
            }
        )
        verifier = JWKSVerifier(_config(jwks_server.url))

        old_token = _make_token(old_key, "kid-old")
        new_token = _make_token(new_key, "kid-new")

        assert verifier.verify_token(old_token).sub == "user-123"
        assert verifier.verify_token(new_token).sub == "user-123"


class TestFailClosedOnBadToken:
    """A resolvable kid still rejects a token that fails any other check."""

    def test_expired_token_rejected(self, jwks_server: _JWKSTestServer) -> None:
        """An expired token is rejected even with a valid signature."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        token = _make_token(key, "kid-a", exp_delta=timedelta(hours=-1))
        with pytest.raises(JWKSVerificationError, match="expired"):
            verifier.verify_token(token)

    def test_bad_signature_rejected(self, jwks_server: _JWKSTestServer) -> None:
        """A token signed by a key other than the one published under its kid is rejected."""
        published_key = _rsa_keypair()
        forger_key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(published_key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        # Signed by a different private key than the one published under kid-a.
        token = _make_token(forger_key, "kid-a")
        with pytest.raises(JWKSVerificationError):
            verifier.verify_token(token)

    def test_wrong_issuer_rejected(self, jwks_server: _JWKSTestServer) -> None:
        """A token claiming an unexpected issuer is rejected."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        token = _make_token(key, "kid-a", issuer="https://not-waddleai.test")
        with pytest.raises(JWKSVerificationError):
            verifier.verify_token(token)

    def test_wrong_audience_rejected(self, jwks_server: _JWKSTestServer) -> None:
        """A token claiming an unexpected audience is rejected."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        token = _make_token(key, "kid-a", audience="not-the-right-audience")
        with pytest.raises(JWKSVerificationError):
            verifier.verify_token(token)


def _make_token_missing_claim(private_key: RSAPrivateKey, kid: str, *, omit: str) -> str:
    """Sign an otherwise-valid RS256 token with *omit* dropped from its claims.

    Used to prove PyJWT only validates a claim if it is present -- a
    well-formed, correctly-signed token that simply never carries ``exp``
    (or ``iss``/``aud``/``sub``) would decode successfully without
    ``options={"require": [...]}``.
    """
    now = datetime.now(UTC)
    claims = {
        "sub": "user-123",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "tenant": "tenant-abc",
        "scope": ["widgets:read"],
        "teams": [],
    }
    del claims[omit]
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


class TestRequiredClaims:
    """A signature-valid token missing a critical claim is still rejected.

    regression: headless-auth-secrev (I1) -- without an explicit
    ``options={"require": [...]}``, PyJWT validates a claim only when it is
    present, so a token that simply omits ``exp`` would skip expiry
    checking entirely (and likewise for ``iss``/``aud``/``sub``) rather
    than being rejected as incomplete.
    """

    @pytest.mark.parametrize("omit", ["exp", "iss", "aud", "sub"])
    def test_token_missing_a_critical_claim_is_rejected(
        self, jwks_server: _JWKSTestServer, omit: str
    ) -> None:
        """A signature-valid token that simply omits *omit* is still rejected."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        token = _make_token_missing_claim(key, "kid-a", omit=omit)
        with pytest.raises(JWKSVerificationError):
            verifier.verify_token(token)

    def test_decode_is_called_with_the_required_claims_option(
        self, jwks_server: _JWKSTestServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert the fix mechanism directly, not just the outcome.

        The downstream ``Claims`` pydantic model (``sub``/``iss``/``aud``/
        ``exp`` are all non-Optional fields) already rejects a payload
        missing any of them, which is why the behavioural test above passes
        even without this module's own ``options={"require": [...]}`` --
        that safety net is specific to this file and does not exist in
        ``penguincode``'s independent validator (see
        ``test_auth_middleware.py``'s equivalent test, where omitting
        ``exp``/``sub`` previously decoded successfully). This test pins the
        ``jwt.decode`` call itself so a future edit cannot silently drop the
        option and rely on the pydantic model alone.
        """
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))
        token = _make_token(key, "kid-a")

        real_decode = jwt.decode
        captured: dict[str, Any] = {}

        def _spy_decode(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return real_decode(*args, **kwargs)

        monkeypatch.setattr(jwt, "decode", _spy_decode)
        verifier.verify_token(token)

        required = set((captured.get("options") or {}).get("require", []))
        assert {"exp", "iss", "aud", "sub"} <= required


class TestJWKSAvailability:
    """Cached-vs-uncached behavior when the JWKS endpoint is unreachable."""

    def test_transiently_unreachable_but_cached_still_validates(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        """A cache-warm kid keeps validating even after the JWKS endpoint goes down."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url, cache_ttl_seconds=300.0))

        token = _make_token(key, "kid-a")
        # Warm the cache with one successful fetch.
        assert verifier.verify_token(token).sub == "user-123"

        # Simulate an outage: the endpoint is now entirely unreachable.
        jwks_server.stop()

        # Same kid, already cached and within the TTL window -- no network
        # call is even attempted, so the outage is invisible here.
        second_token = _make_token(key, "kid-a")
        assert verifier.verify_token(second_token).sub == "user-123"

    def test_unreachable_and_uncached_fails_closed(self, jwks_server: _JWKSTestServer) -> None:
        """Never fail open: an unreachable JWKS with nothing cached rejects the token."""
        key = _rsa_keypair()
        jwks_server.set_keys({"kid-a": _jwk_for(key, "kid-a")})
        verifier = JWKSVerifier(_config(jwks_server.url))

        # No verify_token() call yet -- the cache has never been warmed.
        jwks_server.stop()

        token = _make_token(key, "kid-a")
        with pytest.raises(JWKSVerificationError, match="unreachable"):
            verifier.verify_token(token)

    def test_token_exceeding_max_size_rejected_before_any_fetch(
        self, jwks_server: _JWKSTestServer
    ) -> None:
        """An oversized token is rejected before any JWKS fetch is attempted."""
        verifier = JWKSVerifier(_config(jwks_server.url))
        with pytest.raises(JWKSVerificationError, match="exceeds maximum"):
            verifier.verify_token("x" * 8193)


class TestCreateJWKSVerifierFromEnv:
    """create_jwks_verifier()'s env-var-driven JWKS URL derivation."""

    def test_derives_jwks_url_from_issuer_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OIDC_JWKS_URL unset -- jwks_url derives from OIDC_ISSUER_URL."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://issuer.example")
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        monkeypatch.setenv("OIDC_CLIENT_ID", "my-client")

        verifier = create_jwks_verifier()

        assert verifier._config.jwks_url == "https://issuer.example/.well-known/jwks.json"
        assert verifier._config.issuer == "https://issuer.example"
        assert verifier._config.audience == "my-client"

    def test_explicit_jwks_url_overrides_derivation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OIDC_JWKS_URL set -- it wins over the OIDC_ISSUER_URL-derived default."""
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://issuer.example")
        monkeypatch.setenv("OIDC_JWKS_URL", "https://other-host.example/keys")

        verifier = create_jwks_verifier()

        assert verifier._config.jwks_url == "https://other-host.example/keys"
