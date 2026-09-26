"""Tests for `server/main.py`'s T-L2b interceptor wiring: nesting a second
`MethodPrefixRoutingInterceptor` so `LessonsService` shares `KnowledgeService`'s RS256/
`ScopeContext` gate.

Builds the exact nested-router composition `PenguinCodeServer.start()` constructs (see that
module's "T-L2b addendum" docstring note) rather than importing `tests/test_server_
interceptors.py`'s fixtures (kept self-contained -- that file predates this change and isn't
owned by this task). Proves:

- A `LessonsService` method accepts a WaddleAI RS256 JWT and rejects penguincode's legacy
  HS256 token.
- A `KnowledgeService` method is unaffected by the added nesting (regression check).
- A legacy (Chat/Auth/Tool/Health) method still only accepts the HS256 token, never RS256.

# regression: lessons-promotion (T-L2b -- LessonsService shares KnowledgeService's RS256 gate)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
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
    WaddleAIAuthInterceptor,
    WaddleAIJWTValidator,
)
from penguincode_cli.server.interceptors import (
    JWTValidationInterceptor,
    MethodPrefixRoutingInterceptor,
)

KNOWLEDGE_PREFIX = "/penguincode.knowledge.v1.KnowledgeService/"
LESSONS_PREFIX = "/penguincode.lessons.v1.LessonsService/"
LEGACY_JWT_SECRET = "a-legacy-hs256-secret-that-is-long-enough"  # nosec B105 -- test fixture, not a real secret

ISSUER = "https://waddleai.test"
AUDIENCE = "waddleai-api-test"


def _rsa_keypair() -> tuple[str, str]:
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


def _waddleai_rs256_token(**extra_claims: Any) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": "user-1",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "tenant": "tenant-a",
        "org": "org-a",
        "teams": ["team-a"],
        "scope": ["lessons:approve"],
    }
    claims.update(extra_claims)
    return pyjwt.encode(claims, PRIVATE_PEM, algorithm="RS256")


def _legacy_hs256_token(**extra_claims: Any) -> str:
    claims: dict[str, Any] = {"sub": "user-1", "type": "access"}
    claims.update(extra_claims)
    return pyjwt.encode(claims, LEGACY_JWT_SECRET, algorithm="HS256")


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


async def _run(interceptor: Any, method: str, token: str | None) -> tuple[bool, Any]:
    """Run *interceptor* for one call; returns (continuation_called, result_or_aborted_with)."""
    metadata = (("authorization", f"Bearer {token}"),) if token else ()
    details = _FakeHandlerCallDetails(method, metadata)

    called = {"yes": False}

    async def continuation(_details: Any) -> str:
        called["yes"] = True
        return "handler-result"

    result = await interceptor.intercept_service(continuation, details)
    if called["yes"] or result == "handler-result":
        return True, result

    fake_context = _FakeAbortContext()
    with pytest.raises(RuntimeError):
        await result.unary_unary(object(), fake_context)
    return False, fake_context.aborted_with


@pytest.fixture
def nested_router() -> MethodPrefixRoutingInterceptor:
    """The exact composition `server/main.py`'s `PenguinCodeServer.start()` builds."""
    legacy = JWTValidationInterceptor(
        jwt_secret=LEGACY_JWT_SECRET,
        excluded_methods=[
            "/penguincode.AuthService/Authenticate",
            "/penguincode.HealthService/Check",
        ],
    )
    waddleai = WaddleAIAuthInterceptor(
        WaddleAIJWTValidator(
            JWTValidatorConfig(
                public_key=PUBLIC_PEM,
                jwks_url=None,
                issuer=ISSUER,
                audience=AUDIENCE,
                algorithms=("RS256",),
            )
        )
    )
    return MethodPrefixRoutingInterceptor(
        KNOWLEDGE_PREFIX,
        matched=waddleai,
        unmatched=MethodPrefixRoutingInterceptor(
            LESSONS_PREFIX, matched=waddleai, unmatched=legacy
        ),
    )


class TestLessonsServiceSharesTheRS256Gate:
    @pytest.mark.asyncio
    async def test_lessons_method_accepts_waddleai_rs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _waddleai_rs256_token()
        called, result = await _run(nested_router, f"{LESSONS_PREFIX}ApproveLesson", token)
        assert called is True
        assert result == "handler-result"

    @pytest.mark.asyncio
    async def test_lessons_method_rejects_legacy_hs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _legacy_hs256_token()
        called, aborted_with = await _run(nested_router, f"{LESSONS_PREFIX}ApproveLesson", token)
        assert called is False
        assert aborted_with is not None

    @pytest.mark.asyncio
    async def test_lessons_method_rejects_missing_token(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        called, aborted_with = await _run(
            nested_router, f"{LESSONS_PREFIX}PromoteLesson", token=None
        )
        assert called is False
        assert aborted_with is not None


class TestKnowledgeServiceUnaffectedByNesting:
    @pytest.mark.asyncio
    async def test_knowledge_method_still_accepts_waddleai_rs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _waddleai_rs256_token()
        called, result = await _run(nested_router, f"{KNOWLEDGE_PREFIX}Query", token)
        assert called is True
        assert result == "handler-result"

    @pytest.mark.asyncio
    async def test_knowledge_method_still_rejects_legacy_hs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _legacy_hs256_token()
        called, aborted_with = await _run(nested_router, f"{KNOWLEDGE_PREFIX}Query", token)
        assert called is False
        assert aborted_with is not None


class TestLegacyServicesUnaffectedByNesting:
    @pytest.mark.asyncio
    async def test_legacy_method_accepts_hs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _legacy_hs256_token()
        called, result = await _run(nested_router, "/penguincode.ChatService/Send", token)
        assert called is True
        assert result == "handler-result"

    @pytest.mark.asyncio
    async def test_legacy_method_rejects_waddleai_rs256(
        self, nested_router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _waddleai_rs256_token()
        called, aborted_with = await _run(nested_router, "/penguincode.ChatService/Send", token)
        assert called is False
        assert aborted_with is not None
