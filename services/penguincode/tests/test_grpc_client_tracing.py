"""Tests for the CLI's gRPC client trace-propagation interceptor (ops O1-d).

Covers: client span creation + attributes, metadata carrying ``traceparent`` and
baggage, correlation-id reuse vs minting, the ``penguincode.disable-grpc-trace-
propagation`` kill-switch, and that ``GRPCClient.connect()`` actually registers the
interceptor on the channel it builds.
"""

from __future__ import annotations

import asyncio
import collections
from typing import Any

import grpc
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from penguincode_cli.client import tracing_interceptor as ti
from penguincode_cli.client.tracing_interceptor import TracingClientInterceptor

_FakeClientCallDetails = collections.namedtuple(
    "_FakeClientCallDetails",
    ("method", "timeout", "metadata", "credentials", "wait_for_ready"),
)


class _FakeAsyncCall:
    """Minimal awaitable ``grpc.aio`` Call stand-in for direct interceptor tests."""

    def __init__(self, code: grpc.StatusCode | None = grpc.StatusCode.OK) -> None:
        """Store the status code ``.code()`` should return."""
        self._code = code

    async def code(self) -> grpc.StatusCode | None:
        """Return the stored status code."""
        return self._code


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """A standalone TracerProvider-backed in-memory span sink."""
    exporter: Any = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter._provider = provider
    return exporter


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    """Install an isolated MeterProvider and patch get_meter() to use it."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(ti, "get_meter", lambda: provider.get_meter("test"))
    ti.reset_instruments_for_testing()
    yield reader
    ti.reset_instruments_for_testing()


def _details(metadata: tuple = ()) -> _FakeClientCallDetails:
    """Build a fake ClientCallDetails for /penguincode.v1.ChatService/Chat."""
    return _FakeClientCallDetails(
        "/penguincode.v1.ChatService/Chat", None, metadata, None, None
    )


class TestTracingClientInterceptor:
    """Direct unit coverage of TracingClientInterceptor.intercept_unary_unary."""

    def test_injects_traceparent_and_baggage(
        self, span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh call carries a traceparent and an x-correlation-id."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)
        captured: dict[str, Any] = {}

        async def _continuation(details: Any, request: Any) -> _FakeAsyncCall:
            captured["metadata"] = dict(details.metadata)
            return _FakeAsyncCall(grpc.StatusCode.OK)

        call = asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(
                _continuation, _details(), b"x"
            )
        )
        assert isinstance(call, _FakeAsyncCall)
        assert "traceparent" in captured["metadata"]
        assert "x-correlation-id" in captured["metadata"]

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].kind == SpanKind.CLIENT
        assert spans[0].name == "penguincode.v1.ChatService/Chat"
        assert spans[0].attributes["rpc.grpc.status_code"] == "OK"

    def test_reuses_inbound_correlation_id(
        self, span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing x-correlation-id in metadata is reused, not replaced."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)
        captured: dict[str, Any] = {}

        async def _continuation(details: Any, request: Any) -> _FakeAsyncCall:
            captured["metadata"] = dict(details.metadata)
            return _FakeAsyncCall(grpc.StatusCode.OK)

        details = _details(metadata=(("x-correlation-id", "caller-supplied"),))
        asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(_continuation, details, b"x")
        )
        assert captured["metadata"]["x-correlation-id"] == "caller-supplied"

    def test_error_status_sets_error_span_status(
        self, span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-OK status code is recorded on the span attribute."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)

        async def _continuation(details: Any, request: Any) -> _FakeAsyncCall:
            return _FakeAsyncCall(grpc.StatusCode.UNAVAILABLE)

        asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(_continuation, _details(), b"x")
        )
        spans = span_exporter.get_finished_spans()
        assert spans[0].attributes["rpc.grpc.status_code"] == "UNAVAILABLE"

    def test_code_lookup_failure_falls_back_to_unknown(
        self, span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """call.code() raising never breaks the call; status falls back to UNKNOWN."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)

        class _RaisingCall:
            async def code(self) -> grpc.StatusCode:
                raise RuntimeError("not ready")

        async def _continuation(details: Any, request: Any) -> Any:
            return _RaisingCall()

        asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(_continuation, _details(), b"x")
        )
        spans = span_exporter.get_finished_spans()
        assert spans[0].attributes["rpc.grpc.status_code"] == "UNKNOWN"

    def test_kill_switch_disables_propagation(
        self, span_exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """penguincode.disable-grpc-trace-propagation=ON skips injection entirely."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)
        monkeypatch.setattr(ti, "is_enabled", lambda *a, **k: True)
        captured: dict[str, Any] = {}

        async def _continuation(details: Any, request: Any) -> _FakeAsyncCall:
            captured["metadata"] = dict(details.metadata or ())
            return _FakeAsyncCall(grpc.StatusCode.OK)

        asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(_continuation, _details(), b"x")
        )
        assert "traceparent" not in captured["metadata"]
        assert len(span_exporter.get_finished_spans()) == 0

    def test_metrics_recorded(
        self,
        span_exporter: InMemorySpanExporter,
        metric_reader: InMemoryMetricReader,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A successful call records both the duration histogram and request counter."""
        tracer = span_exporter._provider.get_tracer("test")
        monkeypatch.setattr(ti, "get_tracer", lambda: tracer)

        async def _continuation(details: Any, request: Any) -> _FakeAsyncCall:
            return _FakeAsyncCall(grpc.StatusCode.OK)

        asyncio.run(
            TracingClientInterceptor().intercept_unary_unary(_continuation, _details(), b"x")
        )
        data = metric_reader.get_metrics_data()
        assert data is not None
        names = {
            m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
        }
        assert "rpc_client_duration_seconds" in names
        assert "rpc_client_requests_total" in names


class TestGRPCClientConnectWiring:
    """GRPCClient.connect() must register the tracing interceptor on its channel."""

    def test_connect_passes_interceptor_to_insecure_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """connect() passes a TracingClientInterceptor into grpc.aio.insecure_channel."""
        from penguincode_cli.client import grpc_client as gc

        seen: dict[str, Any] = {}
        real_insecure_channel = gc.grpc.aio.insecure_channel

        def _spy(address: str, *, interceptors=None, **kwargs):
            seen["interceptors"] = interceptors
            return real_insecure_channel(address, interceptors=interceptors, **kwargs)

        monkeypatch.setattr(gc.grpc.aio, "insecure_channel", _spy)

        class _StubServerConfig:
            host = "localhost"
            port = 1
            tls_enabled = False
            grpc_max_message_bytes = 4 * 1024 * 1024

        class _StubClientConfig:
            token_path = "/tmp/does-not-matter"  # noqa: S108 -- test fixture path

        client = gc.GRPCClient(_StubServerConfig(), _StubClientConfig())

        async def _run() -> None:
            try:
                await client.connect()
            except Exception:
                pass  # health check against a non-existent server is expected to fail

        asyncio.run(_run())
        assert seen.get("interceptors")
        assert any(isinstance(i, TracingClientInterceptor) for i in seen["interceptors"])
