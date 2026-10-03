"""Unit tests for the pre-existing (pre-O1-d) gRPC server interceptors.

These classes (AuthInterceptor, RateLimitInterceptor, AuditInterceptor,
RecoveryInterceptor) shipped with zero test coverage before this change; since
touching ``interceptors.py`` makes the whole module a "touched module" under
the coverage gate, this file closes that pre-existing gap alongside the new
tracing/correlation coverage in ``test_interceptors.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import grpc
import jwt
import pytest

from py_libs.grpc.interceptors import (
    AuditInterceptor,
    AuthInterceptor,
    RateLimitInterceptor,
    RecoveryInterceptor,
)

_SECRET = "test-secret"  # noqa: S105 -- test fixture, not a real credential


@dataclass(slots=True)
class _FakeCallDetails:
    """Minimal ``grpc.HandlerCallDetails`` stand-in."""

    method: str
    invocation_metadata: tuple[tuple[str, str], ...] = ()


class _AbortError(Exception):
    """Raised by ``_FakeContext.abort`` to mimic real grpc abort semantics."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details_ = details


@dataclass(slots=True)
class _FakeContext:
    """Minimal ``grpc.ServicerContext`` stand-in that records/raises on abort."""

    aborted: list[tuple[grpc.StatusCode, str]] = field(default_factory=list)

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted.append((code, details))
        raise _AbortError(code, details)


def _ok_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
    """Continuation returning a trivial handler that echoes the request."""
    return grpc.unary_unary_rpc_method_handler(
        lambda request, context: request,
        request_deserializer=lambda x: x,
        response_serializer=lambda x: x,
    )


class TestAuthInterceptor:
    """JWT bearer validation branches."""

    def test_public_method_bypasses_auth(self) -> None:
        """Public method bypasses auth."""
        interceptor = AuthInterceptor(secret_key=_SECRET, public_methods={"/svc/Public"})
        details = _FakeCallDetails(method="/svc/Public")
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_missing_auth_header_aborts(self) -> None:
        """Missing auth header aborts."""
        interceptor = AuthInterceptor(secret_key=_SECRET)
        details = _FakeCallDetails(method="/svc/Method", invocation_metadata=())
        handler = interceptor.intercept_service(_ok_continuation, details)
        with pytest.raises(_AbortError) as exc_info:
            handler.unary_unary(b"x", _FakeContext())
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED

    def test_valid_token_calls_continuation(self) -> None:
        """Valid token calls continuation."""
        token = jwt.encode({"sub": "user-1"}, _SECRET, algorithm="HS256")
        interceptor = AuthInterceptor(secret_key=_SECRET)
        details = _FakeCallDetails(
            method="/svc/Method",
            invocation_metadata=(("authorization", f"Bearer {token}"),),
        )
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_expired_token_aborts(self) -> None:
        """Expired token aborts."""
        token = jwt.encode(
            {"sub": "user-1", "exp": int(time.time()) - 10}, _SECRET, algorithm="HS256"
        )
        interceptor = AuthInterceptor(secret_key=_SECRET)
        details = _FakeCallDetails(
            method="/svc/Method",
            invocation_metadata=(("authorization", f"Bearer {token}"),),
        )
        handler = interceptor.intercept_service(_ok_continuation, details)
        with pytest.raises(_AbortError) as exc_info:
            handler.unary_unary(b"x", _FakeContext())
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED
        assert "expired" in exc_info.value.details_.lower()

    def test_invalid_token_aborts(self) -> None:
        """Invalid token aborts."""
        interceptor = AuthInterceptor(secret_key=_SECRET)
        details = _FakeCallDetails(
            method="/svc/Method",
            invocation_metadata=(("authorization", "Bearer not-a-jwt"),),
        )
        handler = interceptor.intercept_service(_ok_continuation, details)
        with pytest.raises(_AbortError) as exc_info:
            handler.unary_unary(b"x", _FakeContext())
        assert exc_info.value.code == grpc.StatusCode.UNAUTHENTICATED
        assert "invalid" in exc_info.value.details_.lower()


class TestRateLimitInterceptor:
    """Sliding-window per-client limiting."""

    def test_under_limit_calls_continuation(self) -> None:
        """Under limit calls continuation."""
        interceptor = RateLimitInterceptor(requests_per_minute=5)
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_exceeding_limit_aborts(self) -> None:
        """Exceeding limit aborts."""
        interceptor = RateLimitInterceptor(requests_per_minute=1)
        details = _FakeCallDetails(method="/svc/Method")
        interceptor.intercept_service(_ok_continuation, details)  # consumes the 1 slot
        handler = interceptor.intercept_service(_ok_continuation, details)
        with pytest.raises(_AbortError) as exc_info:
            handler.unary_unary(b"x", _FakeContext())
        assert exc_info.value.code == grpc.StatusCode.RESOURCE_EXHAUSTED

    def test_per_ip_mode_uses_forwarded_for(self) -> None:
        """Per ip mode uses forwarded for."""
        interceptor = RateLimitInterceptor(requests_per_minute=5, per_user=False)
        details = _FakeCallDetails(
            method="/svc/Method", invocation_metadata=(("x-forwarded-for", "1.2.3.4"),)
        )
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"
        assert "1.2.3.4" in interceptor.limits

    def test_window_resets_after_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Window resets after expiry."""
        interceptor = RateLimitInterceptor(requests_per_minute=1)
        details = _FakeCallDetails(method="/svc/Method")
        interceptor.intercept_service(_ok_continuation, details)
        future = time.time() + 61.0
        monkeypatch.setattr(time, "time", lambda: future)
        handler = interceptor.intercept_service(_ok_continuation, details)
        # Window reset -- should not raise even though the limit was 1.
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_malformed_bearer_token_falls_back_to_anonymous(self) -> None:
        """Malformed bearer token falls back to anonymous."""
        interceptor = RateLimitInterceptor(requests_per_minute=5)
        details = _FakeCallDetails(
            method="/svc/Method",
            invocation_metadata=(("authorization", "Bearer not-a-real-jwt"),),
        )
        interceptor.intercept_service(_ok_continuation, details)
        assert "anonymous" in interceptor.limits

    def test_bearer_token_sub_used_as_client_id(self) -> None:
        """Bearer token sub used as client id."""
        token = jwt.encode({"sub": "user-42"}, _SECRET, algorithm="HS256")
        interceptor = RateLimitInterceptor(requests_per_minute=5)
        details = _FakeCallDetails(
            method="/svc/Method",
            invocation_metadata=(("authorization", f"Bearer {token}"),),
        )
        interceptor.intercept_service(_ok_continuation, details)
        assert "user-42" in interceptor.limits


class TestAuditInterceptor:
    """Request/response logging wrapper."""

    def test_successful_call_is_logged_and_passed_through(self) -> None:
        """Successful call is logged and passed through."""
        interceptor = AuditInterceptor()
        details = _FakeCallDetails(
            method="/svc/Method", invocation_metadata=(("x-correlation-id", "cid-1"),)
        )
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_failing_call_is_logged_and_reraised(self) -> None:
        """Failing call is logged and reraised."""
        def _raising_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
            def _boom(request: Any, context: Any) -> Any:
                raise RuntimeError("boom")

            return grpc.unary_unary_rpc_method_handler(
                _boom, request_deserializer=lambda x: x, response_serializer=lambda x: x
            )

        interceptor = AuditInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_raising_continuation, details)
        with pytest.raises(RuntimeError, match="boom"):
            handler.unary_unary(b"x", _FakeContext())

    def test_non_unary_unary_handler_passed_through_unwrapped(self) -> None:
        """Non unary unary handler passed through unwrapped."""
        def _stream_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
            return grpc.unary_stream_rpc_method_handler(
                lambda request, context: iter([request]),
                request_deserializer=lambda x: x,
                response_serializer=lambda x: x,
            )

        interceptor = AuditInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_stream_continuation, details)
        assert handler.unary_unary is None


class TestRecoveryInterceptor:
    """Exception-to-gRPC-error conversion."""

    def test_successful_call_passed_through(self) -> None:
        """Successful call passed through."""
        interceptor = RecoveryInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_ok_continuation, details)
        assert handler.unary_unary(b"x", _FakeContext()) == b"x"

    def test_rpc_error_passes_through_unconverted(self) -> None:
        """Rpc error passes through unconverted."""
        def _raising_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
            def _boom(request: Any, context: Any) -> Any:
                raise grpc.RpcError()

            return grpc.unary_unary_rpc_method_handler(
                _boom, request_deserializer=lambda x: x, response_serializer=lambda x: x
            )

        interceptor = RecoveryInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_raising_continuation, details)
        with pytest.raises(grpc.RpcError):
            handler.unary_unary(b"x", _FakeContext())

    def test_unexpected_exception_converted_to_internal_abort(self) -> None:
        """Unexpected exception converted to internal abort."""
        def _raising_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
            def _boom(request: Any, context: Any) -> Any:
                raise ValueError("unexpected")

            return grpc.unary_unary_rpc_method_handler(
                _boom, request_deserializer=lambda x: x, response_serializer=lambda x: x
            )

        interceptor = RecoveryInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_raising_continuation, details)
        context = _FakeContext()
        with pytest.raises(_AbortError) as exc_info:
            handler.unary_unary(b"x", context)
        assert exc_info.value.code == grpc.StatusCode.INTERNAL

    def test_non_unary_unary_handler_passed_through_unwrapped(self) -> None:
        """Non unary unary handler passed through unwrapped."""
        def _stream_continuation(handler_call_details: Any) -> grpc.RpcMethodHandler:
            return grpc.unary_stream_rpc_method_handler(
                lambda request, context: iter([request]),
                request_deserializer=lambda x: x,
                response_serializer=lambda x: x,
            )

        interceptor = RecoveryInterceptor()
        details = _FakeCallDetails(method="/svc/Method")
        handler = interceptor.intercept_service(_stream_continuation, details)
        assert handler.unary_unary is None
