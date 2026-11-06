"""Shared OTel test fixtures: an in-memory meter provider and a data-point reader."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

from shared.observability import metrics as obs_metrics
from shared.observability import vllm_metrics


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """Install a real MeterProvider whose data points a test can read back.

    Hermetic: the process-global provider and its run-once latch are restored on teardown so
    no dead in-memory provider leaks into another module.
    """
    prev_provider = otel_metrics._internal._METER_PROVIDER
    prev_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
    reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    obs_metrics.reset_for_testing()
    vllm_metrics.reset_for_testing()
    try:
        yield reader
    finally:
        otel_metrics._internal._METER_PROVIDER = prev_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_once
        obs_metrics.reset_for_testing()
        vllm_metrics.reset_for_testing()


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    """Every data point recorded for one instrument name."""
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out
