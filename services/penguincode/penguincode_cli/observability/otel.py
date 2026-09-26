"""OpenTelemetry bootstrap and span/metric helpers for penguincode's store layer.

Self-contained mirror of the main WaddleAI repo's ``shared/observability``
package (``tracing.py`` / ``metrics.py``), kept independent rather than
imported across the repo boundary: ``services/penguincode`` is vendored with
its own ``pyproject.toml``, its own hash-locked lockfile, and its own release
story, so importing ``shared.*`` would couple two independently-versioned
release trains for one module. Same behavior as the shared package: init is
driven entirely by the standard OTLP environment variables, is a safe no-op
when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, and a dead/unreachable
collector degrades to the no-op providers rather than raising into a caller
mid-operation -- vector/graph queries and extraction must never fail because
telemetry couldn't be delivered.

Later tasks (T6 ``VectorStore``, T10 ``GraphStore``, T11-T13 extractors) call
into this module for spans, latency histograms, and event counters on their
store/extraction operations. Attributes passed to any helper here MUST be
bounded, non-sensitive values (operation kind, backend name, ``graph_kind``,
counts, booleans) -- never raw query text, usernames, tenant identifiers, or
any other PII/secret. See spec S12 and the org's OTel observability rule.

Also bridges stdlib log RECORDS to OTLP (completing the mandatory
logs+metrics+traces triad), mirroring the ``LoggerProvider`` +
``LoggingHandler`` pattern in ``services/management/app/observability.py``.
The bridge is additive only -- it never replaces or reconfigures existing
stdlib logging, it just adds a handler that also forwards records to OTLP.
"""

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Literal

from opentelemetry import _logs, metrics, trace
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)

_SERVICE_NAME: Final = "penguincode"

#: ``penguincode_cli/core/debug.py`` creates a dedicated ``"penguincode"``
#: logger with ``propagate = False`` (its own file handler, deliberately kept
#: off the root logger to avoid duplicate console output). Every other module
#: logs via ``logging.getLogger(__name__)`` under ``"penguincode_cli.*"``,
#: which propagates to root normally. A handler installed on the root logger
#: alone would therefore silently miss every record routed through
#: ``core.debug``'s ``log.info()``/``log.warning()``/etc. helpers -- the
#: bridge is installed on both loggers so nothing is missed either way.
_DEBUG_LOGGER_NAME: Final = "penguincode"

STORE_DURATION_HISTOGRAM_NAME: Final = "penguincode.store.duration"
STORE_EVENTS_COUNTER_NAME: Final = "penguincode.store.events"

#: Closed set of operation kinds accepted by every helper below. Keeping this
#: bounded is what keeps ``op_kind`` a safe, low-cardinality metric label --
#: an open string here would let a caller accidentally turn it into an
#: unbounded dimension (e.g. by passing a query id or table name).
OpKind = Literal["vector_query", "graph_query", "extraction"]
_VALID_OP_KINDS: Final[frozenset[str]] = frozenset({"vector_query", "graph_query", "extraction"})

_tracer: trace.Tracer | None = None
_meter: metrics.Meter | None = None
_initialized = False

_duration_histogram: metrics.Histogram | None = None
_events_counter: metrics.Counter | None = None

#: Handler bridging stdlib logging to OTLP, installed on the root logger by
#: ``init_observability()`` when a log pipeline is active. Never replaces
#: penguin/stdlib logging -- it is an additional handler alongside whatever
#: handlers a caller (CLI, server) already configured.
_log_handler: logging.Handler | None = None


@dataclass(slots=True)
class ObservabilityConfig:
    """OpenTelemetry configuration for penguincode, read from standard OTLP env vars."""

    otlp_endpoint: str | None = None
    service_name: str = _SERVICE_NAME
    service_version: str = "unknown"
    deployment_environment: str = "development"

    @classmethod
    def from_env(cls) -> "ObservabilityConfig":
        """Load configuration from the standard OTEL_EXPORTER_OTLP_* env vars.

        The endpoint is never hardcoded -- ``OTEL_EXPORTER_OTLP_ENDPOINT``
        unset means telemetry is disabled for this process, by design.
        """
        return cls(
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            service_name=os.getenv("OTEL_SERVICE_NAME", _SERVICE_NAME),
            service_version=os.getenv("OTEL_SERVICE_VERSION", "unknown"),
            deployment_environment=os.getenv("OTEL_DEPLOYMENT_ENVIRONMENT", "development"),
        )


def _validate_op_kind(op_kind: str) -> None:
    if op_kind not in _VALID_OP_KINDS:
        raise ValueError(f"op_kind must be one of {sorted(_VALID_OP_KINDS)}, got {op_kind!r}")


def build_logging_handler(logger_provider: _logs.LoggerProvider) -> logging.Handler:
    """Return an OTel ``LoggingHandler`` bridging stdlib logs to ``logger_provider``.

    Factored out (mirrors ``services/management/app/observability.py``'s helper
    of the same name) so a test can attach it to an in-memory provider and
    prove a log record actually flows, rather than asserting a handler merely
    exists. Typed against the API-level ``_logs.LoggerProvider`` (not the SDK's
    concrete subclass) since ``init_observability()`` passes whatever
    ``_logs.get_logger_provider()`` resolves to, which is API-typed.
    """
    return LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)


def init_observability(config: ObservabilityConfig | None = None) -> None:
    """Idempotently install the tracer + meter + logger providers from OTLP env vars.

    No-op (leaves the OTel API's default no-op tracer/meter in place, and
    installs no logging handler at all) when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
    unset. Never raises: a misconfigured or unreachable collector at
    construction time falls back to the no-op providers for that signal,
    logged as a warning, instead of taking the store/extraction call path down
    with it. The stdlib ``LoggingHandler`` installed here is additive -- it
    never replaces or reconfigures whatever logging (penguin/stdlib) a caller
    already has set up.
    """
    global _initialized, _tracer, _meter, _log_handler
    if _initialized:
        return
    cfg = config or ObservabilityConfig.from_env()
    _initialized = True

    if not cfg.otlp_endpoint:
        logger.info(
            "penguincode OTel disabled (no OTEL_EXPORTER_OTLP_ENDPOINT); using no-op providers"
        )
        _tracer = trace.get_tracer(cfg.service_name)
        _meter = metrics.get_meter(cfg.service_name)
        return

    resource = Resource.create(
        {
            "service.name": cfg.service_name,
            "service.version": cfg.service_version,
            "deployment.environment": cfg.deployment_environment,
        }
    )

    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.otlp_endpoint))
        )
        trace.set_tracer_provider(tracer_provider)
    except Exception as exc:  # pragma: no cover - exporter/collector setup failure
        logger.warning("penguincode OTel trace init failed, continuing without export: %s", exc)

    try:
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=cfg.otlp_endpoint))
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    except Exception as exc:  # pragma: no cover - exporter/collector setup failure
        logger.warning("penguincode OTel metric init failed, continuing without export: %s", exc)

    try:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=cfg.otlp_endpoint))
        )
        _logs.set_logger_provider(logger_provider)
    except Exception as exc:  # pragma: no cover - exporter/collector setup failure
        logger.warning("penguincode OTel log init failed, continuing without export: %s", exc)

    # Resolved via the global accessor (like get_tracer()/get_meter() below),
    # not the local `logger_provider`, so a run-once latch already tripped by
    # something else installing the global first (e.g. a host app, or a test
    # fixture) is respected rather than silently overridden.
    _log_handler = build_logging_handler(_logs.get_logger_provider())
    logging.getLogger().addHandler(_log_handler)
    # core.debug's dedicated "penguincode" logger has propagate=False, so it
    # needs the bridge attached directly -- see _DEBUG_LOGGER_NAME above.
    logging.getLogger(_DEBUG_LOGGER_NAME).addHandler(_log_handler)

    _tracer = trace.get_tracer(cfg.service_name)
    _meter = metrics.get_meter(cfg.service_name)
    logger.info("penguincode OTel initialized: endpoint=%s", cfg.otlp_endpoint)


def get_tracer() -> trace.Tracer:
    """The process tracer, initializing from environment on first use."""
    if _tracer is None:
        init_observability()
    if _tracer is None:  # pragma: no cover - init_observability always sets it
        raise RuntimeError("penguincode OTel tracer unavailable after init_observability()")
    return _tracer


def get_meter() -> metrics.Meter:
    """The process meter, initializing from environment on first use."""
    if _meter is None:
        init_observability()
    if _meter is None:  # pragma: no cover - init_observability always sets it
        raise RuntimeError("penguincode OTel meter unavailable after init_observability()")
    return _meter


def _duration_histogram_instrument() -> metrics.Histogram:
    global _duration_histogram
    if _duration_histogram is None:
        _duration_histogram = get_meter().create_histogram(
            STORE_DURATION_HISTOGRAM_NAME,
            unit="ms",
            description="Latency of vector/graph queries and extraction ops, by op_kind",
        )
    return _duration_histogram


def _events_counter_instrument() -> metrics.Counter:
    global _events_counter
    if _events_counter is None:
        _events_counter = get_meter().create_counter(
            STORE_EVENTS_COUNTER_NAME,
            unit="1",
            description="Store/extraction operations, by op_kind and outcome",
        )
    return _events_counter


def reset_for_testing() -> None:
    """Drop cached tracer/meter/instruments so a test can install its own providers.

    Also detaches the OTLP logging handler (if one was installed) from the
    root logger -- without this, a handler bound to a previous test's
    in-memory provider would keep receiving every subsequent test's log
    records after that provider has gone out of scope.
    """
    global _tracer, _meter, _initialized, _duration_histogram, _events_counter, _log_handler
    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
        logging.getLogger(_DEBUG_LOGGER_NAME).removeHandler(_log_handler)
    _tracer = None
    _meter = None
    _initialized = False
    _duration_histogram = None
    _events_counter = None
    _log_handler = None


@contextmanager
def store_span(name: str, **attrs: AttributeValue) -> Iterator[trace.Span]:
    """Span context manager for one store/extraction operation.

    ``attrs`` become span attributes and MUST be bounded, non-sensitive
    values only (op kind, backend, ``graph_kind``, counts, booleans) -- never
    raw query text, usernames, or secrets. An exception raised inside the
    block is recorded on the span (status=ERROR) and re-raised unchanged.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        for key, value in attrs.items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            raise


def record_vector_query_duration(duration_ms: float, **attrs: AttributeValue) -> None:
    """Record one vector-store query's latency (milliseconds)."""
    _record_duration("vector_query", duration_ms, **attrs)


def record_graph_query_duration(duration_ms: float, **attrs: AttributeValue) -> None:
    """Record one graph-store query/traversal's latency (milliseconds)."""
    _record_duration("graph_query", duration_ms, **attrs)


def record_extraction_duration(duration_ms: float, **attrs: AttributeValue) -> None:
    """Record one extraction call's (code/knowledge/memory graph) latency (milliseconds)."""
    _record_duration("extraction", duration_ms, **attrs)


def _record_duration(op_kind: OpKind, duration_ms: float, **attrs: AttributeValue) -> None:
    _validate_op_kind(op_kind)
    _duration_histogram_instrument().record(duration_ms, attributes={"op_kind": op_kind, **attrs})


def record_store_event(op_kind: str, *, outcome: str = "ok", **attrs: AttributeValue) -> None:
    """Increment the store/extraction event counter for one op_kind + outcome.

    ``op_kind`` must be one of ``vector_query``, ``graph_query``,
    ``extraction`` -- a closed set, kept bounded as a metric label.
    """
    _validate_op_kind(op_kind)
    _events_counter_instrument().add(
        1, attributes={"op_kind": op_kind, "outcome": outcome, **attrs}
    )


@contextmanager
def timed_store_operation(
    op_kind: OpKind, name: str, **attrs: AttributeValue
) -> Iterator[trace.Span]:
    """Span + latency histogram + event counter for one store/extraction op, in one call.

    The primary helper later tasks (``PgVectorStore.query``,
    ``PostgresGraphStore.neighbors``/``subgraph``, the code/knowledge/memory
    extractors) should reach for: wraps the DB/extraction call in a span
    named ``name`` carrying ``attrs``, records its wall-clock duration into
    ``penguincode.store.duration`` labeled ``op_kind=<op_kind>``, and
    increments ``penguincode.store.events`` labeled
    ``outcome=ok``/``outcome=error`` depending on whether the block raised.
    Exceptions are re-raised unchanged after being recorded.
    """
    _validate_op_kind(op_kind)
    start = time.perf_counter()
    outcome = "ok"
    try:
        with store_span(name, **attrs) as span:
            try:
                yield span
            except Exception:
                outcome = "error"
                raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _record_duration(op_kind, duration_ms, **attrs)
        record_store_event(op_kind, outcome=outcome, **attrs)
