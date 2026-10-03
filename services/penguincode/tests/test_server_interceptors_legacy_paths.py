"""Coverage completion for `server.interceptors`'s pre-existing, previously-untested
branches (`JWTValidationInterceptor`'s reject paths, `LoggingInterceptor`) -- this
file's Tier-A coverage gate (`scripts/coverage_gate.py`) is whole-file, so these
branches need direct coverage alongside the new `TracingInterceptor` tests in
`test_server_tracing_interceptor.py`.

Not a regression marker for new behavior -- backfill for existing code this
change's edits to the file brought into scope.
"""

from __future__ import annotations

from typing import Any

import jwt as pyjwt
import pytest

from penguincode_cli.server.interceptors import JWTValidationInterceptor, LoggingInterceptor

SECRET = "a-legacy-hs256-secret-that-is-long-enough"  # nosec B105 -- test fixture, not a real secret


class _FakeHandlerCallDetails:
    def __init__(self, method: str, invocation_metadata: Any = ()) -> None:
        self.method = method
        self.invocation_metadata = invocation_metadata


class _FakeAbortContext:
    def __init__(self) -> None:
        self.aborted_with: tuple[Any, str] | None = None

    async def abort(self, code: Any, message: str) -> None:
        self.aborted_with = (code, message)
        raise RuntimeError("aborted")


async def _invoke_and_capture_abort(
    interceptor: Any, method: str, token: str | None
) -> tuple[Any, str]:
    metadata = (("authorization", f"Bearer {token}"),) if token else ()
    details = _FakeHandlerCallDetails(method, metadata)

    async def continuation(_details: Any) -> str:
        return "handler-result"

    result = await interceptor.intercept_service(continuation, details)
    fake_context = _FakeAbortContext()
    with pytest.raises(RuntimeError):
        await result.unary_unary(object(), fake_context)
    assert fake_context.aborted_with is not None
    return fake_context.aborted_with


class TestJWTValidationInterceptorRejectPaths:
    @pytest.mark.asyncio
    async def test_non_access_token_type_is_rejected(self) -> None:
        interceptor = JWTValidationInterceptor(jwt_secret=SECRET)
        token = pyjwt.encode({"sub": "user-1", "type": "refresh"}, SECRET, algorithm="HS256")
        _, message = await _invoke_and_capture_abort(interceptor, "/svc/Method", token)
        assert "Invalid token type" in message

    @pytest.mark.asyncio
    async def test_expired_token_is_rejected(self) -> None:
        interceptor = JWTValidationInterceptor(jwt_secret=SECRET)
        token = pyjwt.encode(
            {"sub": "user-1", "type": "access", "exp": 1},  # far in the past
            SECRET,
            algorithm="HS256",
        )
        _, message = await _invoke_and_capture_abort(interceptor, "/svc/Method", token)
        assert "Token expired" in message

    @pytest.mark.asyncio
    async def test_malformed_token_is_rejected(self) -> None:
        interceptor = JWTValidationInterceptor(jwt_secret=SECRET)
        _, message = await _invoke_and_capture_abort(interceptor, "/svc/Method", "not-a-real-jwt")
        assert "Invalid token" in message


class TestLoggingInterceptor:
    @pytest.mark.asyncio
    async def test_logs_and_passes_through_on_success(self) -> None:
        interceptor = LoggingInterceptor()
        details = _FakeHandlerCallDetails("/svc/Method")

        async def continuation(_details: Any) -> str:
            return "ok"

        result = await interceptor.intercept_service(continuation, details)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_logs_and_reraises_on_failure(self) -> None:
        interceptor = LoggingInterceptor()
        details = _FakeHandlerCallDetails("/svc/Method")

        async def continuation(_details: Any) -> str:
            raise RuntimeError("downstream failure")

        with pytest.raises(RuntimeError, match="downstream failure"):
            await interceptor.intercept_service(continuation, details)
