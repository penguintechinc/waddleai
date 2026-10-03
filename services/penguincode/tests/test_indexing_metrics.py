"""Tests for `penguincode_cli.indexing.metrics` (O10-a telemetry).

Mirrors `tests/test_observability_otel.py`'s hermetic in-memory-provider
pattern: captures real emitted data points, never just asserts an
instrument object exists.

# regression: penguincode-index-job-queue (O10-a -- load leveling for Index/IndexCode)
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

from penguincode_cli.indexing import metrics as index_metrics
from penguincode_cli.indexing.queue import IndexJobQueue
from penguincode_cli.observability import otel


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    otel.reset_for_testing()
    index_metrics.reset_for_testing()
    yield
    otel.reset_for_testing()
    index_metrics.reset_for_testing()


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """Install a real, in-memory-backed global MeterProvider -- see
    `tests/test_observability_otel.py::in_memory_exporters` for the hermetic
    run-once-latch dance this mirrors."""
    prev_provider = otel_metrics._internal._METER_PROVIDER
    prev_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE

    reader = InMemoryMetricReader()
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    otel_metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

    try:
        yield reader
    finally:
        otel_metrics._internal._METER_PROVIDER = prev_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_once


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


class TestQueueDepthGauge:
    def test_reports_the_summed_depth_of_every_registered_queue(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        queue_a = IndexJobQueue(maxsize=4)  # registers itself on construction
        queue_b = IndexJobQueue(maxsize=4)
        index_metrics.ensure_queue_depth_gauge_registered()

        points = _points(metric_reader, index_metrics.QUEUE_DEPTH_GAUGE_NAME)
        assert len(points) == 1
        assert points[0].value == 0  # both queues empty

        del queue_a, queue_b  # keep references alive until after the read above


class TestJobCountersAndHistograms:
    def test_record_job_enqueued_and_rejected_increment_the_counter(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        index_metrics.record_job_enqueued("index_docs")
        index_metrics.record_job_rejected("index_docs")

        points = _points(metric_reader, index_metrics.JOBS_COUNTER_NAME)
        assert len(points) >= 2
        states = {p.attributes.get("state") for p in points}
        assert {"queued", "rejected"} <= states

    def test_record_job_finished_increments_counter_and_records_duration(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        index_metrics.record_job_finished("index_code", "succeeded", 1.5)

        duration_points = _points(metric_reader, index_metrics.JOB_DURATION_HISTOGRAM_NAME)
        assert len(duration_points) == 1
        assert duration_points[0].sum == pytest.approx(1.5)

    def test_record_chunk_embed_duration_emits_a_histogram_point(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        index_metrics.record_chunk_embed_duration("index_docs", 0.42)

        points = _points(metric_reader, index_metrics.CHUNK_EMBED_DURATION_HISTOGRAM_NAME)
        assert len(points) == 1
        assert points[0].sum == pytest.approx(0.42)


class TestResetForTesting:
    def test_reset_drops_cached_instruments_and_registered_queues(
        self, metric_reader: InMemoryMetricReader
    ) -> None:
        queue = IndexJobQueue(maxsize=2)
        index_metrics.ensure_queue_depth_gauge_registered()
        assert queue in index_metrics._registered_queues

        index_metrics.reset_for_testing()

        assert index_metrics._queue_depth_gauge is None
        assert index_metrics._jobs_counter is None
        assert index_metrics._job_duration_histogram is None
        assert index_metrics._chunk_embed_duration_histogram is None
        assert len(index_metrics._registered_queues) == 0
