"""Async (grpc.aio) client interceptor propagating trace context to the server.

ops O1-d: the CLI->server gRPC channel (``client/grpc_client.py``) previously carried
zero trace context, so a chat/session/tool-callback call never appeared as a child
span of anything the server did. This module provides the client-side half of that
fix -- the server-side extract/attach is owned by a sibling task
(``penguincode_cli/server/*``) and is NOT touched here.

Deliberately self-contained (its own W3C TraceContext + Baggage propagator) rather
than reaching into ``penguincode_cli.observability.otel``'s global propagator state,
which this module does not own or mutate -- it only reads that module's already
publicly-exported ``get_tracer()``/``get_meter()`` so client spans/metrics land in
the same OTLP pipeline as the rest of the CLI.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import grpc
import grpc.aio
from opentelemetry import baggage
from opentelemetry import context as otel_context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from penguincode_cli.flags.client import is_enabled
from penguincode_cli.observability.otel import get_meter, get_tracer

#: ops O1-d opt-out kill-switch (unseen/OFF = propagation ON). Follows this
#: module's own product's flag-key convention (``penguincode.*``, see
#: ``flags/client.py``) rather than ``waddleai.*`` -- penguincode is deliberately
#: standalone and does not share a flag namespace with the rest of the monorepo.
DISABLE_TRACE_PROPAGATION_FLAG = "penguincode.disable-grpc-trace-propagation"

_CORRELATION_METADATA_KEY = "x-correlation-id"
_CORRELATION_BAGGAGE_KEY = "correlation_id"

_PROPAGATOR = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])

_rpc_client_duration: Any = None
_rpc_client_requests: Any = None


@dataclass(frozen=True, slots=True)
class _ClientScopeContext:
    """Minimal ``ScopeContextLike`` for flag evaluation at the client-channel level.

    There is no request-scoped tenant at channel-creation time (unlike a server
    handler) -- this is a process-level infra kill-switch, not a per-tenant feature,
    so it is evaluated once against a fixed, non-PII distinct id.
    """

    tenant_id: str = "penguincode-cli"
    org_id: str | None = None
    team_ids: tuple[str, ...] = ()
    user_id: str = "penguincode-cli"
    scopes: tuple[str, ...] = ()


_FLAG_CTX = _ClientScopeContext()


def _split_method(method: str | bytes | None) -> tuple[str, str]:
    """Split a gRPC full method string ``/package.Service/Method`` into (service, method)."""
    if not method:
        return "unknown", "unknown"
    if isinstance(method, bytes):
        method = method.decode("utf-8", errors="replace")
    parts = method.lstrip("/").split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("unknown", parts[0])


def _client_instruments() -> tuple[Any, Any]:
    """Lazily create (and cache) the RPC client instruments on the process meter."""
    global _rpc_client_duration, _rpc_client_requests
    if _rpc_client_duration is None or _rpc_client_requests is None:
        meter = get_meter()
        _rpc_client_duration = meter.create_histogram(
            "rpc_client_duration_seconds",
            unit="s",
            description="Duration of outgoing gRPC client calls (CLI -> server)",
        )
        _rpc_client_requests = meter.create_counter(
            "rpc_client_requests_total",
            unit="1",
            description="Count of outgoing gRPC client calls (CLI -> server)",
        )
    return _rpc_client_duration, _rpc_client_requests


def reset_instruments_for_testing() -> None:
    """Drop cached RPC client instruments so a test can install a fresh MeterProvider."""
    global _rpc_client_duration, _rpc_client_requests
    _rpc_client_duration = None
    _rpc_client_requests = None


class TracingClientInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    """``grpc.aio`` client interceptor: trace/baggage propagation + RPC metrics.

    Injects W3C trace context + baggage (including a correlation id, reused from
    an incoming one or minted fresh) into outgoing metadata, opens a CLIENT span,
    and records ``rpc_client_duration_seconds``/``rpc_client_requests_total``.
    No-ops when ``penguincode.disable-grpc-trace-propagation`` is ON.
    """

    async def intercept_unary_unary(
        self,
        continuation: Callable[[grpc.aio.ClientCallDetails, Any], Any],
        client_call_details: grpc.aio.ClientCallDetails,
        request: Any,
    ) -> Any:
        """Inject propagation headers, open a CLIENT span, and record metrics."""
        if is_enabled(DISABLE_TRACE_PROPAGATION_FLAG, _FLAG_CTX):
            return await continuation(client_call_details, request)

        service_name, method_name = _split_method(client_call_details.method)
        parent_ctx = otel_context.get_current()
        existing = dict(client_call_details.metadata or ()).get(_CORRELATION_METADATA_KEY)
        correlation_id = existing or baggage.get_baggage(
            _CORRELATION_BAGGAGE_KEY, context=parent_ctx
        )
        correlation_id = str(correlation_id) if correlation_id else str(uuid.uuid4())
        span_ctx = baggage.set_baggage(_CORRELATION_BAGGAGE_KEY, correlation_id, parent_ctx)
        token = otel_context.attach(span_ctx)

        tracer = get_tracer()
        status_name = "OK"
        start = time.monotonic()
        duration_hist, request_counter = _client_instruments()
        try:
            with tracer.start_as_current_span(
                f"{service_name}/{method_name}",
                kind=SpanKind.CLIENT,
                attributes={
                    "rpc.system": "grpc",
                    "rpc.service": service_name,
                    "rpc.method": method_name,
                },
            ) as span:
                carrier: dict[str, str] = {_CORRELATION_METADATA_KEY: correlation_id}
                _PROPAGATOR.inject(carrier)
                merged = list(client_call_details.metadata or ())
                merged.extend(carrier.items())
                new_details = client_call_details._replace(metadata=tuple(merged))
                call = await continuation(new_details, request)
                try:
                    code = await call.code()
                    status_name = code.name if code is not None else "OK"
                except Exception:  # noqa: BLE001 -- status lookup must never break the call
                    status_name = "UNKNOWN"
                span.set_attribute("rpc.grpc.status_code", status_name)
                if status_name not in ("OK", "UNKNOWN"):
                    span.set_status(Status(StatusCode.ERROR, status_name))
                return call
        finally:
            duration_hist.record(
                time.monotonic() - start,
                {"service": service_name, "method": method_name, "status_code": status_name},
            )
            request_counter.add(
                1, {"service": service_name, "method": method_name, "status_code": status_name}
            )
            otel_context.detach(token)
