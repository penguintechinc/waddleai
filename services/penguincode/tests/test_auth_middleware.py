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
            def __init__(self, uri: str) -> None:
                self.uri = uri

            def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
                return SimpleNamespace(key=PUBLIC_PEM)

        monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)

        jwks_validator = WaddleAIJWTValidator(config)
        token = _make_token()
        claims = jwks_validator.validate(token)
        assert claims["sub"] == "user-123"


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
