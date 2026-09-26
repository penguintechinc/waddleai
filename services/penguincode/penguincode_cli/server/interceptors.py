"""gRPC interceptors for authentication and request processing."""

import logging
from collections.abc import Callable
from typing import Any

import grpc
import jwt

logger = logging.getLogger(__name__)


class JWTValidationInterceptor(grpc.aio.ServerInterceptor):
    """Interceptor that validates JWT tokens on incoming requests.

    Extracts token from 'authorization' metadata and validates it.
    Skips validation for excluded methods (e.g., Authenticate, Health).
    """

    def __init__(
        self,
        jwt_secret: str,
        excluded_methods: list[str] | None = None,
    ):
        self.jwt_secret = jwt_secret
        self.excluded_methods = set(excluded_methods or [])

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        """Intercept and validate requests."""
        method = handler_call_details.method

        # Skip validation for excluded methods
        if method in self.excluded_methods:
            return await continuation(handler_call_details)

        # Extract authorization header
        metadata = dict(handler_call_details.invocation_metadata or [])
        auth_header = metadata.get("authorization", "")

        if not auth_header:
            return self._unauthenticated_handler("Missing authorization header")

        # Extract token from "Bearer <token>"
        if not auth_header.startswith("Bearer "):
            return self._unauthenticated_handler("Invalid authorization format")

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Validate token
        try:
            claims = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
            )

            if claims.get("type") != "access":
                return self._unauthenticated_handler("Invalid token type")

            # Token is valid, continue with request
            logger.debug(f"Authenticated request from {claims.get('sub')} to {method}")
            return await continuation(handler_call_details)

        except jwt.ExpiredSignatureError:
            return self._unauthenticated_handler("Token expired")
        except jwt.InvalidTokenError as e:
            return self._unauthenticated_handler(f"Invalid token: {e}")

    def _unauthenticated_handler(self, message: str):
        """Return a handler that rejects the request."""

        async def abort_handler(request, context):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                message,
            )

        return grpc.unary_unary_rpc_method_handler(abort_handler)


class PassthroughInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any -- identical to every other subclass in this file.
    """No-op interceptor: every call goes straight through to its real handler.

    Used as `MethodPrefixRoutingInterceptor`'s "unmatched" branch (see below)
    when penguincode's local HS256 auth is disabled
    (``settings.auth.enabled=False``) but `KnowledgeService`'s RS256 gate must
    still be installed unconditionally -- see ``server/main.py``'s module
    docstring ("Interceptor reconciliation").
    """

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Delegate unconditionally to *continuation* -- no auth check at all."""
        return await continuation(handler_call_details)


class MethodPrefixRoutingInterceptor(grpc.aio.ServerInterceptor):  # type: ignore[misc]
    # grpc ships no type stubs (no types-grpcio pin here), so ServerInterceptor
    # resolves to Any -- identical to every other subclass in this file.
    """Routes each call to one of two interceptors, chosen by its method path's prefix.

    Reconciles two interceptors that would otherwise both claim the same
    ``authorization`` invocation-metadata key for two different token kinds --
    penguincode's local HS256 client-server secret (`JWTValidationInterceptor`)
    vs. a WaddleAI-issued RS256 JWT (`auth.middleware.WaddleAIAuthInterceptor`)
    -- see ``server/main.py``'s "Interceptor reconciliation" module docstring
    note for the full rationale.

    A call whose method starts with *prefix* is gated by *matched*; every
    other call is gated by *unmatched*. Exactly one interceptor ever sees a
    given call, so neither needs its own ``excluded_methods`` to enumerate
    the other's methods -- new RPCs on either side of the split need no
    change here as long as their method path keeps the same prefix
    convention.
    """

    def __init__(
        self,
        prefix: str,
        *,
        matched: grpc.aio.ServerInterceptor,
        unmatched: grpc.aio.ServerInterceptor,
    ) -> None:
        """Bind this router to *prefix*, dispatching to *matched*/*unmatched* accordingly."""
        self._prefix = prefix
        self._matched = matched
        self._unmatched = unmatched

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> Any:
        """Dispatch to whichever interceptor owns *handler_call_details*'s method."""
        target = (
            self._matched
            if handler_call_details.method.startswith(self._prefix)
            else self._unmatched
        )
        return await target.intercept_service(continuation, handler_call_details)


class LoggingInterceptor(grpc.aio.ServerInterceptor):
    """Interceptor that logs all requests."""

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ):
        """Log request details."""
        method = handler_call_details.method
        logger.info(f"Request: {method}")

        try:
            result = await continuation(handler_call_details)
            logger.info(f"Completed: {method}")
            return result
        except Exception as e:
            logger.error(f"Error in {method}: {e}")
            raise
