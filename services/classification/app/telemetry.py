"""OpenTelemetry metrics + traces for the classification service.

OTLP destinations come from the standard ``OTEL_*`` env vars only; a dead or broken
exporter degrades to no export and never fails a request. Emission is PII-free by
construction: metric labels are bounded enums, spans carry only counts/booleans/ids
of backends. Request text, entity text and org ids never reach an attribute.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.metrics import CallbackOptions, Meter, MeterProvider, Observation
from opentelemetry.propagate import extract
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer, TracerProvider

from services.classification.app.config import ClassificationConfig
from services.classification.app.grpc_gen import classification_pb2 as _pb
from services.classification.app.obs import get_logger

pb: Any = _pb
log = get_logger(__name__)

SERVICE_NAME = "waddleai-classification"
SPAN_ANALYZE = "classification.analyze"
OUTCOMES = frozenset({"ok", "gate_disabled", "degraded", "invalid", "unauth"})
CACHE_RESULTS = frozenset({"hit", "miss", "error"})
_OTHER = "OTHER"
_ENUM_PREFIX = "ENTITY_TYPE_"
_BOUNDED_LABELS: dict[int, str] = {
    value: name[len(_ENUM_PREFIX) :]
    for name, value in pb.EntityType.items()
    if name not in ("ENTITY_TYPE_UNSPECIFIED", "ENTITY_TYPE_OTHER")
}
_DURATION_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
_ENDPOINT_ENVS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)


def entity_type_label(value: int) -> str:
    """Return the bounded EntityType name for a metric label; unknown values are OTHER."""
    return _BOUNDED_LABELS.get(int(value), _OTHER)


def _safe(fn: Callable[[], Any]) -> None:
    """Run an emit closure, swallowing everything: telemetry never fails a request."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - emission must be non-fatal by contract
        log.debug("telemetry_emit_failed", error_type=type(exc).__name__)


@dataclass(slots=True)
class Telemetry:
    """Instruments + tracer for one provider pair; every method is non-raising."""

    tracer: Tracer
    requests: Any
    duration: Any
    cache: Any
    entities: Any
    exporting: bool = False
    _queue_source: Callable[[], int] | None = None
    _providers: tuple[Any, Any] = field(default=(None, None))

    def record_request(self, outcome: str) -> None:
        """Count one Analyze request under a bounded outcome label."""
        label = outcome if outcome in OUTCOMES else "invalid"
        _safe(lambda: self.requests.add(1, {"outcome": label}))

    def observe_duration(self, seconds: float) -> None:
        """Record one Analyze duration in the histogram."""
        _safe(lambda: self.duration.record(seconds))

    def record_cache(self, result: str, count: int = 1) -> None:
        """Count cache lookups/errors under hit|miss|error."""
        label = result if result in CACHE_RESULTS else "error"
        _safe(lambda: self.cache.add(count, {"result": label}))

    def record_entities(self, spans: Iterable[Any]) -> None:
        """Count emitted spans per bounded EntityType name (unknown -> OTHER)."""

        def emit() -> None:
            for s in spans:
                self.entities.add(1, {"entity_type": entity_type_label(s.entity_type)})

        _safe(emit)

    def bind_queue_source(self, source: Callable[[], int]) -> None:
        """Point the queue-depth gauge at a callable returning the engine's depth."""
        self._queue_source = source

    def queue_depth(self) -> int | None:
        """Current bound queue depth, or None when unbound/failing."""
        if self._queue_source is None:
            return None
        try:
            return int(self._queue_source())
        except Exception:  # noqa: BLE001 - a gauge read must not break collection
            return None

    def extract_context(self, grpc_context: Any) -> Context | None:
        """Extract the W3C trace context from gRPC invocation metadata (None = new root)."""
        try:
            carrier = {
                str(k).lower(): v
                for k, v in grpc_context.invocation_metadata()
                if isinstance(v, str)
            }
            return extract(carrier)
        except Exception:  # noqa: BLE001 - bad/absent metadata just means a root span
            return None

    def set_attributes(self, span: Any, attrs: dict[str, Any]) -> None:
        """Set span attributes (caller guarantees they are PII-free scalars)."""
        _safe(lambda: span.set_attributes(attrs))

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Flush and stop owned providers (bounded wait; errors swallowed)."""
        tp, mp = self._providers
        if tp is not None:
            _safe(lambda: tp.force_flush(timeout_ms))
            _safe(tp.shutdown)
        if mp is not None:
            _safe(lambda: mp.force_flush(timeout_ms))
            _safe(mp.shutdown)


def build_telemetry(
    tracer_provider: TracerProvider,
    meter_provider: MeterProvider,
    *,
    exporting: bool = False,
) -> Telemetry:
    """Create the instruments on the given providers (no global state touched)."""
    meter: Meter = meter_provider.get_meter(SERVICE_NAME)
    holder: dict[str, Telemetry] = {}

    def queue_cb(_options: CallbackOptions) -> Iterable[Observation]:
        depth = holder["t"].queue_depth() if "t" in holder else None
        return [] if depth is None else [Observation(depth)]

    t = Telemetry(
        tracer=tracer_provider.get_tracer(SERVICE_NAME),
        requests=meter.create_counter(
            "waddleai_classification_requests_total", unit="1", description="Analyze requests"
        ),
        duration=meter.create_histogram(
            "waddleai_classification_duration_seconds",
            unit="s",
            description="Analyze latency",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        ),
        cache=meter.create_counter(
            "waddleai_classification_cache_total", unit="1", description="NER cache outcomes"
        ),
        entities=meter.create_counter(
            "waddleai_classification_entities_total", unit="1", description="Entities by type"
        ),
        exporting=exporting,
    )
    holder["t"] = t
    meter.create_observable_gauge(
        "waddleai_classification_engine_queue_depth",
        callbacks=[queue_cb],
        unit="1",
        description="In-flight NER jobs",
    )
    return t


def _make_exporters() -> tuple[Any, Any]:
    """Build OTLP span + metric exporters; endpoints/timeouts come from OTEL_* env only."""
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(), OTLPMetricExporter()


def _exporting_enabled() -> bool:
    """True when an OTLP endpoint is configured via env and the SDK is not disabled."""
    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    return any(os.environ.get(n, "").strip() for n in _ENDPOINT_ENVS)


_lock = threading.Lock()
_default: Telemetry | None = None


def get_telemetry() -> Telemetry:
    """Return the process Telemetry (API-global providers; no-op until init installs SDK)."""
    global _default
    with _lock:
        if _default is None:
            _default = build_telemetry(trace.get_tracer_provider(), metrics.get_meter_provider())
        return _default


def init_telemetry(cfg: ClassificationConfig, *, set_global: bool = True) -> Telemetry:
    """Configure OTLP tracing + metrics from ``OTEL_*`` env; a dead exporter is non-fatal.

    Exporters are asynchronous (batch / periodic), so an unreachable collector can
    only drop data, never fail a request. ``set_global=False`` keeps providers private
    (tests). ``cfg`` is accepted for symmetry with other service init hooks.
    """
    global _default
    del cfg  # no config-sourced destinations by design: OTEL_* env only
    resource = Resource.create({"service.name": SERVICE_NAME})
    tp = SdkTracerProvider(resource=resource)
    readers: list[Any] = []
    exporting = False
    if _exporting_enabled():
        try:
            span_exp, metric_exp = _make_exporters()
            tp.add_span_processor(BatchSpanProcessor(span_exp))
            readers.append(PeriodicExportingMetricReader(metric_exp))
            exporting = True
        except Exception as exc:  # noqa: BLE001 - broken exporter must not stop the service
            log.warning("telemetry_exporter_init_failed", error_type=type(exc).__name__)
    mp = SdkMeterProvider(resource=resource, metric_readers=readers)
    with _lock:
        if set_global:
            if _default is not None and _default._providers[0] is not None:
                return _default  # already installed in this process
            trace.set_tracer_provider(tp)
            metrics.set_meter_provider(mp)
    tel = build_telemetry(tp, mp, exporting=exporting)
    tel._providers = (tp, mp)
    if set_global:
        with _lock:
            _default = tel
    log.info("telemetry_initialised", exporting=exporting)
    return tel
