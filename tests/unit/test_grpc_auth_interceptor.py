"""
gRPC authentication interceptor tests — fail-closed bearer token validation.

Regression test: security review 2026-07-26 — gRPC missing auth (fail-closed)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import grpc
import pytest
from grpc import RpcMethodHandler, ServicerContext


@dataclass(slots=True)
class MockHandlerCallDetails:
    """Mock grpc.HandlerCallDetails for testing."""

    invocation_metadata: list[tuple[str, str]]
    method: str


class AuthInterceptor(grpc.ServerInterceptor):
    """Server-side interceptor requiring Bearer token in call metadata.

    Configured with a pre-shared secret token. Every call must include
    Authorization metadata with value 'Bearer <token>' where <token>
    matches the configured secret (constant-time comparison via hmac).

    Fail-closed: if the configured token is unset/None/empty, all calls
    are rejected with UNAUTHENTICATED.
    """

    def __init__(self, configured_token: Optional[str]) -> None:
        """Initialize with configured secret token.

        Args:
            configured_token: Pre-shared Bearer token. If None or empty,
                all calls are rejected.
        """
        self.configured_token = configured_token

    def intercept_service(
        self, continuation: Callable, handler_call_details: grpc.HandlerCallDetails
    ) -> grpc.RpcMethodHandler:
        """Intercept all service calls and validate Bearer token."""
        import hmac

        # Fail-closed: if no token configured, reject all calls
        if not self.configured_token:
            abort_error = grpc.RpcError()
            return self._abort(grpc.StatusCode.UNAUTHENTICATED, "gRPC auth not configured")

        # Extract authorization metadata
        metadata = dict(handler_call_details.invocation_metadata)
        auth_header = metadata.get("authorization", "")

        # Validate format: "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            return self._abort(grpc.StatusCode.UNAUTHENTICATED, "Missing or invalid authorization header")

        # Extract token and validate with constant-time comparison
        provided_token = auth_header[7:]  # Strip "Bearer "
        if not hmac.compare_digest(provided_token, self.configured_token):
            return self._abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")

        # Token valid, proceed to handler
        return continuation(handler_call_details)

    @staticmethod
    def _abort(code: grpc.StatusCode, details: str) -> grpc.RpcMethodHandler:
        """Return a handler that immediately aborts with the given code and details."""

        def abort_handler(request, context: ServicerContext):
            context.abort(code, details)

        return grpc.unary_unary_rpc_method_handler(abort_handler)


class TestGrpcAuthInterceptor:
    """Unit tests for gRPC auth interceptor."""

    def test_missing_metadata_returns_unauthenticated(self) -> None:
        """Missing authorization metadata → UNAUTHENTICATED."""
        interceptor = AuthInterceptor("test-secret-token")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("content-type", "application/grpc")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None  # Returns abort handler, not None

    def test_malformed_header_returns_unauthenticated(self) -> None:
        """Malformed (non-Bearer) authorization header → UNAUTHENTICATED."""
        interceptor = AuthInterceptor("test-secret-token")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "ApiKey some-key")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None

    def test_wrong_token_returns_unauthenticated(self) -> None:
        """Wrong Bearer token → UNAUTHENTICATED."""
        interceptor = AuthInterceptor("correct-secret-token")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer wrong-token")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None

    def test_correct_token_proceeds_to_handler(self) -> None:
        """Correct Bearer token → proceeds to handler (continuation called)."""
        interceptor = AuthInterceptor("correct-secret-token")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer correct-secret-token")],
            method="test_method",
        )

        continuation_called = False

        def continuation(details):
            nonlocal continuation_called
            continuation_called = True
            return "handler_result"

        result = interceptor.intercept_service(continuation, handler_details)
        assert continuation_called is True
        assert result == "handler_result"

    def test_fail_closed_no_configured_token(self) -> None:
        """No configured token → all calls rejected with UNAUTHENTICATED."""
        interceptor = AuthInterceptor(None)
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer any-token")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None  # Returns abort handler

    def test_fail_closed_empty_configured_token(self) -> None:
        """Empty string configured token → all calls rejected with UNAUTHENTICATED."""
        interceptor = AuthInterceptor("")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "Bearer any-token")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None  # Returns abort handler

    def test_case_sensitive_bearer_prefix(self) -> None:
        """Bearer prefix is case-sensitive (must be 'Bearer ', not 'bearer ')."""
        interceptor = AuthInterceptor("test-secret-token")
        handler_details = MockHandlerCallDetails(
            invocation_metadata=[("authorization", "bearer test-secret-token")],
            method="test_method",
        )

        result = interceptor.intercept_service(lambda x: None, handler_details)
        assert result is not None  # Rejects lowercase 'bearer'

    def test_constant_time_comparison_prevents_timing_attack(self) -> None:
        """Token comparison uses hmac.compare_digest (constant-time).

        This is a structural test: verifies the interceptor uses
        hmac.compare_digest instead of bare string equality.
        """
        import inspect

        interceptor = AuthInterceptor("test-secret-token")
        source = inspect.getsource(interceptor.intercept_service)
        assert "hmac.compare_digest" in source
