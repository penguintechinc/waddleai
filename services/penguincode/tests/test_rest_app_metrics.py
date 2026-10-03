"""`/metrics` route on the REST app: served, kill-switchable, unauthenticated.

# regression: fix/penguincode-metrics-endpoint-and-lint-gate -- O2 audit
# finding (PR #261's ServiceMonitor had nothing to scrape; penguincode was
# OTLP-push-only). The route lives on `rest_app.py` (not a dedicated port)
# because that is the exact Service port/path PR #261's ServiceMonitor
# already targets -- see `penguincode_cli/observability/otel.py`'s module
# docstring for the full rationale.

Every test resets `otel`'s module-level init state explicitly (not just via
`conftest.py`'s global kill-switch-default fixture) -- `otel._initialized`
is a process-wide latch that otherwise stays sticky across whichever test
ran first in the full suite, which would make this file's per-test
kill-switch toggling a no-op depending on test order.
"""

from collections.abc import Iterator

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.util._once import Once

from penguincode_cli.observability import otel


@pytest.fixture(autouse=True)
def _reset_otel_state() -> Iterator[None]:
    """Every test in this file starts from and ends on a fresh otel state."""
    otel.reset_for_testing()
    yield
    otel.reset_for_testing()


@pytest.fixture
def _fresh_meter_provider() -> Iterator[None]:
    """Reset the OTel API's one-shot MeterProvider latch for this test only.

    Same hermetic dance as `test_observability_otel.py`'s fixture of the
    same name -- without it, whichever test in the full suite first called
    `init_observability()` keeps the global MeterProvider forever (OTel's
    API treats `set_meter_provider()` as settable once per process), so a
    later test's own `PrometheusMetricReader` never actually receives the
    metrics it records.
    """
    prev_provider = otel_metrics._internal._METER_PROVIDER
    prev_once = otel_metrics._internal._METER_PROVIDER_SET_ONCE
    otel_metrics._internal._METER_PROVIDER = None
    otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()
    try:
        yield
    finally:
        otel_metrics._internal._METER_PROVIDER = prev_provider
        otel_metrics._internal._METER_PROVIDER_SET_ONCE = prev_once


class TestMetricsRouteDisabledByDefault:
    """`conftest.py`'s global fixture defaults the kill-switch ON for the whole suite."""

    async def test_returns_404_when_kill_switched(self, api_client):
        response = await api_client.get("/metrics")
        assert response.status_code == 404


class TestMetricsRouteEnabled:
    """Flag OFF (mechanism ON): the route serves real Prometheus text."""

    async def test_serves_prometheus_text(self, api_client, monkeypatch, _fresh_meter_provider):
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_PROMETHEUS_METRICS", "false")

        response = await api_client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.content_type
        body = (await response.get_data()).decode()
        type_lines = [line for line in body.splitlines() if line.startswith("# TYPE")]
        assert len(type_lines) >= 1

    async def test_reflects_the_shared_meter_instruments(
        self, api_client, monkeypatch, _fresh_meter_provider
    ):
        """A metric recorded via the normal store helpers must show up on the route."""
        monkeypatch.setenv("PENGUINCODE_FLAG_DISABLE_PROMETHEUS_METRICS", "false")
        otel.init_observability()
        otel.record_store_event("vector_query", outcome="ok")

        response = await api_client.get("/metrics")

        body = (await response.get_data()).decode()
        metric_name = otel.STORE_EVENTS_COUNTER_NAME.replace(".", "_")
        assert any(metric_name in line for line in body.splitlines())
