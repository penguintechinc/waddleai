"""Tests for `server.interceptors.TracingInterceptor` (O1, gRPC server hardening).

Self-contained (installs its own in-memory OTel providers, mirroring
`test_observability_otel.py`'s fixture rather than importing it cross-module)
-- proves the interceptor opens a real SERVER span per RPC, extracts an
incoming W3C `traceparent` as the span's parent, records
`rpc_server_duration_seconds` / `rpc_server_requests_total` with bounded
labels, and supports all four gRPC handler shapes (unary-unary,
unary-stream, stream-unary, stream-stream).

# regression: gRPC server hardening (O1 -- per-RPC tracing)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import grpc
import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util._once import Once

from penguincode_cli.observability import otel
from penguincode_cli.server.interceptors import TracingInterceptor


@pytest.fixture(autouse=True)
def _reset_otel_module_state() -> Iterator[None]:
    """Every test starts from penguincode otel's own un-initialized state."""
    otel.reset_for_testing()
    yield
    otel.reset_for_testing()


@pytest.fixture
def in_memory_exporters() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader]]:
    """Install real, in-memory-backed global providers a test can read back.

    Same hermetic run-once-latch dance as `test_observability_otel.py`'s
    fixture of the same name -- captured and restored so this never leaks a
    dead in-memory provider into another test module.
    """
    prev_tracer_provider = otel_trace._TRACER_PROVIDER
    prev_tracer_once = otel_trace._TRACER_PROVIDER_SET_ONCE
    prev_meter_provider = otel_metrics._internal._METER_PROVIDER
    prev_meter_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    otel_trace._TRACER_PROVIDER = None
    otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
    otel_trace.set_tracer_provider(tracer_provider)

    metric_reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader]))

    try:
        yield span_exporter, metric_reader
    finally:
        otel_trace._TRACER_PROVIDER = prev_tracer_provider
        otel_trace._TRACER_PROVIDER_SET_ONCE = prev_tracer_once
        otel_metrics._internal._METER_PROVIDER = prev_meter_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_meter_once


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    """Every data point recorded for one instrument name."""
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


class _FakeHandlerCallDetails:
    def __init__(self, method: str, invocation_metadata: Any = ()) -> None:
        self.method = method
        self.invocation_metadata = invocation_metadata


class _FakeContext:
    """Minimal stand-in for `grpc.aio.ServicerContext` -- only `.code()` used."""

    def __init__(self) -> None:
        self._code: Any = None

    def code(self) -> Any:
        return self._code

    def set_code(self, code: Any) -> None:
        self._code = code


def _unary_handler(response: Any = "ok", *, raises: Exception | None = None) -> Any:
    async def _behavior(request: Any, context: Any) -> Any:
        if raises is not None:
            raise raises
        return response

    return grpc.unary_unary_rpc_method_handler(_behavior)


def _stream_handler(items: list[Any], *, raises: Exception | None = None) -> Any:
    async def _behavior(request: Any, context: Any) -> AsyncIterator[Any]:
        for item in items:
            yield item
        if raises is not None:
            raise raises

    return grpc.unary_stream_rpc_method_handler(_behavior)


async def _continuation_returning(handler: Any) -> Any:
    async def _continuation(_details: Any) -> Any:
        return handler

    return _continuation


class TestUnarySuccess:
    @pytest.mark.asyncio
    async def test_records_span_and_metrics_on_success(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, metric_reader = in_memory_exporters
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.knowledge.v1.KnowledgeService/Query")

        wrapped = await interceptor.intercept_service(
            await _continuation_returning(_unary_handler("hello")), details
        )
        context = _FakeContext()
        result = await wrapped.unary_unary("request", context)
        assert result == "hello"

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "penguincode.knowledge.v1.KnowledgeService/Query"
        assert span.kind == otel_trace.SpanKind.SERVER
        assert span.attributes["rpc.system"] == "grpc"
        assert span.attributes["rpc.service"] == "penguincode.knowledge.v1.KnowledgeService"
        assert span.attributes["rpc.method"] == "Query"
        assert span.attributes["rpc.grpc.status_code"] == "OK"

        duration_points = _points(metric_reader, otel.RPC_SERVER_DURATION_HISTOGRAM_NAME)
        count_points = _points(metric_reader, otel.RPC_SERVER_REQUESTS_COUNTER_NAME)
        assert len(duration_points) == 1
        assert count_points[0].value == 1
        assert count_points[0].attributes["status_code"] == "OK"
        assert count_points[0].attributes["method"] == "Query"


class TestUnaryFailure:
    @pytest.mark.asyncio
    async def test_records_error_status_and_exception_on_raise(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, metric_reader = in_memory_exporters
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.ChatService/Send")

        async def _aborting_behavior(request: Any, context: Any) -> Any:
            context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
            raise RuntimeError("boom")

        handler = grpc.unary_unary_rpc_method_handler(_aborting_behavior)
        wrapped = await interceptor.intercept_service(
            await _continuation_returning(handler), details
        )
        context = _FakeContext()

        with pytest.raises(RuntimeError, match="boom"):
            await wrapped.unary_unary("request", context)

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["rpc.grpc.status_code"] == "RESOURCE_EXHAUSTED"
        assert spans[0].status.status_code == otel_trace.StatusCode.ERROR
        # `start_as_current_span`'s own context manager auto-records the
        # exception (and sets ERROR status) when it propagates out of the
        # `with` block -- see `_finish_rpc_span`'s docstring.
        assert len(spans[0].events) == 1

        count_points = _points(metric_reader, otel.RPC_SERVER_REQUESTS_COUNTER_NAME)
        assert count_points[0].attributes["status_code"] == "RESOURCE_EXHAUSTED"


class TestStreamingResponse:
    @pytest.mark.asyncio
    async def test_unary_stream_yields_items_and_records_once(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, metric_reader = in_memory_exporters
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.ChatService/StreamChat")

        wrapped = await interceptor.intercept_service(
            await _continuation_returning(_stream_handler([1, 2, 3])), details
        )
        context = _FakeContext()
        items = [item async for item in wrapped.unary_stream("request", context)]
        assert items == [1, 2, 3]

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        count_points = _points(metric_reader, otel.RPC_SERVER_REQUESTS_COUNTER_NAME)
        assert len(count_points) == 1

    @pytest.mark.asyncio
    async def test_stream_stream_propagates_exception_after_partial_yield(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, _ = in_memory_exporters
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.tools.v1.ToolCallbackService/ExecuteTools")

        async def _behavior(request_iterator: Any, context: Any) -> AsyncIterator[Any]:
            yield "first"
            raise ValueError("stream broke")

        handler = grpc.stream_stream_rpc_method_handler(_behavior)
        wrapped = await interceptor.intercept_service(
            await _continuation_returning(handler), details
        )
        context = _FakeContext()

        collected = []
        with pytest.raises(ValueError, match="stream broke"):
            async for item in wrapped.stream_stream(iter(["req"]), context):
                collected.append(item)
        assert collected == ["first"]

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == otel_trace.StatusCode.ERROR


class TestTraceparentPropagation:
    @pytest.mark.asyncio
    async def test_incoming_traceparent_becomes_the_span_parent(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, _ = in_memory_exporters
        interceptor = TracingInterceptor()

        # Mint a real W3C traceparent header for a synthetic parent span.
        tracer_provider = TracerProvider()
        parent_exporter = InMemorySpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(parent_exporter))
        parent_tracer = tracer_provider.get_tracer("test-client")
        propagator = TraceContextTextMapPropagator()
        carrier: dict[str, str] = {}
        with parent_tracer.start_as_current_span("client-call") as parent_span:
            propagator.inject(carrier)
            expected_trace_id = parent_span.get_span_context().trace_id

        details = _FakeHandlerCallDetails(
            "/penguincode.knowledge.v1.KnowledgeService/Query",
            invocation_metadata=tuple(carrier.items()),
        )
        wrapped = await interceptor.intercept_service(
            await _continuation_returning(_unary_handler()), details
        )
        await wrapped.unary_unary("request", _FakeContext())

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].context.trace_id == expected_trace_id


class TestNoHandler:
    @pytest.mark.asyncio
    async def test_none_handler_passes_through_untouched(self) -> None:
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.HealthService/Check")

        async def _continuation(_details: Any) -> None:
            return None

        result = await interceptor.intercept_service(_continuation, details)
        assert result is None

    @pytest.mark.asyncio
    async def test_handler_with_no_recognized_shape_passes_through_unchanged(self) -> None:
        """A handler with all four behaviors `None` (shouldn't happen in practice,
        but the interceptor must not crash on it) is returned as-is."""
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.HealthService/Check")
        empty_handler = grpc.unary_unary_rpc_method_handler(None)._replace(unary_unary=None)

        result = await interceptor.intercept_service(
            await _continuation_returning(empty_handler), details
        )
        assert result is empty_handler


class TestStreamUnaryShape:
    @pytest.mark.asyncio
    async def test_stream_unary_is_wrapped_and_recorded(
        self, in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader]
    ) -> None:
        span_exporter, metric_reader = in_memory_exporters
        interceptor = TracingInterceptor()
        details = _FakeHandlerCallDetails("/penguincode.tools.v1.ToolCallbackService/UploadBatch")

        async def _behavior(request_iterator: Any, context: Any) -> str:
            collected = [item async for item in request_iterator]
            return f"received {len(collected)}"

        async def _request_iterator() -> AsyncIterator[str]:
            yield "a"
            yield "b"

        handler = grpc.stream_unary_rpc_method_handler(_behavior)
        wrapped = await interceptor.intercept_service(
            await _continuation_returning(handler), details
        )
        result = await wrapped.stream_unary(_request_iterator(), _FakeContext())
        assert result == "received 2"

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].attributes["rpc.grpc.status_code"] == "OK"
        count_points = _points(metric_reader, otel.RPC_SERVER_REQUESTS_COUNTER_NAME)
        assert len(count_points) == 1
