"""Tests for ``penguincode_cli.auth.middleware`` -- WaddleAI JWT validation.

TDD: written before ``penguincode_cli/auth/middleware.py`` exists; must fail
with an ImportError/ModuleNotFoundError until the module is implemented.
Uses crafted RS256 JWTs (an in-test RSA keypair) -- no DB/network required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from penguincode_cli.auth.middleware import (
    JWTValidatorConfig,
    SpiffeIdentity,
    TokenValidationError,
    WaddleAIAuthInterceptor,
    WaddleAIJWTValidator,
    authenticate_request,
    current_scope_context,
    default_spiffe_verifier,
    extract_bearer_token,
    extract_token_from_grpc_metadata,
    extract_token_from_headers,
)
from penguincode_cli.auth.scope import ScopeContext

ISSUER = "https://waddleai.test"
AUDIENCE = "waddleai-api-test"


def _rsa_keypair() -> tuple[str, str]:
    """Generate a fresh RSA keypair; returns (private_pem, public_pem)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _rsa_keypair()
OTHER_PRIVATE_PEM, _OTHER_PUBLIC_PEM = _rsa_keypair()


def _make_token(
    *,
    private_pem: str = PRIVATE_PEM,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    exp_delta: timedelta = timedelta(hours=1),
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": "user-123",
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + exp_delta,
        "tenant": "tenant-abc",
        "org": "org-xyz",
        "teams": ["team-1", "team-2"],
        "scope": ["widgets:read", "widgets:write"],
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_pem, algorithm="RS256")


@pytest.fixture
def validator() -> WaddleAIJWTValidator:
    config = JWTValidatorConfig(
        public_key=PUBLIC_PEM,
        jwks_url=None,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
    )
    return WaddleAIJWTValidator(config)


class TestJWTValidatorConfig:
    def test_from_env_reads_public_key_and_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WADDLEAI_JWT_PUBLIC_KEY", PUBLIC_PEM)
        monkeypatch.delenv("WADDLEAI_JWT_PUBLIC_KEY_FILE", raising=False)
        monkeypatch.delenv("WADDLEAI_JWT_JWKS_URL", raising=False)
        monkeypatch.delenv("WADDLEAI_JWT_ISSUER", raising=False)
        monkeypatch.delenv("WADDLEAI_JWT_AUDIENCE", raising=False)
        monkeypatch.delenv("WADDLEAI_JWT_ALGORITHMS", raising=False)

        config = JWTValidatorConfig.from_env()

        assert config.public_key == PUBLIC_PEM
        assert config.jwks_url is None
        assert config.issuer == "https://waddleai.localhost.local"
        assert config.audience == "waddleai-api"
        assert config.algorithms == ("RS256",)

    def test_from_env_reads_public_key_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        key_file = tmp_path / "waddleai-jwt.pub"
        key_file.write_text(PUBLIC_PEM)
        monkeypatch.delenv("WADDLEAI_JWT_PUBLIC_KEY", raising=False)
        monkeypatch.setenv("WADDLEAI_JWT_PUBLIC_KEY_FILE", str(key_file))

        config = JWTValidatorConfig.from_env()

        assert config.public_key == PUBLIC_PEM

    def test_from_env_parses_comma_separated_algorithms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WADDLEAI_JWT_PUBLIC_KEY", PUBLIC_PEM)
        monkeypatch.setenv("WADDLEAI_JWT_ALGORITHMS", "RS256, ES256")

        config = JWTValidatorConfig.from_env()

        assert config.algorithms == ("RS256", "ES256")

    def test_config_is_frozen(self) -> None:
        config = JWTValidatorConfig(
            public_key=PUBLIC_PEM,
            jwks_url=None,
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )
        with pytest.raises(AttributeError):
            config.issuer = "other"  # type: ignore[misc]

    def test_jwks_default_cache_ttl_and_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WADDLEAI_JWT_JWKS_URL", "https://waddleai.test/jwks.json")
        monkeypatch.delenv("WADDLEAI_JWT_JWKS_CACHE_TTL_SECONDS", raising=False)
        monkeypatch.delenv("WADDLEAI_JWT_JWKS_TIMEOUT_SECONDS", raising=False)

        config = JWTValidatorConfig.from_env()

        assert config.jwks_cache_ttl_seconds == 300.0
        assert config.jwks_http_timeout_seconds == 10.0

    def test_jwks_cache_ttl_and_timeout_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WADDLEAI_JWT_JWKS_URL", "https://waddleai.test/jwks.json")
        monkeypatch.setenv("WADDLEAI_JWT_JWKS_CACHE_TTL_SECONDS", "60")
        monkeypatch.setenv("WADDLEAI_JWT_JWKS_TIMEOUT_SECONDS", "3")

        config = JWTValidatorConfig.from_env()

        assert config.jwks_cache_ttl_seconds == 60.0
        assert config.jwks_http_timeout_seconds == 3.0

    def test_non_https_non_localhost_jwks_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            JWTValidatorConfig(
                public_key=None,
                jwks_url="http://evil.example/jwks.json",
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=("RS256",),
            )

    def test_localhost_jwks_url_allowed_over_http(self) -> None:
        # Should not raise -- local dev/tests only.
        JWTValidatorConfig(
            public_key=None,
            jwks_url="http://127.0.0.1:8080/.well-known/jwks.json",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )

    # regression: headless-auth-secrev (M1) -- RS256-to-HS256 key-confusion
    # forgery: an operator setting WADDLEAI_JWT_ALGORITHMS to include HS256
    # alongside a static WADDLEAI_JWT_PUBLIC_KEY would let anyone who can
    # read the (non-secret) RSA public key mint a token this validator
    # accepts, by re-signing it HS256 using the public key as the HMAC
    # secret. Construction must fail closed before that config is ever used.

    def test_hs256_alongside_asymmetric_algorithms_rejected(self) -> None:
        with pytest.raises(ValueError, match="HS256"):
            JWTValidatorConfig(
                public_key=PUBLIC_PEM,
                jwks_url=None,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=("RS256", "HS256"),
            )

    def test_hs384_and_hs512_also_rejected(self) -> None:
        for alg in ("HS384", "HS512"):
            with pytest.raises(ValueError, match=alg):
                JWTValidatorConfig(
                    public_key=PUBLIC_PEM,
                    jwks_url=None,
                    issuer=ISSUER,
                    audience=AUDIENCE,
                    algorithms=(alg,),
                )

    def test_none_algorithm_rejected(self) -> None:
        with pytest.raises(ValueError, match="none"):
            JWTValidatorConfig(
                public_key=PUBLIC_PEM,
                jwks_url=None,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=("none",),
            )

    def test_unknown_algorithm_outside_allowlist_rejected(self) -> None:
        with pytest.raises(ValueError, match="permitted asymmetric set"):
            JWTValidatorConfig(
                public_key=PUBLIC_PEM,
                jwks_url=None,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=("EdDSA",),
            )

    def test_rs256_default_still_constructs(self) -> None:
        # Should not raise -- RS256 alone is the documented default.
        JWTValidatorConfig(
            public_key=PUBLIC_PEM,
            jwks_url=None,
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )

    def test_full_asymmetric_allowlist_constructs(self) -> None:
        # Should not raise -- every algorithm in the documented allowlist.
        JWTValidatorConfig(
            public_key=PUBLIC_PEM,
            jwks_url=None,
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=(
                "RS256",
                "RS384",
                "RS512",
                "ES256",
                "ES384",
                "ES512",
                "PS256",
                "PS384",
                "PS512",
            ),
        )

    def test_from_env_hs256_algorithm_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator misconfiguring the env var fails at startup, not silently."""
        monkeypatch.setenv("WADDLEAI_JWT_PUBLIC_KEY", PUBLIC_PEM)
        monkeypatch.setenv("WADDLEAI_JWT_ALGORITHMS", "RS256,HS256")

        with pytest.raises(ValueError, match="HS256"):
            JWTValidatorConfig.from_env()


class TestWaddleAIJWTValidator:
    def test_valid_token_returns_claims(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token()
        claims = validator.validate(token)
        assert claims["sub"] == "user-123"
        assert claims["tenant"] == "tenant-abc"

    def test_valid_token_builds_scope_context(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token()
        ctx = validator.scope_context(token)
        assert ctx == ScopeContext(
            tenant_id="tenant-abc",
            org_id="org-xyz",
            team_ids=("team-1", "team-2"),
            user_id="user-123",
            scopes=("widgets:read", "widgets:write"),
        )

    def test_expired_token_rejected(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token(exp_delta=timedelta(hours=-1))
        with pytest.raises(TokenValidationError):
            validator.validate(token)

    def test_bad_signature_rejected(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token(private_pem=OTHER_PRIVATE_PEM)
        with pytest.raises(TokenValidationError):
            validator.validate(token)

    def test_wrong_issuer_rejected(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token(issuer="https://not-waddleai.test")
        with pytest.raises(TokenValidationError):
            validator.validate(token)

    def test_wrong_audience_rejected(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token(audience="not-the-right-audience")
        with pytest.raises(TokenValidationError):
            validator.validate(token)

    def test_missing_tenant_claim_rejected_as_token_error(
        self, validator: WaddleAIJWTValidator
    ) -> None:
        token = _make_token(extra_claims={"tenant": ""})
        with pytest.raises(TokenValidationError):
            validator.scope_context(token)

    def test_no_verification_key_configured_raises(self) -> None:
        config = JWTValidatorConfig(
            public_key=None, jwks_url=None, issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256",)
        )
        bad_validator = WaddleAIJWTValidator(config)
        with pytest.raises(TokenValidationError):
            bad_validator.validate(_make_token())

    def test_jwks_path_used_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = JWTValidatorConfig(
            public_key=None,
            jwks_url="https://waddleai.test/.well-known/jwks.json",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )

        class _FakeJWKClient:
            # **kwargs: headless-auth H4 added lifespan/timeout kwargs to the
            # real jwt.PyJWKClient(...) call (see _get_jwks_client) -- this
            # fake only needs to prove the URI it was built with.
            def __init__(self, uri: str, **kwargs: Any) -> None:
                self.uri = uri

            def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
                return SimpleNamespace(key=PUBLIC_PEM)

        monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)

        jwks_validator = WaddleAIJWTValidator(config)
        token = _make_token()
        claims = jwks_validator.validate(token)
        assert claims["sub"] == "user-123"

    def test_jwks_client_constructed_once_and_reused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """regression: headless-auth H4 -- no per-call PyJWKClient (no per-request JWKS fetch)."""
        config = JWTValidatorConfig(
            public_key=None,
            jwks_url="https://waddleai.test/.well-known/jwks.json",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )
        construction_count = {"n": 0}

        class _FakeJWKClient:
            def __init__(self, uri: str, **kwargs: Any) -> None:
                construction_count["n"] += 1

            def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
                return SimpleNamespace(key=PUBLIC_PEM)

        monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)

        jwks_validator = WaddleAIJWTValidator(config)
        jwks_validator.validate(_make_token())
        jwks_validator.validate(_make_token())
        jwks_validator.validate(_make_token())

        assert construction_count["n"] == 1

    def test_jwks_key_resolution_failure_raises_token_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreachable/unresolvable JWKS surfaces as TokenValidationError, never a raw PyJWTError."""
        config = JWTValidatorConfig(
            public_key=None,
            jwks_url="https://waddleai.test/.well-known/jwks.json",
            issuer=ISSUER,
            audience=AUDIENCE,
            algorithms=("RS256",),
        )

        class _UnreachableJWKClient:
            def __init__(self, uri: str, **kwargs: Any) -> None:
                pass

            def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
                raise jwt.PyJWKClientConnectionError("connection refused")

        monkeypatch.setattr(jwt, "PyJWKClient", _UnreachableJWKClient)

        jwks_validator = WaddleAIJWTValidator(config)
        with pytest.raises(TokenValidationError):
            jwks_validator.validate(_make_token())


class TestTokenExtraction:
    def test_extract_bearer_token_valid(self) -> None:
        assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_extract_bearer_token_case_insensitive_scheme(self) -> None:
        assert extract_bearer_token("bearer abc.def.ghi") == "abc.def.ghi"

    @pytest.mark.parametrize("value", [None, "", "abc.def.ghi", "Basic dXNlcjpwYXNz"])
    def test_extract_bearer_token_invalid(self, value: str | None) -> None:
        assert extract_bearer_token(value) is None

    def test_extract_from_grpc_metadata(self) -> None:
        metadata = (("authorization", "Bearer tok123"), ("x-other", "value"))
        assert extract_token_from_grpc_metadata(metadata) == "tok123"

    def test_extract_from_grpc_metadata_missing(self) -> None:
        assert extract_token_from_grpc_metadata((("x-other", "value"),)) is None
        assert extract_token_from_grpc_metadata(None) is None

    def test_extract_from_headers_case_insensitive(self) -> None:
        assert extract_token_from_headers({"Authorization": "Bearer tok456"}) == "tok456"

    def test_extract_from_headers_missing(self) -> None:
        assert extract_token_from_headers({}) is None


class TestAuthenticateRequest:
    def test_valid_header_returns_scope_context(self, validator: WaddleAIJWTValidator) -> None:
        token = _make_token()
        ctx = authenticate_request({"Authorization": f"Bearer {token}"}, validator)
        assert ctx.tenant_id == "tenant-abc"

    def test_missing_header_raises(self, validator: WaddleAIJWTValidator) -> None:
        with pytest.raises(TokenValidationError):
            authenticate_request({}, validator)


class _FakeHandlerCallDetails:
    def __init__(self, method: str, invocation_metadata: Any) -> None:
        self.method = method
        self.invocation_metadata = invocation_metadata


class _FakeAbortContext:
    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, message: str) -> None:
        self.aborted_with = (code, message)
        raise RuntimeError("aborted")  # grpc.abort raises internally


class TestWaddleAIAuthInterceptor:
    @pytest.mark.asyncio
    async def test_excluded_method_bypasses_auth(self, validator: WaddleAIJWTValidator) -> None:
        interceptor = WaddleAIAuthInterceptor(
            validator, excluded_methods=["/penguincode.HealthService/Check"]
        )
        details = _FakeHandlerCallDetails("/penguincode.HealthService/Check", ())

        called = {"yes": False}

        async def continuation(_details: Any) -> str:
            called["yes"] = True
            return "handler"

        result = await interceptor.intercept_service(continuation, details)
        assert result == "handler"
        assert called["yes"] is True

    @pytest.mark.asyncio
    async def test_missing_token_aborts_unauthenticated(
        self, validator: WaddleAIJWTValidator
    ) -> None:
        interceptor = WaddleAIAuthInterceptor(validator)
        details = _FakeHandlerCallDetails("/penguincode.ChatService/Send", ())

        async def continuation(_details: Any) -> str:
            raise AssertionError("continuation must not be called without a token")

        handler = await interceptor.intercept_service(continuation, details)
        fake_context = _FakeAbortContext()
        with pytest.raises(RuntimeError):
            await handler.unary_unary(object(), fake_context)
        assert fake_context.aborted_with is not None
        import grpc

        assert fake_context.aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_valid_token_propagates_scope_context(
        self, validator: WaddleAIJWTValidator
    ) -> None:
        interceptor = WaddleAIAuthInterceptor(validator)
        token = _make_token()
        details = _FakeHandlerCallDetails(
            "/penguincode.ChatService/Send", (("authorization", f"Bearer {token}"),)
        )

        captured: dict[str, Any] = {}

        async def continuation(_details: Any) -> str:
            captured["ctx"] = current_scope_context()
            return "handler"

        result = await interceptor.intercept_service(continuation, details)
        assert result == "handler"
        assert captured["ctx"] == ScopeContext(
            tenant_id="tenant-abc",
            org_id="org-xyz",
            team_ids=("team-1", "team-2"),
            user_id="user-123",
            scopes=("widgets:read", "widgets:write"),
        )

    @pytest.mark.asyncio
    async def test_invalid_token_aborts_unauthenticated(
        self, validator: WaddleAIJWTValidator
    ) -> None:
        interceptor = WaddleAIAuthInterceptor(validator)
        details = _FakeHandlerCallDetails(
            "/penguincode.ChatService/Send", (("authorization", "Bearer not-a-jwt"),)
        )

        async def continuation(_details: Any) -> str:
            raise AssertionError("continuation must not be called with an invalid token")

        handler = await interceptor.intercept_service(continuation, details)
        fake_context = _FakeAbortContext()
        with pytest.raises(RuntimeError):
            await handler.unary_unary(object(), fake_context)
        assert fake_context.aborted_with is not None


class TestSpiffeReadiness:
    """SPIFFE hook is additive identity enrichment, never a JWT bypass (security.md)."""

    def test_default_verifier_extracts_spiffe_id(self) -> None:
        fake_context = SimpleNamespace(
            auth_context=lambda: {
                "x509_subject_alternative_name": [b"spiffe://penguintech.io/beta/penguincode"]
            }
        )
        identity = default_spiffe_verifier(fake_context)
        assert identity == SpiffeIdentity(
            spiffe_id="spiffe://penguintech.io/beta/penguincode",
            trust_domain="penguintech.io",
        )

    def test_default_verifier_returns_none_without_spiffe_san(self) -> None:
        fake_context = SimpleNamespace(auth_context=lambda: {})
        assert default_spiffe_verifier(fake_context) is None

    def test_default_verifier_returns_none_on_non_mtls_context(self) -> None:
        fake_context = SimpleNamespace(
            auth_context=lambda: {"x509_subject_alternative_name": [b"not-a-spiffe-uri"]}
        )
        assert default_spiffe_verifier(fake_context) is None
