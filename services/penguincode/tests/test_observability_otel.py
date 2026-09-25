"""penguincode's OTel bootstrap must be safe-by-default and actually emit.

Mirrors the pattern in the main WaddleAI repo's
``tests/unit/security/test_pii_telemetry.py`` / ``test_management_observability.py``:
capture real data through in-memory exporters/readers rather than asserting an
instrument object merely exists -- a declared-but-never-emitted instrument is
exactly the silent failure this telemetry is meant to prevent.

# regression: penguincode-knowledge-platform (T5 -- OTel foundation)
"""

from collections.abc import Iterator
from typing import Any, cast

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from penguincode_cli.observability import otel


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Every test starts from penguincode otel's own un-initialized state."""
    otel.reset_for_testing()
    yield
    otel.reset_for_testing()


@pytest.fixture
def in_memory_exporters() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader]]:
    """Install real, in-memory-backed global providers a test can read back.

    Hermetic: OTel's global TracerProvider/MeterProvider are each guarded by a
    run-once latch. Both latches (and the previous provider) are captured on
    setup and restored on teardown so this fixture never leaks a dead
    in-memory provider into another test module -- see
    ``tests/unit/management/test_management_observability.py`` in the main
    repo for the same hazard against ``waddleai.pii.detected``.
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


class TestNoOpWhenEndpointUnset:
    """No OTEL_EXPORTER_OTLP_ENDPOINT -- everything must still work, silently."""

    def test_init_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        otel.init_observability()

    def test_span_and_record_helpers_are_safe_with_no_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        otel.init_observability()

        with otel.store_span("vector.query", backend="pgvector"):
            pass
        otel.record_vector_query_duration(12.5, backend="pgvector")
        otel.record_graph_query_duration(3.2, graph_kind="code")
        otel.record_extraction_duration(50.0, graph_kind="knowledge")
        otel.record_store_event("vector_query", outcome="ok")

        with otel.timed_store_operation("extraction", "knowledge.extract", graph_kind="knowledge"):
            pass

    def test_init_observability_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second call must be a no-op -- env changes between calls have no effect."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        otel.init_observability()
        tracer_first = otel.get_tracer()
        meter_first = otel.get_meter()

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        otel.init_observability()

        assert otel.get_tracer() is tracer_first
        assert otel.get_meter() is meter_first


class TestFailingExporterNeverRaises:
    """A dead/unreachable collector must degrade to no-op, never raise."""

    def test_broken_span_exporter_construction_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("collector unreachable")

        monkeypatch.setattr(otel, "OTLPSpanExporter", _boom)
        monkeypatch.setattr(otel, "OTLPMetricExporter", _boom)

        otel.init_observability()

        with otel.store_span("graph.query", graph_kind="code"):
            pass

    def test_broken_metric_exporter_construction_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        monkeypatch.setattr(
            otel,
            "OTLPMetricExporter",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("collector unreachable")),
        )

        otel.init_observability()
        otel.record_vector_query_duration(1.0, backend="pgvector")


class TestInitWithEndpointSet:
    """The construction-succeeds path: real (if unreachable) OTLP exporters."""

    def test_unreachable_endpoint_installs_providers_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exporter construction against an unreachable target never raises.

        gRPC channel creation is lazy -- constructing OTLPSpanExporter/
        OTLPMetricExporter against a port nothing is listening on succeeds
        immediately; only a later export attempt would fail, and that failure
        is handled inside BatchSpanProcessor/PeriodicExportingMetricReader's
        own background export path, never here. This exercises init's
        genuine success branch (providers installed) as distinct from the
        monkeypatched-failure branches above.
        """
        prev_tracer_provider = otel_trace._TRACER_PROVIDER
        prev_tracer_once = otel_trace._TRACER_PROVIDER_SET_ONCE
        prev_meter_provider = otel_metrics._internal._METER_PROVIDER
        prev_meter_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
        otel_trace._TRACER_PROVIDER = None
        otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
        otel_metrics._internal._METER_PROVIDER = None
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
        try:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:44317")
            otel.init_observability()

            with otel.store_span("vector.query", backend="pgvector"):
                pass
            otel.record_vector_query_duration(1.0, backend="pgvector")
        finally:
            otel_trace._TRACER_PROVIDER = prev_tracer_provider
            otel_trace._TRACER_PROVIDER_SET_ONCE = prev_tracer_once
            otel_metrics._internal._METER_PROVIDER = prev_meter_provider
            otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_meter_once


class TestEmitsRealTelemetry:
    """The helpers must produce actual spans and metric data points."""

    def test_store_span_emits_a_span(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        span_exporter, _ = in_memory_exporters
        otel.reset_for_testing()

        with otel.store_span("vector.query", backend="pgvector", n=5):
            pass

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "vector.query"
        span_attrs = spans[0].attributes
        assert span_attrs is not None
        assert span_attrs["backend"] == "pgvector"
        assert span_attrs["n"] == 5

    def test_store_span_records_exception_and_reraises(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        span_exporter, _ = in_memory_exporters
        otel.reset_for_testing()

        with pytest.raises(ValueError, match="boom"):
            with otel.store_span("graph.query"):
                raise ValueError("boom")

        spans = span_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == otel_trace.StatusCode.ERROR

    def test_duration_helpers_emit_histogram_points(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        _, metric_reader = in_memory_exporters
        otel.reset_for_testing()

        otel.record_vector_query_duration(12.5, backend="pgvector")
        otel.record_graph_query_duration(3.2, graph_kind="code")
        otel.record_extraction_duration(50.0, graph_kind="knowledge")

        points = _points(metric_reader, otel.STORE_DURATION_HISTOGRAM_NAME)
        assert len(points) >= 1
        op_kinds = {p.attributes["op_kind"] for p in points}
        assert op_kinds == {"vector_query", "graph_query", "extraction"}

    def test_event_counter_emits_a_data_point(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        _, metric_reader = in_memory_exporters
        otel.reset_for_testing()

        otel.record_store_event("vector_query", outcome="ok")

        points = _points(metric_reader, otel.STORE_EVENTS_COUNTER_NAME)
        assert len(points) >= 1
        assert any(p.value >= 1 for p in points)

    def test_timed_store_operation_emits_span_and_metrics_together(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        span_exporter, metric_reader = in_memory_exporters
        otel.reset_for_testing()

        with otel.timed_store_operation("vector_query", "pgvector.query", backend="pgvector"):
            pass

        assert len(span_exporter.get_finished_spans()) == 1
        duration_points = _points(metric_reader, otel.STORE_DURATION_HISTOGRAM_NAME)
        event_points = _points(metric_reader, otel.STORE_EVENTS_COUNTER_NAME)
        assert duration_points and event_points
        assert all(p.attributes["outcome"] == "ok" for p in event_points)

    def test_timed_store_operation_records_error_outcome_and_reraises(
        self,
        in_memory_exporters: tuple[InMemorySpanExporter, InMemoryMetricReader],
    ) -> None:
        _, metric_reader = in_memory_exporters
        otel.reset_for_testing()

        with pytest.raises(RuntimeError):
            with otel.timed_store_operation("graph_query", "graph.neighbors"):
                raise RuntimeError("db down")

        event_points = _points(metric_reader, otel.STORE_EVENTS_COUNTER_NAME)
        assert any(p.attributes["outcome"] == "error" for p in event_points)


class TestBoundedOpKind:
    """op_kind is a closed set -- keeps metric label cardinality bounded."""

    def test_unknown_op_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="op_kind"):
            otel.record_store_event("not-a-real-kind")

    def test_unknown_op_kind_rejected_in_timed_operation(self) -> None:
        # cast(): a real caller could pass an untyped/dynamic value (e.g. from
        # config or a dict) that mypy can't see is invalid -- the runtime
        # guard in timed_store_operation is what protects that path.
        bad_op_kind = cast(otel.OpKind, "not-a-real-kind")
        with pytest.raises(ValueError, match="op_kind"):
            with otel.timed_store_operation(bad_op_kind, "x"):
                pass
