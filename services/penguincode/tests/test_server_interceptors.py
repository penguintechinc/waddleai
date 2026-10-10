"""Tests for `penguincode_cli.server.interceptors` -- interceptor reconciliation (F2).

Covers the two new classes added to gate `KnowledgeService` independently of
penguincode's pre-existing legacy gRPC services (Chat/Auth/Tool/Health):

- `PassthroughInterceptor`: a no-op, used when legacy HS256 auth is disabled.
- `MethodPrefixRoutingInterceptor`: routes each call to one of two
  interceptors based on its method path's prefix.

The `TestReconciliation` class at the bottom is the important one -- it wires
the *real* `JWTValidationInterceptor` (HS256, legacy) and the *real*
`WaddleAIAuthInterceptor` (RS256, `ScopeContext`) behind a real
`MethodPrefixRoutingInterceptor`, exactly as `server/main.py` builds them, and
proves: a knowledge-service method only ever accepts a WaddleAI RS256 JWT: a
legacy method only ever accepts penguincode's own HS256 token, and each
gate rejects the *other* token type -- the security property F3 (the CLI
client) depends on when deciding which token to attach to which call.

# regression: penguincode-knowledge-platform (F2 -- KnowledgeService server + auth wiring)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

import penguincode_cli.auth.middleware as auth_middleware
from penguincode_cli.auth.middleware import (
    JWTValidatorConfig,
    WaddleAIAuthInterceptor,
    WaddleAIJWTValidator,
    current_scope_context,
)
from penguincode_cli.server.interceptors import (
    DISABLE_CHAT_RS256_GATE_FLAG,
    JWTValidationInterceptor,
    MethodPrefixRoutingInterceptor,
    PassthroughInterceptor,
    reset_chat_rs256_warning_for_testing,
)

KNOWLEDGE_PREFIX = "/penguincode.knowledge.v1.KnowledgeService/"
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
        "scope": ["knowledge:read"],
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
    """Run *interceptor* for one call; returns (continuation_called, result_or_context)."""
    metadata = (("authorization", f"Bearer {token}"),) if token else ()
    details = _FakeHandlerCallDetails(method, metadata)

    called = {"yes": False}

    async def continuation(_details: Any) -> str:
        called["yes"] = True
        return "handler-result"

    result = await interceptor.intercept_service(continuation, details)
    if called["yes"]:
        return True, result

    # Not called directly -- either handed back an abort-handler (unary_unary
    # wrapping `context.abort`), or the underlying interceptor itself invoked
    # `continuation` and returned its result unchanged (PassthroughInterceptor).
    if result == "handler-result":
        return True, result

    fake_context = _FakeAbortContext()
    with pytest.raises(RuntimeError):
        await result.unary_unary(object(), fake_context)
    return False, fake_context.aborted_with


class TestPassthroughInterceptor:
    @pytest.mark.asyncio
    async def test_always_calls_continuation(self) -> None:
        interceptor = PassthroughInterceptor()
        called, result = await _run(interceptor, "/anything/AtAll", token=None)
        assert called is True
        assert result == "handler-result"


class TestMethodPrefixRoutingInterceptor:
    @pytest.mark.asyncio
    async def test_matched_prefix_routes_to_matched_interceptor(self) -> None:
        matched_calls: list[str] = []
        unmatched_calls: list[str] = []

        class _Recorder:
            def __init__(self, sink: list[str], label: str) -> None:
                self._sink = sink
                self._label = label

            async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
                self._sink.append(handler_call_details.method)
                return await continuation(handler_call_details)

        router = MethodPrefixRoutingInterceptor(
            "/knowledge.v1/",
            matched=_Recorder(matched_calls, "matched"),
            unmatched=_Recorder(unmatched_calls, "unmatched"),
        )

        called, result = await _run(router, "/knowledge.v1/Query", token=None)
        assert called is True
        assert matched_calls == ["/knowledge.v1/Query"]
        assert unmatched_calls == []

    @pytest.mark.asyncio
    async def test_unmatched_prefix_routes_to_unmatched_interceptor(self) -> None:
        matched_calls: list[str] = []
        unmatched_calls: list[str] = []

        class _Recorder:
            def __init__(self, sink: list[str]) -> None:
                self._sink = sink

            async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
                self._sink.append(handler_call_details.method)
                return await continuation(handler_call_details)

        router = MethodPrefixRoutingInterceptor(
            "/knowledge.v1/",
            matched=_Recorder(matched_calls),
            unmatched=_Recorder(unmatched_calls),
        )

        await _run(router, "/penguincode.ChatService/Send", token=None)
        assert unmatched_calls == ["/penguincode.ChatService/Send"]
        assert matched_calls == []


class TestReconciliation:
    """The real `server/main.py` wiring: RS256 gates knowledge, HS256 gates legacy."""

    @pytest.fixture
    def router(self) -> MethodPrefixRoutingInterceptor:
        legacy = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET,
            excluded_methods=[
                "/penguincode.AuthService/Authenticate",
                "/penguincode.HealthService/Check",
            ],
        )
        knowledge = WaddleAIAuthInterceptor(
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
        return MethodPrefixRoutingInterceptor(KNOWLEDGE_PREFIX, matched=knowledge, unmatched=legacy)

    @pytest.mark.asyncio
    async def test_knowledge_method_accepts_waddleai_rs256_and_sets_scope(
        self, router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _waddleai_rs256_token()
        called, _ = await _run(router, f"{KNOWLEDGE_PREFIX}Query", token)
        assert called is True
        ctx = current_scope_context()
        assert ctx is not None
        assert ctx.tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_knowledge_method_rejects_legacy_hs256_token(
        self, router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _legacy_hs256_token()
        called, aborted_with = await _run(router, f"{KNOWLEDGE_PREFIX}Query", token)
        assert called is False
        assert aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_legacy_method_accepts_hs256_token(
        self, router: MethodPrefixRoutingInterceptor
    ) -> None:
        # regression: `/penguincode.ChatService/*` used to be the example "legacy"
        # method here -- it is now RS256-gated too (tenancy-gap fix, see
        # TestChatServiceRS256Gate below), so a genuinely still-legacy method
        # (ToolCallbackService, untouched by that fix) is used instead.
        token = _legacy_hs256_token()
        called, _ = await _run(router, "/penguincode.ToolCallbackService/ExecuteTools", token)
        assert called is True

    @pytest.mark.asyncio
    async def test_legacy_method_rejects_waddleai_rs256_token(
        self, router: MethodPrefixRoutingInterceptor
    ) -> None:
        token = _waddleai_rs256_token()
        called, aborted_with = await _run(
            router, "/penguincode.ToolCallbackService/ExecuteTools", token
        )
        assert called is False
        assert aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_legacy_excluded_method_still_bypasses_auth(
        self, router: MethodPrefixRoutingInterceptor
    ) -> None:
        called, result = await _run(router, "/penguincode.HealthService/Check", token=None)
        assert called is True
        assert result == "handler-result"


CHAT_METHOD = "/penguincode.ChatService/Chat"


class TestChatServiceRS256Gate:
    """Tenancy-gap fix: `ChatService` now shares `KnowledgeService`'s RS256 gate.

    Covers both interceptors that could otherwise serve a `ChatService` call
    (`JWTValidationInterceptor` when `settings.auth.enabled=True`,
    `PassthroughInterceptor` when it's `False`) -- both must divert Chat
    calls to RS256 by default, and both must fall back to their own legacy
    behavior (plus a one-time WARN) when the opt-out kill switch is ON.

    # regression: penguincode-chat-rs256-scope (tenancy gap, PR #262 follow-up)
    """

    def _waddleai_interceptor(self) -> WaddleAIAuthInterceptor:
        return WaddleAIAuthInterceptor(
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

    @pytest.fixture(autouse=True)
    def _reset_warn_latch(self) -> None:
        reset_chat_rs256_warning_for_testing()

    @pytest.fixture(autouse=True)
    def _reset_scope_context(self) -> Iterator[None]:
        """Isolate this class's ScopeContext assertions from any other test in this module."""
        auth_middleware._current_scope.set(None)
        yield
        auth_middleware._current_scope.set(None)

    @pytest.mark.asyncio
    async def test_jwt_validation_interceptor_requires_rs256_for_chat_by_default(self) -> None:
        interceptor = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET, waddleai_interceptor=self._waddleai_interceptor()
        )
        token = _waddleai_rs256_token()
        called, _ = await _run(interceptor, CHAT_METHOD, token)
        assert called is True
        ctx = current_scope_context()
        assert ctx is not None
        assert ctx.tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_jwt_validation_interceptor_rejects_legacy_hs256_for_chat_by_default(
        self,
    ) -> None:
        interceptor = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET, waddleai_interceptor=self._waddleai_interceptor()
        )
        token = _legacy_hs256_token()
        called, aborted_with = await _run(interceptor, CHAT_METHOD, token)
        assert called is False
        assert aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_jwt_validation_interceptor_missing_token_for_chat_is_unauthenticated(
        self,
    ) -> None:
        interceptor = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET, waddleai_interceptor=self._waddleai_interceptor()
        )
        called, aborted_with = await _run(interceptor, CHAT_METHOD, token=None)
        assert called is False
        assert aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_kill_switch_reverts_chat_to_legacy_hs256_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_CHAT_RS256_GATE", "true")
        interceptor = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET, waddleai_interceptor=self._waddleai_interceptor()
        )
        token = _legacy_hs256_token()

        with caplog.at_level("WARNING"):
            called_1, _ = await _run(interceptor, CHAT_METHOD, token)
            called_2, _ = await _run(interceptor, CHAT_METHOD, token)

        assert called_1 is True
        assert called_2 is True
        warnings = [r for r in caplog.records if DISABLE_CHAT_RS256_GATE_FLAG in r.getMessage()]
        assert len(warnings) == 1  # warn-once, not once per call

    @pytest.mark.asyncio
    async def test_kill_switch_on_still_rejects_rs256_token_for_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON reverts to legacy HS256-only -- an RS256 token is no longer valid either."""
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_CHAT_RS256_GATE", "true")
        interceptor = JWTValidationInterceptor(
            jwt_secret=LEGACY_JWT_SECRET, waddleai_interceptor=self._waddleai_interceptor()
        )
        token = _waddleai_rs256_token()
        called, aborted_with = await _run(interceptor, CHAT_METHOD, token)
        assert called is False
        assert aborted_with[0] == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_passthrough_interceptor_also_requires_rs256_for_chat_by_default(self) -> None:
        """Standalone mode (`settings.auth.enabled=False`) does not exempt Chat."""
        interceptor = PassthroughInterceptor(waddleai_interceptor=self._waddleai_interceptor())
        token = _waddleai_rs256_token()
        called, _ = await _run(interceptor, CHAT_METHOD, token)
        assert called is True
        assert current_scope_context() is not None

    @pytest.mark.asyncio
    async def test_passthrough_interceptor_non_chat_methods_still_bypass_auth(self) -> None:
        interceptor = PassthroughInterceptor(waddleai_interceptor=self._waddleai_interceptor())
        called, result = await _run(interceptor, "/penguincode.AuthService/RefreshToken", None)
        assert called is True
        assert result == "handler-result"

    @pytest.mark.asyncio
    async def test_passthrough_interceptor_kill_switch_bypasses_chat_auth_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_CHAT_RS256_GATE", "true")
        interceptor = PassthroughInterceptor(waddleai_interceptor=self._waddleai_interceptor())
        called, result = await _run(interceptor, CHAT_METHOD, token=None)
        assert called is True
        assert result == "handler-result"
