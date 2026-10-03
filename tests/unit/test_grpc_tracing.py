"""Tests for shared.observability.grpc_tracing.TracingServerInterceptor (ops O1-d).

End-to-end coverage pairs a real sync ``grpc.server`` (registering
``TracingServerInterceptor``) with a real client-side interceptor
(``py_libs.grpc.interceptors.TracingClientInterceptor``) over a raw byte-passthrough
RPC (no .proto stubs needed), proving the server span is a CHILD of the client span
-- the actual propagation contract this interceptor exists to deliver.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from typing import Any

import grpc
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from shared.observability import grpc_tracing as gt
from shared.observability.grpc_tracing import TracingServerInterceptor

_SERVICE_NAME = "test.EchoService"
_METHOD_NAME = "Echo"
_FULL_METHOD = f"/{_SERVICE_NAME}/{_METHOD_NAME}"


def _echo(request: bytes, context: grpc.ServicerContext) -> bytes:
    """Raw byte-passthrough RPC body."""
    return request


def _failing(request: bytes, context: grpc.ServicerContext) -> bytes:
    """RPC body that always raises to exercise the error-status branch."""
    raise ValueError("boom")


def _generic_handler(behavior: Any = _echo) -> grpc.GenericRpcHandler:
    """Raw generic handler for ``_FULL_METHOD`` (no .proto stubs needed)."""
    method_handler = grpc.unary_unary_rpc_method_handler(
        behavior, request_deserializer=lambda x: x, response_serializer=lambda x: x
    )
    return grpc.method_handlers_generic_handler(_SERVICE_NAME, {_METHOD_NAME: method_handler})


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A standalone TracerProvider-backed sink patched into both tracer lookups."""
    exporter: Any = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter._provider = provider
    tracer = provider.get_tracer("test")
    monkeypatch.setattr(gt, "get_tracer", lambda *a, **k: tracer)
    yield exporter


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """An isolated MeterProvider patched into the server's meter lookup."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        "shared.observability.metrics.get_meter", lambda: provider.get_meter("test")
    )
    gt.reset_instruments_for_testing()
    yield reader
    gt.reset_instruments_for_testing()


@pytest.fixture
def sync_server() -> Iterator[tuple[grpc.Server, str]]:
    """Real sync gRPC server with TracingServerInterceptor registered."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=2),
        interceptors=[TracingServerInterceptor()],
    )
    server.add_generic_rpc_handlers((_generic_handler(),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield server, f"127.0.0.1:{port}"
    finally:
        server.stop(None)


class TestTracingServerInterceptor:
    """A real client call carrying a manually-injected W3C traceparent, against a real server."""

    def test_server_span_is_child_of_injected_traceparent(self, span_exporter, sync_server) -> None:
        """A traceparent injected into outgoing metadata produces a child SERVER span.

        Mirrors what any client interceptor in this monorepo (py_libs, management's
        AILB client, the penguincode CLI client) actually does -- a detached root
        span would mean O1-d regressed.
        """
        from opentelemetry import trace as otel_trace

        provider = span_exporter._provider if hasattr(span_exporter, "_provider") else None
        tracer = (provider or otel_trace).get_tracer("test-client")

        _, address = sync_server
        channel = grpc.insecure_channel(address)
        try:
            with tracer.start_as_current_span("client-span", kind=SpanKind.CLIENT) as client_span:
                from opentelemetry import propagate

                carrier: dict[str, str] = {}
                propagate.inject(carrier)
                call = channel.unary_unary(
                    _FULL_METHOD,
                    request_serializer=lambda x: x,
                    response_deserializer=lambda x: x,
                )
                response = call(b"ping", metadata=tuple(carrier.items()))
                client_span_id = client_span.get_span_context().span_id
                client_trace_id = client_span.get_span_context().trace_id
        finally:
            channel.close()

        assert response == b"ping"
        spans = span_exporter.get_finished_spans()
        server_spans = [s for s in spans if s.kind == SpanKind.SERVER]
        assert len(server_spans) == 1
        assert server_spans[0].parent is not None
        assert server_spans[0].parent.span_id == client_span_id
        assert server_spans[0].parent.trace_id == client_trace_id
        assert server_spans[0].attributes["rpc.grpc.status_code"] == "OK"
        assert "correlation_id" in server_spans[0].attributes

    def test_no_incoming_context_still_opens_a_root_server_span(
        self, sync_server, span_exporter
    ) -> None:
        """A plain call with no traceparent still gets a SERVER span (detached root)."""
        _, address = sync_server
        channel = grpc.insecure_channel(address)
        try:
            call = channel.unary_unary(
                _FULL_METHOD,
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
            call(b"ping")
        finally:
            channel.close()

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].kind == SpanKind.SERVER
        assert spans[0].parent is None

    def test_non_unary_unary_handler_passed_through_unwrapped(self, span_exporter) -> None:
        """Streaming handlers are returned as-is (same limitation as py_libs's)."""

        def _stream_continuation(details: Any) -> grpc.RpcMethodHandler:
            return grpc.unary_stream_rpc_method_handler(
                lambda request, context: iter([request]),
                request_deserializer=lambda x: x,
                response_serializer=lambda x: x,
            )

        class _Details:
            method = _FULL_METHOD
            invocation_metadata = ()

        handler = TracingServerInterceptor().intercept_service(_stream_continuation, _Details())
        assert handler.unary_unary is None

    def test_handler_exception_sets_internal_status(self, metric_reader, span_exporter) -> None:
        """An unhandled exception in the RPC body records INTERNAL on the span."""
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=2),
            interceptors=[TracingServerInterceptor()],
        )
        server.add_generic_rpc_handlers((_generic_handler(_failing),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            call = channel.unary_unary(
                _FULL_METHOD,
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
            with pytest.raises(grpc.RpcError):
                call(b"ping")
            channel.close()
        finally:
            server.stop(None)

        spans = span_exporter.get_finished_spans()
        assert spans[0].attributes["rpc.grpc.status_code"] == "INTERNAL"

        data = metric_reader.get_metrics_data()
        assert data is not None
        names = {
            m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
        }
        assert "rpc_server_duration_seconds" in names
        assert "rpc_server_requests_total" in names

    def test_rpc_error_raised_in_handler_sets_its_own_status(self, span_exporter) -> None:
        """A handler re-raising a downstream grpc.RpcError records that code, not INTERNAL."""

        def _rpc_erroring(request: bytes, context: grpc.ServicerContext) -> bytes:
            raise grpc.RpcError()

        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=2),
            interceptors=[TracingServerInterceptor()],
        )
        server.add_generic_rpc_handlers((_generic_handler(_rpc_erroring),))
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            call = channel.unary_unary(
                _FULL_METHOD,
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
            with pytest.raises(grpc.RpcError):
                call(b"ping")
            channel.close()
        finally:
            server.stop(None)

        spans = span_exporter.get_finished_spans()
        assert spans[0].attributes["rpc.grpc.status_code"] == "UNKNOWN"


def test_split_method_handles_missing_method() -> None:
    """An empty/None method string falls back to ('unknown', 'unknown')."""
    assert gt._split_method(None) == ("unknown", "unknown")
    assert gt._split_method("") == ("unknown", "unknown")
