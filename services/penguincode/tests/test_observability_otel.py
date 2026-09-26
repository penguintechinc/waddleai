"""penguincode's OTel bootstrap must be safe-by-default and actually emit.

Mirrors the pattern in the main WaddleAI repo's
``tests/unit/security/test_pii_telemetry.py`` / ``test_management_observability.py``:
capture real data through in-memory exporters/readers rather than asserting an
instrument object merely exists -- a declared-but-never-emitted instrument is
exactly the silent failure this telemetry is meant to prevent.

# regression: penguincode-knowledge-platform (T5 -- OTel foundation)
# regression: fix/penguincode-otel-logs -- logs pipeline (completes the mandatory
# logs+metrics+traces triad; previously log records only went through stdlib logging)
"""

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from opentelemetry import _logs as otel_logs
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
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


@pytest.fixture
def in_memory_log_exporter() -> Iterator[InMemoryLogExporter]:
    """Install a real, in-memory-backed global LoggerProvider a test can read back.

    Same hermetic run-once-latch dance as ``in_memory_exporters`` above, applied
    to the logs API's ``_LOGGER_PROVIDER`` / ``_LOGGER_PROVIDER_SET_ONCE`` globals.
    """
    prev_logger_provider = otel_logs._internal._LOGGER_PROVIDER
    prev_logger_once = otel_logs._internal._LOGGER_PROVIDER_SET_ONCE

    # SDK ctor is unannotated upstream (logs API not yet stable) -- no fix available.
    log_exporter = InMemoryLogExporter()  # type: ignore[no-untyped-call]
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    otel_logs._internal._LOGGER_PROVIDER = None
    otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = Once()
    otel_logs.set_logger_provider(logger_provider)

    try:
        yield log_exporter
    finally:
        otel_logs._internal._LOGGER_PROVIDER = prev_logger_provider
        otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = prev_logger_once


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

    def test_no_log_pipeline_leaks_a_dedicated_otlp_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no endpoint, no ``LoggingHandler`` is left on the root logger."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        otel.init_observability()

        assert not any(isinstance(h, LoggingHandler) for h in logging.getLogger().handlers)

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

    def test_broken_log_exporter_construction_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        monkeypatch.setattr(
            otel,
            "OTLPLogExporter",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("collector unreachable")),
        )

        otel.init_observability()
        logging.getLogger(__name__).info("must not raise even though the log exporter is dead")


class TestInitWithEndpointSet:
    """The construction-succeeds path: real (if unreachable) OTLP exporters."""

    def test_unreachable_endpoint_installs_providers_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exporter construction against an unreachable target never raises.

        gRPC channel creation is lazy -- constructing OTLPSpanExporter/
        OTLPMetricExporter/OTLPLogExporter against a port nothing is listening
        on succeeds immediately; only a later export attempt would fail, and
        that failure is handled inside BatchSpanProcessor/
        PeriodicExportingMetricReader/BatchLogRecordProcessor's own background
        export path, never here. This exercises init's genuine success branch
        (providers installed) as distinct from the monkeypatched-failure
        branches above.
        """
        prev_tracer_provider = otel_trace._TRACER_PROVIDER
        prev_tracer_once = otel_trace._TRACER_PROVIDER_SET_ONCE
        prev_meter_provider = otel_metrics._internal._METER_PROVIDER
        prev_meter_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
        prev_logger_provider = otel_logs._internal._LOGGER_PROVIDER
        prev_logger_once = otel_logs._internal._LOGGER_PROVIDER_SET_ONCE
        otel_trace._TRACER_PROVIDER = None
        otel_trace._TRACER_PROVIDER_SET_ONCE = Once()
        otel_metrics._internal._METER_PROVIDER = None
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
        otel_logs._internal._LOGGER_PROVIDER = None
        otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = Once()
        try:
            monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:44317")
            otel.init_observability()

            with otel.store_span("vector.query", backend="pgvector"):
                pass
            otel.record_vector_query_duration(1.0, backend="pgvector")
            logging.getLogger(__name__).info("real (unreachable-collector) log init path")
        finally:
            otel_trace._TRACER_PROVIDER = prev_tracer_provider
            otel_trace._TRACER_PROVIDER_SET_ONCE = prev_tracer_once
            otel_metrics._internal._METER_PROVIDER = prev_meter_provider
            otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_meter_once
            otel_logs._internal._LOGGER_PROVIDER = prev_logger_provider
            otel_logs._internal._LOGGER_PROVIDER_SET_ONCE = prev_logger_once


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

    def test_init_observability_emits_a_log_record(
        self,
        in_memory_log_exporter: InMemoryLogExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A log emitted after ``init_observability()`` reaches the OTLP log pipeline.

        The endpoint-set branch's own ``_logs.set_logger_provider()`` call is a
        silent no-op here (the run-once latch was already tripped by the
        ``in_memory_log_exporter`` fixture) -- init instead binds its
        ``LoggingHandler`` to whichever provider ``_logs.get_logger_provider()``
        currently resolves to, exactly mirroring how ``get_tracer()``/
        ``get_meter()`` already resolve against a pre-installed fixture provider
        elsewhere in this file. That is what lets this in-memory exporter
        observe a real record produced through the full init path.

        Logger name deliberately avoids a ``"penguincode."`` prefix: that
        namespace belongs to ``core.debug``'s dedicated non-propagating
        logger (see ``TestLoggingBridgeCoversNonPropagatingDebugLogger``
        below) and this test is specifically about root-logger propagation.
        """
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        otel.init_observability()

        # The root logger's default effective level is WARNING; a dedicated
        # logger with an explicit INFO level is required for the record to be
        # created at all, independent of whether the OTLP handler is attached.
        test_logger = logging.getLogger("otel-logs-test.root-propagation")
        test_logger.setLevel(logging.INFO)
        test_logger.info("penguincode otel logs pipeline smoke record")

        records = in_memory_log_exporter.get_finished_logs()
        assert len(records) >= 1

    def test_init_observability_log_pipeline_survives_no_endpoint_then_reinit(
        self,
        in_memory_log_exporter: InMemoryLogExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No-op init leaves the process able to log without ever touching OTLP."""
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        otel.init_observability()

        test_logger = logging.getLogger("otel-logs-test.noop-then-reinit")
        test_logger.setLevel(logging.INFO)
        # No-op path attaches no handler -- nothing should reach the exporter.
        test_logger.info("no-op path, must not be exported")
        assert list(in_memory_log_exporter.get_finished_logs()) == []


class TestLoggingBridge:
    """``build_logging_handler`` is the factored, independently-testable bridge.

    Mirrors ``services/management/app/observability.py``'s
    ``build_logging_handler`` + its ``test_logging_bridge_emits_a_log_record``:
    proves the handler-construction wiring itself, independent of
    ``init_observability()``'s env-driven branching.
    """

    def test_build_logging_handler_emits_a_log_record(self) -> None:
        exporter = InMemoryLogExporter()  # type: ignore[no-untyped-call]
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

        handler = otel.build_logging_handler(provider)
        test_logger = logging.getLogger("penguincode.test.otel-logs-bridge")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        try:
            test_logger.info("penguincode observability smoke log")
        finally:
            test_logger.removeHandler(handler)

        provider.force_flush()
        assert len(exporter.get_finished_logs()) >= 1

    def test_stdlib_logging_still_works_alongside_the_otlp_handler(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The OTLP bridge is additive -- ordinary stdlib logging still works."""
        exporter = InMemoryLogExporter()  # type: ignore[no-untyped-call]
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

        handler = otel.build_logging_handler(provider)
        test_logger = logging.getLogger("penguincode.test.otel-logs-alongside-stdlib")
        test_logger.setLevel(logging.INFO)
        test_logger.addHandler(handler)
        try:
            with caplog.at_level(logging.INFO, logger=test_logger.name):
                test_logger.info("both sinks must see this")
        finally:
            test_logger.removeHandler(handler)

        provider.force_flush()
        assert len(exporter.get_finished_logs()) >= 1
        assert "both sinks must see this" in caplog.text


class TestLoggingBridgeCoversNonPropagatingDebugLogger:
    """``core.debug`` sets up a dedicated ``"penguincode"`` logger with
    ``propagate = False`` (its own file handler, deliberately kept off the
    root logger to avoid duplicate console output -- see
    ``penguincode_cli/core/debug.py``). A handler installed on the root
    logger alone would silently miss every record routed through
    ``core.debug``'s ``log.info()``/``log.warning()``/etc. helpers, so
    ``init_observability()`` must attach the bridge to that logger directly
    too.
    """

    def test_debug_module_logger_reaches_the_otlp_pipeline(
        self,
        in_memory_log_exporter: InMemoryLogExporter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        otel.init_observability()

        debug_logger = logging.getLogger("penguincode")
        assert debug_logger.propagate is False, (
            "test assumption: core.debug must still disable propagation on "
            "this logger, or this test is no longer exercising the gap it "
            "guards against"
        )
        debug_logger.setLevel(logging.INFO)
        debug_logger.info("record routed through core.debug's non-propagating logger")

        records = in_memory_log_exporter.get_finished_logs()
        assert len(records) >= 1


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
