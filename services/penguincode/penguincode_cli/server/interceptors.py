"""gRPC interceptors for authentication and request processing."""

import logging
from collections.abc import Callable

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
