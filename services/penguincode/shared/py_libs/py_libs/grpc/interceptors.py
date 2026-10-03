"""gRPC security interceptors for authentication, rate limiting, and audit logging.
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

import grpc
import jwt
from opentelemetry import baggage, metrics, trace
from opentelemetry import context as otel_context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

# W3C TraceContext (``traceparent``/``tracestate``) + Baggage (``baggage``) propagator
# used for every gRPC boundary this module touches. Deliberately local to this module
# (rather than reusing a process-wide global textmap propagator) so py_libs stays a
# self-contained, pip-installable package with no dependency on any consuming app's
# tracing bootstrap.
_PROPAGATOR = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])

_TRACER = trace.get_tracer("py_libs.grpc")
_METER = metrics.get_meter("py_libs.grpc")

#: Plain metadata key carrying the correlation id, kept alongside OTel baggage so
#: log lines (which don't parse baggage) can still correlate across services.
_CORRELATION_METADATA_KEY = "x-correlation-id"
_CORRELATION_BAGGAGE_KEY = "correlation_id"

# rpc.* bounded labels only -- never ids/paths/emails (critical-rules.md Observability).
_RPC_CLIENT_DURATION = _METER.create_histogram(
    name="rpc_client_duration_seconds",
    description="Duration of outgoing gRPC client calls",
    unit="s",
)
_RPC_CLIENT_REQUESTS = _METER.create_counter(
    name="rpc_client_requests_total",
    description="Count of outgoing gRPC client calls",
)


def _split_method(method: str | bytes | None) -> tuple[str, str]:
    """Split a gRPC full method string ``/package.Service/Method`` into (service, method).

    ``grpc.aio`` passes ``ClientCallDetails.method`` as ``bytes``; the sync API uses
    ``str`` -- normalize before splitting so both variants share this helper.
    """
    if not method:
        return "unknown", "unknown"
    if isinstance(method, bytes):
        method = method.decode("utf-8", errors="replace")
    parts = method.lstrip("/").split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "unknown", parts[0]


def _get_or_mint_correlation_id(
    metadata: Any,
    ctx: otel_context.Context,
) -> str:
    """Reuse an inbound correlation id (metadata or baggage); mint a UUID only if absent."""
    existing = dict(metadata or ()).get(_CORRELATION_METADATA_KEY)
    if existing:
        return str(existing)
    from_baggage = baggage.get_baggage(_CORRELATION_BAGGAGE_KEY, context=ctx)
    if from_baggage:
        return str(from_baggage)
    return str(uuid.uuid4())


def _merge_metadata(existing: Any, extra: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Append ``extra`` key/value pairs onto an existing gRPC metadata tuple."""
    merged = list(existing or ())
    merged.extend(extra.items())
    return tuple(merged)


def _status_code_name(code: grpc.StatusCode | None) -> str:
    """Render a gRPC status code as its bounded label name, defaulting to ``OK``."""
    return code.name if code is not None else "OK"


class AuthInterceptor(grpc.ServerInterceptor):
    """JWT authentication interceptor for gRPC servers.

    Validates JWT tokens in metadata and sets user context.
    """

    def __init__(
        self,
        secret_key: str,
        algorithms: list[str] | None = None,
        public_methods: set[str] | None = None,
    ):
        """Initialize auth interceptor.

        Args:
            secret_key: JWT secret key for validation
            algorithms: List of allowed JWT algorithms (default: ['HS256'])
            public_methods: Set of method names that don't require auth

        """
        self.secret_key = secret_key
        self.algorithms = algorithms or ["HS256"]
        self.public_methods = public_methods or set()

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Intercept and validate authentication."""
        method = handler_call_details.method

        # Skip auth for public methods
        if method in self.public_methods:
            return continuation(handler_call_details)

        # Extract token from metadata
        metadata = dict(handler_call_details.invocation_metadata)
        auth_header = metadata.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            logger.warning(f"Missing or invalid auth header for {method}")
            return self._abort_with_error(
                grpc.StatusCode.UNAUTHENTICATED,
                "Missing or invalid authorization header",
            )

        token = auth_header[7:]  # Remove 'Bearer ' prefix

        try:
            # Validate JWT token
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=self.algorithms,
            )

            # Add user info to context (can be retrieved in handlers)
            user_id = payload.get("sub")
            logger.info(f"Authenticated request to {method}", extra={"user_id": user_id})

            return continuation(handler_call_details)

        except jwt.ExpiredSignatureError:
            logger.warning(f"Expired token for {method}")
            return self._abort_with_error(grpc.StatusCode.UNAUTHENTICATED, "Token has expired")
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid token for {method}: {e}")
            return self._abort_with_error(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")

    def _abort_with_error(
        self,
        code: grpc.StatusCode,
        details: str,
    ) -> grpc.RpcMethodHandler:
        """Return an RPC handler that aborts with error."""

        def abort(request: Any, context: grpc.ServicerContext) -> None:
            context.abort(code, details)

        return grpc.unary_unary_rpc_method_handler(
            abort,
            request_deserializer=lambda x: x,
            response_serializer=lambda x: x,
        )


@dataclass(slots=True)
class RateLimitEntry:
    """Track rate limit for a client."""

    count: int = 0
    window_start: float = 0.0


class RateLimitInterceptor(grpc.ServerInterceptor):
    """Rate limiting interceptor with per-client limits.

    Implements sliding window rate limiting.
    """

    def __init__(
        self,
        requests_per_minute: int = 100,
        per_user: bool = True,
    ):
        """Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
            per_user: Rate limit per user (True) or per IP (False)

        """
        self.requests_per_minute = requests_per_minute
        self.per_user = per_user
        self.limits: dict[str, RateLimitEntry] = defaultdict(RateLimitEntry)
        self.lock = Lock()

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Intercept and check rate limits."""
        # Determine client identifier
        metadata = dict(handler_call_details.invocation_metadata)

        if self.per_user:
            # Extract user from token
            auth_header = metadata.get("authorization", "")
            if auth_header.startswith("Bearer "):
                try:
                    token = auth_header[7:]
                    payload = jwt.decode(token, options={"verify_signature": False})
                    client_id = payload.get("sub", "anonymous")
                except Exception:
                    client_id = "anonymous"
            else:
                client_id = "anonymous"
        else:
            # Use peer address (IP)
            client_id = metadata.get("x-forwarded-for", "unknown")

        # Check rate limit
        current_time = time.time()

        with self.lock:
            entry = self.limits[client_id]

            # Reset window if expired
            if current_time - entry.window_start >= 60.0:
                entry.count = 0
                entry.window_start = current_time

            # Check limit
            if entry.count >= self.requests_per_minute:
                logger.warning(
                    f"Rate limit exceeded for {client_id}",
                    extra={
                        "client_id": client_id,
                        "requests": entry.count,
                    },
                )
                return self._abort_with_error(grpc.StatusCode.RESOURCE_EXHAUSTED, "Rate limit exceeded")

            # Increment counter
            entry.count += 1

        return continuation(handler_call_details)

    def _abort_with_error(
        self,
        code: grpc.StatusCode,
        details: str,
    ) -> grpc.RpcMethodHandler:
        """Return an RPC handler that aborts with error."""

        def abort(request: Any, context: grpc.ServicerContext) -> None:
            context.abort(code, details)

        return grpc.unary_unary_rpc_method_handler(
            abort,
            request_deserializer=lambda x: x,
            response_serializer=lambda x: x,
        )


class AuditInterceptor(grpc.ServerInterceptor):
    """Audit logging interceptor for request/response tracking.

    Logs method calls, duration, and status codes.
    """

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Intercept and log requests."""
        method = handler_call_details.method
        start_time = time.time()

        # Get correlation ID from metadata
        metadata = dict(handler_call_details.invocation_metadata)
        correlation_id = metadata.get("x-correlation-id", "unknown")

        logger.info(
            f"gRPC request started: {method}",
            extra={
                "method": method,
                "correlation_id": correlation_id,
            },
        )

        handler = continuation(handler_call_details)

        # Wrap handler to log completion
        if handler and handler.unary_unary:
            original_handler = handler.unary_unary

            def logged_handler(request: Any, context: grpc.ServicerContext) -> Any:
                try:
                    response = original_handler(request, context)
                    duration_ms = (time.time() - start_time) * 1000

                    logger.info(
                        f"gRPC request completed: {method}",
                        extra={
                            "method": method,
                            "duration_ms": duration_ms,
                            "correlation_id": correlation_id,
                            "status": "OK",
                        },
                    )
                    return response

                except Exception as e:
                    duration_ms = (time.time() - start_time) * 1000

                    logger.error(
                        f"gRPC request failed: {method}",
                        extra={
                            "method": method,
                            "duration_ms": duration_ms,
                            "correlation_id": correlation_id,
                            "error": str(e),
                        },
                        exc_info=True,
                    )
                    raise

            return grpc.unary_unary_rpc_method_handler(
                logged_handler,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        return handler


class CorrelationInterceptor(grpc.ServerInterceptor):
    """Server-side W3C trace-context + baggage propagator and correlation-id continuity.

    Extracts an inbound ``traceparent``/``baggage`` carrier from gRPC metadata so the
    server-side span becomes a CHILD of the caller's CLIENT span (see
    ``TracingClientInterceptor``/``AsyncTracingClientInterceptor``), and opens that
    SERVER span for the handler's duration. The correlation id is reused from an
    inbound ``x-correlation-id`` metadata entry or baggage, and minted only when
    neither is present -- any server registering this interceptor gets propagation
    for free.
    """

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Extract trace context/baggage and wrap the handler in a SERVER span."""
        method = handler_call_details.method
        metadata = dict(handler_call_details.invocation_metadata)

        parent_ctx = _PROPAGATOR.extract(metadata)

        correlation_id = metadata.get(_CORRELATION_METADATA_KEY)
        if not correlation_id:
            correlation_id = baggage.get_baggage(_CORRELATION_BAGGAGE_KEY, context=parent_ctx)
        if not correlation_id:
            correlation_id = str(uuid.uuid4())
            logger.debug(f"Generated new correlation ID: {correlation_id}")
        parent_ctx = baggage.set_baggage(
            _CORRELATION_BAGGAGE_KEY, correlation_id, context=parent_ctx
        )

        handler = continuation(handler_call_details)
        if not handler or not handler.unary_unary:
            # Non-unary-unary handlers (streaming) are not yet wrapped -- same
            # limitation as AuditInterceptor above; context is still extracted,
            # it's just not attached around the handler body for those RPC types.
            return handler

        service_name, method_name = _split_method(method)
        original_handler = handler.unary_unary

        def traced_handler(request: Any, context: grpc.ServicerContext) -> Any:
            token = otel_context.attach(parent_ctx)
            try:
                with _TRACER.start_as_current_span(
                    f"{service_name}/{method_name}",
                    context=parent_ctx,
                    kind=trace.SpanKind.SERVER,
                    attributes={
                        "rpc.system": "grpc",
                        "rpc.service": service_name,
                        "rpc.method": method_name,
                    },
                ) as span:
                    try:
                        response = original_handler(request, context)
                        span.set_attribute("rpc.grpc.status_code", "OK")
                        return response
                    except grpc.RpcError as exc:
                        code = exc.code() if hasattr(exc, "code") else None
                        status_name = _status_code_name(code) if code else "UNKNOWN"
                        span.set_attribute("rpc.grpc.status_code", status_name)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, status_name))
                        raise
                    except Exception as exc:
                        span.set_attribute("rpc.grpc.status_code", "INTERNAL")
                        span.record_exception(exc)
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                        raise
            finally:
                otel_context.detach(token)

        return grpc.unary_unary_rpc_method_handler(
            traced_handler,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


class TracingClientInterceptor(grpc.UnaryUnaryClientInterceptor):
    """Sync gRPC client interceptor for trace propagation and RPC metrics.

    Injects W3C trace context + baggage, emits a CLIENT span, and records
    ``rpc_client_duration_seconds``/``rpc_client_requests_total``. Use via
    ``grpc.intercept_channel(channel, TracingClientInterceptor())`` on any synchronous
    ``grpc.Channel``. Pairs with ``CorrelationInterceptor`` on the server side so the
    server span is a child of this client span.
    """

    def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.ClientCallDetails, Any], grpc.Call],
        client_call_details: grpc.ClientCallDetails,
        request: Any,
    ) -> grpc.Call:
        """Inject propagation headers, open a CLIENT span, and record metrics."""
        service_name, method_name = _split_method(client_call_details.method)
        parent_ctx = otel_context.get_current()
        correlation_id = _get_or_mint_correlation_id(client_call_details.metadata, parent_ctx)
        span_ctx = baggage.set_baggage(_CORRELATION_BAGGAGE_KEY, correlation_id, parent_ctx)
        token = otel_context.attach(span_ctx)
        status_name = "OK"
        start = time.monotonic()
        try:
            with _TRACER.start_as_current_span(
                f"{service_name}/{method_name}",
                kind=trace.SpanKind.CLIENT,
                attributes={
                    "rpc.system": "grpc",
                    "rpc.service": service_name,
                    "rpc.method": method_name,
                },
            ) as span:
                carrier: dict[str, str] = {_CORRELATION_METADATA_KEY: correlation_id}
                _PROPAGATOR.inject(carrier)
                new_details = client_call_details._replace(
                    metadata=_merge_metadata(client_call_details.metadata, carrier)
                )
                call = continuation(new_details, request)
                try:
                    status_name = _status_code_name(call.code())
                except Exception:  # noqa: BLE001 -- status lookup must never break the call
                    status_name = "UNKNOWN"
                span.set_attribute("rpc.grpc.status_code", status_name)
                if status_name not in ("OK", "UNKNOWN"):
                    span.set_status(trace.Status(trace.StatusCode.ERROR, status_name))
                return call
        finally:
            duration = time.monotonic() - start
            attrs = {"service": service_name, "method": method_name, "status_code": status_name}
            _RPC_CLIENT_DURATION.record(duration, attrs)
            _RPC_CLIENT_REQUESTS.add(1, attrs)
            otel_context.detach(token)


class AsyncTracingClientInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """Async (``grpc.aio``) counterpart to ``TracingClientInterceptor``.

    Same propagation/span/metric contract for async channels
    (``grpc.aio.insecure_channel``/``secure_channel``), awaited instead of blocking.
    """

    async def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.aio.ClientCallDetails, Any], Any],
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Inject propagation headers, open a CLIENT span, and record metrics (async)."""
        service_name, method_name = _split_method(client_call_details.method)
        parent_ctx = otel_context.get_current()
        correlation_id = _get_or_mint_correlation_id(client_call_details.metadata, parent_ctx)
        span_ctx = baggage.set_baggage(_CORRELATION_BAGGAGE_KEY, correlation_id, parent_ctx)
        token = otel_context.attach(span_ctx)
        status_name = "OK"
        start = time.monotonic()
        try:
            with _TRACER.start_as_current_span(
                f"{service_name}/{method_name}",
                kind=trace.SpanKind.CLIENT,
                attributes={
                    "rpc.system": "grpc",
                    "rpc.service": service_name,
                    "rpc.method": method_name,
                },
            ) as span:
                carrier: dict[str, str] = {_CORRELATION_METADATA_KEY: correlation_id}
                _PROPAGATOR.inject(carrier)
                new_details = client_call_details._replace(
                    metadata=_merge_metadata(client_call_details.metadata, carrier)
                )
                call = await continuation(new_details, request)
                try:
                    status_name = _status_code_name(await call.code())
                except Exception:  # noqa: BLE001 -- status lookup must never break the call
                    status_name = "UNKNOWN"
                span.set_attribute("rpc.grpc.status_code", status_name)
                if status_name not in ("OK", "UNKNOWN"):
                    span.set_status(trace.Status(trace.StatusCode.ERROR, status_name))
                return call
        finally:
            duration = time.monotonic() - start
            attrs = {"service": service_name, "method": method_name, "status_code": status_name}
            _RPC_CLIENT_DURATION.record(duration, attrs)
            _RPC_CLIENT_REQUESTS.add(1, attrs)
            otel_context.detach(token)


class RecoveryInterceptor(grpc.ServerInterceptor):
    """Recovery interceptor for exception handling.

    Catches unexpected exceptions and returns proper gRPC errors.
    """

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Intercept and handle exceptions."""
        handler = continuation(handler_call_details)

        if handler and handler.unary_unary:
            original_handler = handler.unary_unary

            def recovery_handler(request: Any, context: grpc.ServicerContext) -> Any:
                try:
                    return original_handler(request, context)

                except grpc.RpcError:
                    # Let gRPC errors pass through
                    raise

                except Exception as e:
                    # Convert unexpected exceptions to gRPC errors
                    method = handler_call_details.method
                    error_trace = traceback.format_exc()

                    logger.error(
                        f"Unexpected error in {method}",
                        extra={
                            "method": method,
                            "error": str(e),
                            "trace": error_trace,
                        },
                        exc_info=True,
                    )

                    context.abort(grpc.StatusCode.INTERNAL, f"Internal server error: {str(e)}")

            return grpc.unary_unary_rpc_method_handler(
                recovery_handler,
                request_deserializer=handler.request_deserializer,
                response_serializer=handler.response_serializer,
            )

        return handler
