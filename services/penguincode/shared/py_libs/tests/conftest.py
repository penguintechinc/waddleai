"""Shared pytest fixtures for py_libs: in-memory OTel trace/metric capture.

OpenTelemetry's global TracerProvider/MeterProvider may only be *set* once per
process (a second ``set_tracer_provider``/``set_meter_provider`` call just logs
a warning and keeps the first one), so both are installed once, session-scoped,
with in-memory exporters/readers. Per-test fixtures clear the captured buffers
instead of re-installing providers.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture(scope="session")
def _span_exporter() -> InMemorySpanExporter:
    """Install a real TracerProvider backed by an in-memory exporter, once."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture(scope="session")
def _metric_reader() -> InMemoryMetricReader:
    """Install a real MeterProvider backed by an in-memory reader, once."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    from opentelemetry import metrics

    metrics.set_meter_provider(provider)
    return reader


@pytest.fixture
def span_exporter(_span_exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    """Per-test view of the session-wide span exporter, cleared before each test."""
    _span_exporter.clear()
    yield _span_exporter
    _span_exporter.clear()


@pytest.fixture
def metric_reader(_metric_reader: InMemoryMetricReader) -> InMemoryMetricReader:
    """Per-test view of the session-wide metric reader (no clear API; read fresh)."""
    return _metric_reader
