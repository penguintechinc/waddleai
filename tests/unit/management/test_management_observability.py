"""The management service must emit real OTel: spans, RED metrics, DB spans, logs.

regression: release-audit-2026-09-23

Before this change ``services/management`` emitted zero OpenTelemetry and its
RED metrics were dead code (audit O1). These tests capture actual data points
through in-memory exporters/readers -- a declared-but-never-emitted instrument
is exactly the silent failure being guarded against, following the pattern in
``tests/unit/security/test_pii_telemetry.py``.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from services.management.app import observability as mgmt_obs
from shared.observability import metrics as obs_metrics


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """Install a real MeterProvider whose data points a test can read back.

    Hermetic: the process-global OTel MeterProvider and its run-once latch are
    captured on setup and restored on teardown, so this fixture never leaks a
    dead in-memory provider into another test module. A leaked provider broke
    tests/unit/security/test_pii_telemetry.py, which installs its own in-memory
    reader and can only do so while the run-once latch is still un-tripped.
    """
    prev_provider = otel_metrics._internal._METER_PROVIDER
    prev_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
    reader = InMemoryMetricReader()
    # set_meter_provider is guarded by a run-once latch; reset both the latch and
    # the cached provider so this test installs its own reader as the global one.
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    obs_metrics.reset_for_testing()
    mgmt_obs.reset_for_testing()
    try:
        yield reader
    finally:
        otel_metrics._internal._METER_PROVIDER = prev_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_once
        obs_metrics.reset_for_testing()
        mgmt_obs.reset_for_testing()


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """A TracerProvider-backed in-memory span sink; hand its tracer to the code."""
    # `Any` so stashing the provider (to keep it alive past the fixture) is a
    # plain attribute assignment: ruff rejects setattr-with-constant (B010) and
    # mypy rejects the attribute on the concrete type -- an Any-typed local
    # satisfies both.
    exporter: Any = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter._provider = provider
    return exporter


def _points(reader: InMemoryMetricReader, name: str) -> list:
    """Every data point recorded for one instrument name."""
    data = reader.get_metrics_data()
    out: list = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


async def _drive_request(app, scope: dict) -> None:
    """Drive one ASGI request through ``app`` collecting nothing (helper)."""

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        return None

    await app(scope, receive, send)


async def test_asgi_middleware_emits_a_server_span(span_exporter) -> None:
    """One HTTP request produces one SERVER span carrying method/path/status."""
    tracer = span_exporter._provider.get_tracer("test")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mgmt_obs.OTelASGIMiddleware(inner, tracer=tracer)
    await _drive_request(mw, {"type": "http", "method": "GET", "path": "/api/v1/widgets"})

    spans = span_exporter.get_finished_spans()
    assert len(spans) >= 1
    span = spans[-1]
    assert span.name == "GET /api/v1/widgets"
    assert span.attributes["http.request.method"] == "GET"
    assert span.attributes["http.response.status_code"] == 204


async def test_asgi_middleware_records_red_metrics(metric_reader, span_exporter) -> None:
    """The middleware records the RED rate counter and latency histogram."""
    tracer = span_exporter._provider.get_tracer("test")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mgmt_obs.OTelASGIMiddleware(inner, tracer=tracer)
    await _drive_request(mw, {"type": "http", "method": "POST", "path": "/api/v1/widgets"})

    assert _points(metric_reader, "http.server.requests"), "no RED request-count points"
    assert _points(metric_reader, "http.server.request.duration"), "no RED latency points"


async def test_red_metrics_labels_are_bounded(metric_reader, span_exporter) -> None:
    """RED attributes are method + status only -- never the raw path (cardinality)."""
    tracer = span_exporter._provider.get_tracer("test")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mgmt_obs.OTelASGIMiddleware(inner, tracer=tracer)
    await _drive_request(mw, {"type": "http", "method": "GET", "path": "/api/v1/users/12345"})

    for p in _points(metric_reader, "http.server.requests"):
        keys = set(p.attributes)
        assert keys == {"http.request.method", "http.response.status_code"}
        assert "12345" not in str(p.attributes)


async def test_5xx_increments_the_error_counter(metric_reader, span_exporter) -> None:
    """A 500 response bumps the RED error counter and marks the span errored."""
    tracer = span_exporter._provider.get_tracer("test")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mgmt_obs.OTelASGIMiddleware(inner, tracer=tracer)
    await _drive_request(mw, {"type": "http", "method": "DELETE", "path": "/api/v1/orgs/1"})

    assert _points(metric_reader, "http.server.errors"), "5xx did not increment error counter"


def test_sqlalchemy_engine_spans_queries_without_leaking_params(span_exporter) -> None:
    """Instrumenting the engine spans each statement; SQL text yes, values no."""
    tracer = span_exporter._provider.get_tracer("test")
    engine = sa.create_engine("sqlite://")
    assert mgmt_obs.instrument_sqlalchemy_engine(engine, tracer=tracer) is True

    param_value = "topsecret-value-9999"
    with engine.connect() as conn:
        conn.execute(sa.text("SELECT :v AS v"), {"v": param_value})

    db_spans = [s for s in span_exporter.get_finished_spans() if s.name == "db.query"]
    assert db_spans, "no db.query span emitted for an executed statement"
    stmt = db_spans[-1].attributes["db.statement"]
    assert "SELECT" in stmt
    assert param_value not in stmt  # bound parameter values must never enter the span


def test_engine_instrumented_only_once(span_exporter) -> None:
    """A second instrument call is a no-op (idempotent), not a duplicate listener."""
    tracer = span_exporter._provider.get_tracer("test")
    engine = sa.create_engine("sqlite://")
    assert mgmt_obs.instrument_sqlalchemy_engine(engine, tracer=tracer) is True
    assert mgmt_obs.instrument_sqlalchemy_engine(engine, tracer=tracer) is True

    with engine.connect() as conn:
        conn.execute(sa.text("SELECT 1"))

    assert len([s for s in span_exporter.get_finished_spans() if s.name == "db.query"]) == 1


def test_mock_engine_is_not_instrumented() -> None:
    """A non-Engine (e.g. a test mock DB) is skipped, not crashed on."""
    from unittest.mock import MagicMock

    assert mgmt_obs.instrument_sqlalchemy_engine(MagicMock()) is False


def test_logging_bridge_emits_a_log_record() -> None:
    """A stdlib log routed through the OTel handler reaches the log exporter."""
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    handler = mgmt_obs.build_logging_handler(provider)
    test_logger = logging.getLogger("waddleai.management.test.audit-telemetry")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    try:
        test_logger.info("management observability smoke log")
    finally:
        test_logger.removeHandler(handler)

    provider.force_flush()
    assert len(exporter.get_finished_logs()) >= 1


async def test_all_three_signals_present(metric_reader, span_exporter) -> None:
    """The telemetry gate: >=1 span, >=1 metric data point, >=1 log record."""
    tracer = span_exporter._provider.get_tracer("test")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mgmt_obs.OTelASGIMiddleware(inner, tracer=tracer)
    await _drive_request(mw, {"type": "http", "method": "PUT", "path": "/api/v1/quotas/1"})

    log_exporter = InMemoryLogExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    handler = mgmt_obs.build_logging_handler(log_provider)
    lg = logging.getLogger("waddleai.management.test.gate")
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        lg.info("gate log")
    finally:
        lg.removeHandler(handler)
    log_provider.force_flush()

    assert len(span_exporter.get_finished_spans()) >= 1
    assert _points(metric_reader, "http.server.requests")
    assert len(log_exporter.get_finished_logs()) >= 1
