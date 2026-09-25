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
"""

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Literal

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)

_SERVICE_NAME: Final = "penguincode"

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


def init_observability(config: ObservabilityConfig | None = None) -> None:
    """Idempotently install the tracer + meter providers from OTLP env vars.

    No-op (leaves the OTel API's default no-op tracer/meter in place) when
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset. Never raises: a misconfigured or
    unreachable collector at construction time falls back to the no-op
    providers for that signal, logged as a warning, instead of taking the
    store/extraction call path down with it.
    """
    global _initialized, _tracer, _meter
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
    """Drop cached tracer/meter/instruments so a test can install its own providers."""
    global _tracer, _meter, _initialized, _duration_histogram, _events_counter
    _tracer = None
    _meter = None
    _initialized = False
    _duration_histogram = None
    _events_counter = None


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
