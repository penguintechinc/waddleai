"""OpenTelemetry wiring for the management (control-plane) service.

The management service previously emitted zero OpenTelemetry and exposed a
hand-rolled ``/metrics`` that never recorded a single RED metric (audit O1).
This module closes that gap by:

- bootstrapping traces, metrics, and logs from the shared, env-configurable
  OTLP setup (``shared.observability``) -- a no-op when
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, so nothing here can take startup or
  the request path down;
- providing an ASGI middleware that opens one SERVER span per request and
  records RED metrics (request count, latency histogram, error count) with
  *bounded* attributes only (method + status code -- never the raw path or a
  user id, which are unbounded-cardinality label hazards);
- instrumenting the SQLAlchemy engine that penguin-dal runs every runtime query
  through, so DB calls are finally spanned;
- feeding the same RED numbers into the existing prometheus_client surface so
  ``/metrics`` reflects real traffic rather than four static gauges.

The OTLP destination is never hardcoded: everything flows from the standard
``OTEL_EXPORTER_OTLP_*`` env vars via ``shared.observability``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import Gauge

from shared.observability.metrics import get_meter, init_metrics
from shared.observability.tracing import get_tracer, init_tracing

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opentelemetry.metrics import Counter, Histogram, Meter
    from opentelemetry.trace import Tracer
    from quart import Quart

logger = logging.getLogger(__name__)

# Process start time for the uptime gauge exposed on /metrics.
_START_TIME = time.time()

# RED instruments are created lazily (on first record) so tests can install
# their own MeterProvider first, mirroring shared.observability.metrics.
_req_duration: Any = None
_req_count: Any = None
_err_count: Any = None

# prometheus_client gauges for the /metrics availability surface. Declared at
# module scope so they register in the default registry exactly once per
# process (re-declaring the same name raises "Duplicated timeseries").
DB_UP = Gauge("waddleai_management_db_up", "Management DB connectivity (1=up, 0=down)")
REDIS_UP = Gauge("waddleai_management_redis_up", "Management Redis connectivity (1=up, 0=down)")
UPTIME = Gauge("waddleai_management_uptime_seconds", "Seconds since management process start")


def _instruments() -> tuple[Histogram, Counter, Counter]:
    """Lazily create (and cache) the three RED instruments on the process meter."""
    global _req_duration, _req_count, _err_count
    if _req_duration is None or _req_count is None or _err_count is None:
        meter: Meter = get_meter()
        _req_duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Management HTTP server request latency",
        )
        _req_count = meter.create_counter(
            "http.server.requests",
            unit="1",
            description="Management HTTP server requests (RED rate)",
        )
        _err_count = meter.create_counter(
            "http.server.errors",
            unit="1",
            description="Management HTTP server 5xx responses (RED errors)",
        )
    return _req_duration, _req_count, _err_count


def reset_for_testing() -> None:
    """Drop cached RED instruments so a test can install a fresh MeterProvider."""
    global _req_duration, _req_count, _err_count
    _req_duration = None
    _req_count = None
    _err_count = None


def _record_request(method: str, status_code: int, duration_s: float) -> None:
    """Emit RED signals for one completed request via OTel and prometheus_client."""
    attrs: dict[str, Any] = {
        "http.request.method": method,
        "http.response.status_code": status_code,
    }
    duration, count, errors = _instruments()
    duration.record(duration_s, attrs)
    count.add(1, attrs)
    if status_code >= 500:
        errors.add(1, attrs)

    # Feed the existing prometheus RED collectors so /metrics is no longer dead
    # code. Endpoint label is the bounded method here, not the raw path, to keep
    # cardinality safe. Never let a metrics failure escape into the request.
    try:
        from shared.utils.metrics import get_management_metrics

        get_management_metrics().record_request(
            endpoint="", method=method, status_code=status_code, duration=duration_s
        )
    except Exception as exc:  # pragma: no cover - metrics must never break a request
        logger.debug("prometheus RED record failed (non-fatal): %s", exc)


class OTelASGIMiddleware:
    """ASGI middleware: one SERVER span + RED metrics per HTTP request.

    Wraps ``app.asgi_app`` so it sees every request, including ones that never
    reach a Quart route (404s, auth rejections). Only ``http`` scopes are
    instrumented; lifespan/websocket traffic passes straight through.
    """

    def __init__(self, app: Any, tracer: Tracer | None = None) -> None:
        """Wrap ``app``; ``tracer`` is resolved lazily from the global provider if None."""
        self._app = app
        self._tracer = tracer

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """Instrument HTTP scopes; pass everything else through untouched."""
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "")
        tracer = self._tracer or get_tracer()
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def _wrapped_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        with tracer.start_as_current_span(
            f"{method} {path}",
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": path},
        ) as span:
            try:
                await self._app(scope, receive, _wrapped_send)
            except Exception:
                span.set_status(Status(StatusCode.ERROR))
                _record_request(method, 500, time.perf_counter() - start)
                raise
            status = status_holder["code"]
            span.set_attribute("http.response.status_code", status)
            if status >= 500:
                span.set_status(Status(StatusCode.ERROR))
            _record_request(method, status, time.perf_counter() - start)


def instrument_sqlalchemy_engine(engine: Any, tracer: Tracer | None = None) -> bool:
    """Span every statement executed through ``engine`` (penguin-dal's runtime engine).

    penguin-dal runs all runtime queries through a SQLAlchemy engine, so a
    single set of Core cursor-execute listeners covers the whole service. The
    SQL *text* is recorded (truncated); bound parameter *values* never are --
    they can carry PII. Returns True when listeners were attached.
    """
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except Exception:  # pragma: no cover - sqlalchemy always present in this service
        return False

    if not isinstance(engine, Engine):
        # A mock DB (tests) or an uninitialised engine -- nothing to instrument.
        return False
    if getattr(engine, "_waddleai_otel_instrumented", False):
        return True

    tr = tracer or get_tracer()

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        span = tr.start_span(
            "db.query",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": getattr(engine.dialect, "name", "unknown"),
                "db.statement": (statement or "")[:1024],
            },
        )
        conn.info.setdefault("_waddleai_otel_spans", []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        spans = conn.info.get("_waddleai_otel_spans")
        if spans:
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def _on_error(exc_context):  # type: ignore[no-untyped-def]
        conn = exc_context.connection
        spans = conn.info.get("_waddleai_otel_spans") if conn is not None else None
        if spans:
            span = spans.pop()
            span.set_status(Status(StatusCode.ERROR))
            span.end()

    setattr(engine, "_waddleai_otel_instrumented", True)  # noqa: B010 - dynamic marker attr
    return True


def build_logging_handler(logger_provider: Any) -> logging.Handler:
    """Return an OTel ``LoggingHandler`` bridging stdlib logs to ``logger_provider``.

    Factored out so a test can attach it to an in-memory provider and prove a
    log record actually flows, rather than asserting a handler merely exists.
    """
    from opentelemetry.sdk._logs import LoggingHandler

    return LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)


_logs_initialised = False


def init_otel_logging() -> bool:
    """Bridge stdlib logging to OTLP log records; no-op when no endpoint is set.

    Returns True when the OTLP log pipeline was installed. Failure to build the
    exporter is swallowed -- telemetry must never stop the service from serving.
    """
    global _logs_initialised
    if _logs_initialised:
        return True

    from shared.observability.metrics import MetricsConfig  # reuse the same env contract

    cfg = MetricsConfig.from_env()
    if not cfg.otlp_endpoint:
        logger.info("OTel logs disabled (no OTEL_EXPORTER_OTLP_ENDPOINT)")
        _logs_initialised = True
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": cfg.service_name,
                "service.version": cfg.service_version,
                "deployment.environment": cfg.deployment_environment,
            }
        )
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=cfg.otlp_endpoint))
        )
        set_logger_provider(provider)
        logging.getLogger().addHandler(build_logging_handler(provider))
        logger.info("OTel logs exporting to %s", cfg.otlp_endpoint)
    except Exception as exc:  # pragma: no cover - exporter setup failure
        logger.warning("OTel logs init failed, continuing without export: %s", exc)

    _logs_initialised = True
    return True


def init_observability(app: Quart) -> None:
    """Wire traces + metrics + logs and instrument the ASGI app and DB engine.

    Idempotent and fail-safe: with no OTLP endpoint every provider is a no-op,
    so this is safe to call unconditionally from the app factory.
    """
    init_tracing()
    init_metrics()
    init_otel_logging()

    # Span every runtime DB query (penguin-dal runs them through this engine).
    from . import extensions

    if extensions.db is not None:
        try:
            if instrument_sqlalchemy_engine(extensions.db.engine):
                app.logger.info("SQLAlchemy engine instrumented for tracing")
        except Exception as exc:  # pragma: no cover - never block startup on telemetry
            app.logger.warning("SQLAlchemy instrumentation skipped: %s", exc)

    # Wrap the ASGI app last so the span/RED middleware is outermost. Mirrors
    # the proxy's own `app.asgi_app = <Middleware>(app.asgi_app)` assignments;
    # reassigning this framework hook is the documented ASGI-middleware pattern.
    app.asgi_app = OTelASGIMiddleware(app.asgi_app)  # type: ignore[method-assign,assignment]
    app.logger.info("OpenTelemetry request instrumentation applied")
