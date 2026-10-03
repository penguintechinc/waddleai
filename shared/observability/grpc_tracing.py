"""Server-side gRPC trace-context extraction (ops O1-d).

Pairs with the client-side interceptors in ``py_libs.grpc.interceptors``
(penguincode), ``services.management.app.grpc.client`` (management -> AILB), and
``penguincode_cli.client.tracing_interceptor`` (CLI -> penguincode server): any of
those inject W3C ``traceparent`` + baggage into outgoing gRPC metadata; this module
is the generic extract/attach half any **sync** ``grpc.server`` in this monorepo
(currently: the proxy's gRPC server) can register to turn that into a real SERVER
span that is a child of the caller's CLIENT span, instead of a detached root.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import grpc
from opentelemetry.trace import SpanKind, Status, StatusCode

from shared.observability.tracing import extract_context, get_tracer

_CORRELATION_METADATA_KEY = "x-correlation-id"

_rpc_server_duration: Any = None
_rpc_server_requests: Any = None


def _split_method(method: str | None) -> tuple[str, str]:
    """Split a gRPC full method string ``/package.Service/Method`` into (service, method)."""
    if not method:
        return "unknown", "unknown"
    parts = method.lstrip("/").split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("unknown", parts[0])


def _server_instruments() -> tuple[Any, Any]:
    """Lazily create (and cache) the RPC server instruments on the process meter."""
    global _rpc_server_duration, _rpc_server_requests
    if _rpc_server_duration is None or _rpc_server_requests is None:
        from shared.observability.metrics import get_meter

        meter = get_meter()
        _rpc_server_duration = meter.create_histogram(
            "rpc_server_duration_seconds",
            unit="s",
            description="Duration of inbound gRPC server calls",
        )
        _rpc_server_requests = meter.create_counter(
            "rpc_server_requests_total",
            unit="1",
            description="Count of inbound gRPC server calls",
        )
    return _rpc_server_duration, _rpc_server_requests


def reset_instruments_for_testing() -> None:
    """Drop cached RPC server instruments so a test can install a fresh MeterProvider."""
    global _rpc_server_duration, _rpc_server_requests
    _rpc_server_duration = None
    _rpc_server_requests = None


class TracingServerInterceptor(grpc.ServerInterceptor):
    """Extracts W3C trace context from inbound metadata and opens a SERVER span.

    Any caller using this monorepo's gRPC client interceptors (py_libs, management's
    AILB client, or the penguincode CLI client) now produces a true parent/child span
    pair once the server registers this interceptor -- previously the correlation id
    was generated server-side and never linked to the caller's trace at all.
    """

    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Extract trace context and wrap the handler in a SERVER span."""
        method = handler_call_details.method
        metadata = dict(handler_call_details.invocation_metadata)
        parent_ctx = extract_context(metadata)
        correlation_id = metadata.get(_CORRELATION_METADATA_KEY) or str(uuid.uuid4())

        handler = continuation(handler_call_details)
        if not handler or not handler.unary_unary:
            return handler

        service_name, method_name = _split_method(method)
        original_handler = handler.unary_unary
        tracer = get_tracer()
        duration_hist, request_counter = _server_instruments()

        def traced_handler(request: Any, context: grpc.ServicerContext) -> Any:
            status_name = "OK"
            start = time.monotonic()
            try:
                with tracer.start_as_current_span(
                    f"{service_name}/{method_name}",
                    context=parent_ctx,
                    kind=SpanKind.SERVER,
                    attributes={
                        "rpc.system": "grpc",
                        "rpc.service": service_name,
                        "rpc.method": method_name,
                        "correlation_id": correlation_id,
                    },
                ) as span:
                    try:
                        response = original_handler(request, context)
                        span.set_attribute("rpc.grpc.status_code", "OK")
                        return response
                    except grpc.RpcError as exc:
                        code = exc.code() if hasattr(exc, "code") else None
                        status_name = code.name if code else "UNKNOWN"
                        span.set_attribute("rpc.grpc.status_code", status_name)
                        span.set_status(Status(StatusCode.ERROR, status_name))
                        raise
                    except Exception as exc:
                        status_name = "INTERNAL"
                        span.set_attribute("rpc.grpc.status_code", status_name)
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        raise
            finally:
                duration_hist.record(
                    time.monotonic() - start,
                    {
                        "service": service_name,
                        "method": method_name,
                        "status_code": status_name,
                    },
                )
                request_counter.add(
                    1,
                    {
                        "service": service_name,
                        "method": method_name,
                        "status_code": status_name,
                    },
                )

        return grpc.unary_unary_rpc_method_handler(
            traced_handler,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )
