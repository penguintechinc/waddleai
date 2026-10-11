#!/usr/bin/env python3
"""Smoke gate: OTel emission (gh-finding-O3) + hand-rolled-logging conformance.

`make smoke-test` previously asserted only file existence and Python syntax
(tests/smoke/test_management_build.sh) -- it never proved the app actually
emits telemetry, so a regression that silently broke OTel wiring (a declared-
but-never-emitted instrument, a handler that never attaches) would still pass
every commit (critical-rules.md Verification Integrity).

This script drives the exact, already-proven instrumentation code in
``services.management.app.observability`` (``OTelASGIMiddleware`` +
``build_logging_handler``, the same objects
``tests/unit/management/test_management_observability.py::
test_all_three_signals_present`` exercises) through one fake ASGI request
against freshly-installed in-memory OTel providers, then asserts and PRINTS
real counts for every signal -- a sink that fails to start, or a zero count
on any signal, is a FAILURE, never a silent skip.

Separately scans proxy/services/management/shared source for hand-rolled
logging (`logging.basicConfig(`, bare `print(`) that bypasses the
OTel-bridged `logging` module this service actually uses.

Exit 0 only when every assertion passes. Run directly (`python3
tests/smoke/test_telemetry_emission.py`) or via `make smoke-test`.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_in_memory_metrics():
    """Install a fresh global MeterProvider backed by an InMemoryMetricReader.

    Mirrors tests/unit/management/test_management_observability.py's
    `metric_reader` fixture exactly: the global OTel MeterProvider is guarded
    by a run-once latch, so the latch and cached provider are both reset
    before installing this process's own reader as the new global one.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.util._once import Once

    from services.management.app import observability as mgmt_obs
    from shared.observability import metrics as shared_metrics

    reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    shared_metrics.reset_for_testing()
    mgmt_obs.reset_for_testing()
    return reader


def _metric_points(reader, name: str) -> list:
    """Every data point recorded for instrument `name` (counter or histogram)."""
    data = reader.get_metrics_data()
    out: list = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


async def _drive_fake_request(middleware) -> None:
    """Push one synthetic HTTP request through an ASGI middleware instance."""

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/smoke/telemetry"}
    await middleware(scope, receive, send)


def check_telemetry_emission() -> bool:
    """Assert >=1 span, >=1 counter point, >=1 histogram point, >=1 log record.

    Returns False (never raises past this function) on any failure, including
    a sink that fails to start -- that counts as a FAILURE, not a skip.
    """
    import asyncio

    print("-- OTel emission (spans, metrics, logs) --")
    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from services.management.app import observability as mgmt_obs
    except Exception as exc:  # pragma: no cover - import/setup failure
        print(f"!! OTel in-memory sink failed to start: {exc} -- counting as FAILURE")
        return False

    try:
        metric_reader = _install_in_memory_metrics()

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        tracer = tracer_provider.get_tracer("smoke-telemetry")

        async def _inner_app(scope: dict, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = mgmt_obs.OTelASGIMiddleware(_inner_app, tracer=tracer)
        asyncio.run(_drive_fake_request(middleware))

        log_exporter = InMemoryLogExporter()  # type: ignore[no-untyped-call]
        log_provider = LoggerProvider()
        log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        handler = mgmt_obs.build_logging_handler(log_provider)
        smoke_logger = logging.getLogger("waddleai.smoke.telemetry")
        smoke_logger.setLevel(logging.INFO)
        smoke_logger.addHandler(handler)
        try:
            smoke_logger.info("smoke-test telemetry emission check")
        finally:
            smoke_logger.removeHandler(handler)
        log_provider.force_flush()
    except Exception as exc:  # pragma: no cover - instrumentation call failure
        print(f"!! OTel emission run failed: {exc} -- counting as FAILURE")
        return False

    spans = span_exporter.get_finished_spans()
    request_points = _metric_points(metric_reader, "http.server.requests")
    histogram_points = _metric_points(metric_reader, "http.server.request.duration")
    log_records = log_exporter.get_finished_logs()

    print(f"spans received: {len(spans)}")
    print(f"counter (http.server.requests) data points received: {len(request_points)}")
    print(f"histogram (http.server.request.duration) data points received: {len(histogram_points)}")
    print(f"log records received: {len(log_records)}")

    ok = True
    if len(spans) < 1:
        print("!! 0 spans received -- FAILURE")
        ok = False
    if len(request_points) < 1:
        print("!! 0 counter data points received -- FAILURE")
        ok = False
    if len(histogram_points) < 1:
        print(
            "!! 0 histogram data points received -- FAILURE "
            "(load/latency histograms are the most-often-missing signal)"
        )
        ok = False
    if len(log_records) < 1:
        print("!! 0 log records received -- FAILURE")
        ok = False
    return ok


# Hand-rolled logging patterns this service must never use in production
# source -- it bridges stdlib `logging` through the OTel LoggingHandler
# (services/management/app/observability.py, shared/observability/), so a
# `logging.basicConfig(...)` call elsewhere would silently fight that
# bridge, and a bare `print(...)` bypasses it (and OTel) entirely.
_FORBIDDEN_PATTERNS = (
    re.compile(r"\blogging\.basicConfig\s*\("),
    re.compile(r"(?<![\w.])print\s*\("),
)

# Scoped to service source that actually ships -- never tests/, scripts/
# (CLI output is a legitimate `print()` use per testing.md), or vendored/
# generated code.
_SCAN_ROOTS = ("shared", "proxy", "services/management/app")
_EXCLUDE_DIR_PARTS = {".venv", "venv", "__pycache__", ".git", "node_modules"}


def _iter_source_files():
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if _EXCLUDE_DIR_PARTS & set(path.parts):
                continue
            yield path


def check_logging_conformance() -> bool:
    """Scan service source for hand-rolled logging that bypasses the OTel bridge.

    Prints the number of files scanned (a zero denominator is itself a
    failure -- a scanner pointed at the wrong root reports clean) and the
    number of forbidden calls found.
    """
    print("-- hand-rolled logging conformance (shared/, proxy/, services/management/app) --")
    files = list(_iter_source_files())
    print(f"service source files scanned: {len(files)}")
    if len(files) == 0:
        print("!! 0 source files scanned -- scan root is wrong, counting as FAILURE")
        return False

    violations: list[str] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"!! could not read {path}: {exc} -- counting as FAILURE")
            violations.append(str(path))
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Strip a naive `#`-comment tail before matching -- this scan is a
            # lightweight heuristic, not a parser, and a comment *documenting*
            # why hand-rolled logging must be avoided (e.g. this very file's
            # own services/management/app/__init__.py fix) would otherwise
            # false-positive on itself.
            code = line.split("#", 1)[0]
            for pattern in _FORBIDDEN_PATTERNS:
                if pattern.search(code):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    print(f"hand-rolled logging calls found: {len(violations)}")
    if violations:
        for v in violations:
            print(f"!! {v}")
        print(
            "!! hand-rolled logging.basicConfig()/print() in service source -- counting as FAILURE"
        )
        return False
    return True


def main() -> int:
    """Run both gates; FAIL (non-zero exit) if either one fails."""
    telemetry_ok = check_telemetry_emission()
    logging_ok = check_logging_conformance()
    if telemetry_ok and logging_ok:
        print("=== telemetry + logging conformance smoke gate: PASS ===")
        return 0
    print("=== telemetry + logging conformance smoke gate: FAILED ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
