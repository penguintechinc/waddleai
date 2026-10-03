"""Tests for `penguincode_cli.sessions.metrics` -- the O4-a (High) instruments.

No DB or live OTLP collector required: `observability.otel`'s no-op
providers (installed when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset) are
sufficient to exercise instrument construction and recording.

# regression: penguincode-shared-chat-sessions (O4-a High -- sessions.metrics)
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from penguincode_cli.sessions import metrics as sessions_metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    sessions_metrics.reset_for_testing()
    yield
    sessions_metrics.reset_for_testing()


class TestRecordActiveSessions:
    def test_does_not_raise_and_builds_the_gauge_lazily(self) -> None:
        sessions_metrics.record_active_sessions(3)
        # Second call reuses the already-built instrument.
        sessions_metrics.record_active_sessions(0)


class TestTimedSessionStoreOperation:
    def test_records_duration_on_success(self) -> None:
        with sessions_metrics.timed_session_store_operation("get"):
            pass

    def test_records_duration_even_when_block_raises(self) -> None:
        with pytest.raises(ValueError), sessions_metrics.timed_session_store_operation("create"):
            raise ValueError("boom")


class TestResetForTesting:
    def test_clears_cached_instruments(self) -> None:
        sessions_metrics.record_active_sessions(1)
        assert sessions_metrics._active_sessions_gauge is not None
        sessions_metrics.reset_for_testing()
        assert sessions_metrics._active_sessions_gauge is None
        assert sessions_metrics._session_store_duration_histogram is None
