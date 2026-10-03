"""Tests for `ToolCallbackServiceImpl`'s bounded per-session queue (O10, gRPC server hardening).

Covers: the queue bound default/env override/validation, explicit
full-queue rejection (never an indefinite block), the `enqueued`/`rejected`
OTel counter, and the `waddleai.disable-tool-queue-bound` kill-switch
reverting to an unbounded queue.

# regression: gRPC server hardening (O10 -- tool callback queue bound)
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

from penguincode_cli.observability import otel
from penguincode_cli.server.services.tools import ToolCallbackServiceImpl, _tool_queue_maxsize


@pytest.fixture(autouse=True)
def _reset_otel_module_state() -> Iterator[None]:
    otel.reset_for_testing()
    yield
    otel.reset_for_testing()


@pytest.fixture
def in_memory_metric_reader() -> Iterator[InMemoryMetricReader]:
    """Install a real in-memory MeterProvider so counter emission is provable."""
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


def _counter_points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in getattr(data, "resource_metrics", []) or []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


class TestQueueMaxsizeResolution:
    def test_default_is_256(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", raising=False)
        assert _tool_queue_maxsize() == 256

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", "10")
        assert _tool_queue_maxsize() == 10

    @pytest.mark.parametrize("raw", ["not-a-number", "", "0", "-3"])
    def test_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", raw)
        assert _tool_queue_maxsize() == 256


class TestRegisterSessionBound:
    @pytest.mark.asyncio
    async def test_register_session_bounds_the_queue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", "3")
        service = ToolCallbackServiceImpl()
        queue = await service.register_session("session-1")
        assert queue.maxsize == 3

    @pytest.mark.asyncio
    async def test_kill_switch_reverts_to_unbounded_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", "3")
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_TOOL_QUEUE_BOUND", "true")
        service = ToolCallbackServiceImpl()
        queue = await service.register_session("session-1")
        assert queue.maxsize == 0


class TestFullQueueRejection:
    @pytest.mark.asyncio
    async def test_full_queue_rejects_immediately_instead_of_blocking(
        self,
        monkeypatch: pytest.MonkeyPatch,
        in_memory_metric_reader: InMemoryMetricReader,
    ) -> None:
        monkeypatch.setenv("PENGUINCODE_TOOL_QUEUE_MAXSIZE", "1")
        service = ToolCallbackServiceImpl()
        await service.register_session("session-1")

        # Fill the single slot.
        first = asyncio.create_task(
            service.request_tool_execution("session-1", "bash", {"cmd": "ls"}, timeout_seconds=5)
        )
        await asyncio.sleep(0)  # let `first` enqueue and start waiting on its future

        # Second call must be rejected immediately (not block), well under
        # the 5s timeout above -- proven by `wait_for` here.
        second = await asyncio.wait_for(
            service.request_tool_execution("session-1", "bash", {"cmd": "ls"}, timeout_seconds=5),
            timeout=1.0,
        )
        assert second.success is False
        assert "queue full" in second.error.lower()

        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass

        points = _counter_points(in_memory_metric_reader, otel.TOOL_QUEUE_EVENTS_COUNTER_NAME)
        outcomes = {p.attributes["outcome"] for p in points}
        assert "enqueued" in outcomes
        assert "rejected" in outcomes

    @pytest.mark.asyncio
    async def test_rejected_request_is_not_left_pending(self) -> None:
        service = ToolCallbackServiceImpl()
        import os

        os.environ["PENGUINCODE_TOOL_QUEUE_MAXSIZE"] = "1"
        try:
            await service.register_session("session-2")
            first = asyncio.create_task(
                service.request_tool_execution(
                    "session-2", "bash", {"cmd": "ls"}, timeout_seconds=5
                )
            )
            await asyncio.sleep(0)
            second = await service.request_tool_execution(
                "session-2", "bash", {"cmd": "ls"}, timeout_seconds=5
            )
            assert second.success is False
            # The rejected request must never linger in _pending_requests.
            assert second.request_id not in service._pending_requests.get("session-2", {})
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
        finally:
            os.environ.pop("PENGUINCODE_TOOL_QUEUE_MAXSIZE", None)
