# Elastic Pooled Quota — Phase 4d: Quota Telemetry

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec: house rule that logs, metrics and traces are mandatory (`critical-rules.md` Observability).

---

### Task 25: `QuotaTelemetry` — OTel instruments and spans

**Files:**
- Create: `shared/quota/telemetry.py`
- Test: `tests/unit/quota/test_telemetry.py`

**Interfaces:**
- Consumes: `shared.observability.metrics.get_meter()` and `shared.observability.tracing.get_tracer()` (no-ops when no OTLP endpoint is configured).
- Produces: `QuotaTelemetry(meter=None, tracer=None)` with `decision(outcome: str, reason: str | None = None)`, `rejection(reason: str)`, `lease(credits: int)`, `reserved(credits: int)`, `reconciled(applied: bool, credits: int)`, `overage(kind: str, amount: int)`, `reserve_seconds(seconds: float)`, `span(name: str)` (context manager yielding an OTel span). Instruments: counters `waddleai.quota.decisions`, `waddleai.quota.rejections`, `waddleai.quota.buffer_leases`, `waddleai.quota.reconciles`, `waddleai.quota.overages`; histograms `waddleai.quota.credits_reserved`, `waddleai.quota.credits_actual`, `waddleai.quota.buffer_lease_credits`, `waddleai.quota.reserve_duration`. Attributes are ONLY bounded enums (`outcome`, `reason`, `applied`, `kind`) — never user/team/org ids.

- [ ] **Step 1: Write the failing test**

`tests/unit/quota/test_telemetry.py`:

```python
"""OTel instruments exist, record bounded attributes only, and spans are emitted."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shared.quota.telemetry import QuotaTelemetry


def _points(reader):
    """name -> list of data points from an InMemoryMetricReader."""
    out = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


def test_every_instrument_records_with_bounded_attributes_only():
    """All nine instruments emit; attribute keys are a closed enum set (no user/team/org ids)."""
    reader = InMemoryMetricReader()
    tele = QuotaTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tele.decision("denied", "quota_exhausted")
    tele.rejection("quota_exhausted")
    tele.lease(7)
    tele.reserved(12)
    tele.reconciled(True, 9)
    tele.overage("buffer", 3)
    tele.reserve_seconds(0.01)
    points = _points(reader)
    assert set(points) == {
        "waddleai.quota.decisions", "waddleai.quota.rejections", "waddleai.quota.buffer_leases",
        "waddleai.quota.reconciles", "waddleai.quota.overages", "waddleai.quota.credits_reserved",
        "waddleai.quota.credits_actual", "waddleai.quota.buffer_lease_credits",
        "waddleai.quota.reserve_duration",
    }  # fmt: skip
    assert dict(points["waddleai.quota.decisions"][0].attributes) == {"outcome": "denied", "reason": "quota_exhausted"}
    assert (points["waddleai.quota.credits_reserved"][0].count, points["waddleai.quota.credits_reserved"][0].sum) == (1, 12)
    assert points["waddleai.quota.buffer_lease_credits"][0].sum == 7
    for dps in points.values():
        for dp in dps:
            assert not any(any(s in k for s in ("user", "team", "org")) for k in dp.attributes)


def test_span_is_emitted_with_attributes():
    """span() wraps a block in a named OTel span."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tele = QuotaTelemetry(meter=MeterProvider().get_meter("t"), tracer=provider.get_tracer("t"))
    with tele.span("quota.reserve") as span:
        span.set_attribute("quota.blocked", False)
    (finished,) = exporter.get_finished_spans()
    assert finished.name == "quota.reserve" and finished.attributes["quota.blocked"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/quota/test_telemetry.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.telemetry`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/telemetry.py`:

```python
"""OpenTelemetry instruments + spans for the pooled quota (house rule: logs, metrics, traces).

Histograms carry credit amounts and latency; counters carry decisions, 429s, buffer leases,
reconciles and overages. Attributes are CLOSED enums only (outcome/reason/applied/kind): user,
team and org ids are PII-adjacent and unbounded, so they never become metric or span attributes
(counts only). With no OTLP endpoint configured the underlying meter/tracer are no-ops.
"""

from contextlib import AbstractContextManager
from typing import Any

from shared.observability.metrics import get_meter
from shared.observability.tracing import get_tracer


class QuotaTelemetry:
    """Creates the quota instruments once and exposes one method per signal."""

    def __init__(self, meter: Any = None, tracer: Any = None) -> None:
        """Build instruments on ``meter`` (default: the process meter) and keep a tracer."""
        meter = meter or get_meter()
        self.tracer = tracer or get_tracer("waddleai-quota")
        self._decisions = meter.create_counter(
            "waddleai.quota.decisions", unit="1", description="Reserve outcomes (allowed/denied/noop/degraded)")
        self._rejections = meter.create_counter(
            "waddleai.quota.rejections", unit="1", description="Requests refused with 429 by the quota")
        self._leases = meter.create_counter(
            "waddleai.quota.buffer_leases", unit="1", description="Reserves that leased from the team buffer")
        self._reconciles = meter.create_counter(
            "waddleai.quota.reconciles", unit="1", description="Reconcile attempts by applied/orphan")
        self._overages = meter.create_counter(
            "waddleai.quota.overages", unit="1", description="Reconciles that exceeded a buffer or override cap")
        self._reserved = meter.create_histogram(
            "waddleai.quota.credits_reserved", unit="credit", description="Credits reserved per request")
        self._actual = meter.create_histogram(
            "waddleai.quota.credits_actual", unit="credit", description="Actual credits per reconciled request")
        self._lease = meter.create_histogram(
            "waddleai.quota.buffer_lease_credits", unit="credit", description="Credits leased from the buffer")
        self._latency = meter.create_histogram(
            "waddleai.quota.reserve_duration", unit="s", description="Reserve latency including config load")

    def decision(self, outcome: str, reason: str | None = None) -> None:
        """Count one reserve outcome (``reason`` only when there is one)."""
        attrs = {"outcome": outcome} | ({"reason": reason} if reason else {})
        self._decisions.add(1, attrs)

    def rejection(self, reason: str) -> None:
        """Count one 429 by reason."""
        self._rejections.add(1, {"reason": reason})

    def lease(self, credits: int) -> None:
        """Record one buffer lease and its size."""
        self._leases.add(1)
        self._lease.record(credits)

    def reserved(self, credits: int) -> None:
        """Record the credits reserved for an admitted request."""
        self._reserved.record(credits)

    def reconciled(self, applied: bool, credits: int) -> None:
        """Count a reconcile and record the actual credits when it applied."""
        self._reconciles.add(1, {"applied": str(applied).lower()})
        if applied:
            self._actual.record(credits)

    def overage(self, kind: str, amount: int) -> None:
        """Count an overage (``kind`` = buffer | override); ``amount`` is informational."""
        self._overages.add(1, {"kind": kind})

    def reserve_seconds(self, seconds: float) -> None:
        """Record reserve latency."""
        self._latency.record(seconds)

    def span(self, name: str) -> AbstractContextManager[Any]:
        """Open a span named ``name`` (callers set bounded attributes only)."""
        return self.tracer.start_as_current_span(name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/quota/test_telemetry.py -v --no-cov`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add shared/quota/telemetry.py tests/unit/quota/test_telemetry.py
git commit -m "feat(quota): QuotaTelemetry — OTel counters, histograms and spans (bounded attributes)"
git push
```
