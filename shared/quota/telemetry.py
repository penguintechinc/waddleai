"""OpenTelemetry instruments + spans for the pooled quota (house rule: logs, metrics, traces).

Histograms carry credit amounts and latency; counters carry decisions, 429s, buffer leases,
reconciles, overages and faults. Attributes are CLOSED enums only (outcome/reason/applied/kind):
user, team and org ids are PII-adjacent and unbounded, so they never become metric or span
attributes. Emission is best-effort: a dead exporter or broken instrument is logged at DEBUG and
swallowed, never raised into the reserve/reconcile path. With no OTLP endpoint configured the
underlying meter/tracer are no-ops; the endpoint itself comes from standard OTLP env vars.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import INVALID_SPAN

from shared.observability.metrics import get_meter
from shared.observability.tracing import get_tracer

logger = logging.getLogger(__name__)


class _NoopInstrument:
    """Stand-in used when an instrument could not be created."""

    def add(self, *args: Any, **kwargs: Any) -> None:
        """Discard a counter increment."""

    def record(self, *args: Any, **kwargs: Any) -> None:
        """Discard a histogram sample."""


class QuotaTelemetry:
    """Creates the quota instruments once and exposes one best-effort method per signal."""

    def __init__(self, meter: Any = None, tracer: Any = None) -> None:
        """Build instruments on ``meter`` (default: the process meter) and keep a tracer."""
        meter = meter or get_meter()
        self.tracer = tracer or get_tracer("waddleai-quota")
        self._decisions = self._counter(
            meter, "waddleai.quota.decisions", "Reserve outcomes (allowed/denied/noop/degraded)"
        )
        self._rejections = self._counter(
            meter, "waddleai.quota.rejections", "Requests refused with 429 by the quota"
        )
        self._leases = self._counter(
            meter, "waddleai.quota.buffer_leases", "Reserves that leased from the team buffer"
        )
        self._reconciles = self._counter(
            meter, "waddleai.quota.reconciles", "Reconcile attempts by applied/orphan"
        )
        self._overages = self._counter(
            meter, "waddleai.quota.overages", "Reconciles that exceeded a buffer or override cap"
        )
        self._faults = self._counter(
            meter,
            "waddleai.quota.faults",
            "Script errors, lost refunds and fail-open events by kind",
        )
        self._reserved = self._histogram(
            meter, "waddleai.quota.credits_reserved", "credit", "Credits reserved"
        )
        self._actual = self._histogram(
            meter, "waddleai.quota.credits_actual", "credit", "Actual credits reconciled"
        )
        self._lease = self._histogram(
            meter, "waddleai.quota.buffer_lease_credits", "credit", "Credits leased from the buffer"
        )
        self._latency = self._histogram(
            meter, "waddleai.quota.reserve_duration", "s", "Reserve latency incl. config"
        )

    @staticmethod
    def _counter(meter: Any, name: str, description: str) -> Any:
        """Create a counter, degrading to a no-op if the meter is broken."""
        try:
            return meter.create_counter(name, unit="1", description=description)
        except Exception:
            logger.debug("quota telemetry: counter %s unavailable", name, exc_info=True)
            return _NoopInstrument()

    @staticmethod
    def _histogram(meter: Any, name: str, unit: str, description: str) -> Any:
        """Create a histogram, degrading to a no-op if the meter is broken."""
        try:
            return meter.create_histogram(name, unit=unit, description=description)
        except Exception:
            logger.debug("quota telemetry: histogram %s unavailable", name, exc_info=True)
            return _NoopInstrument()

    @staticmethod
    def _safe(call: Any, *args: Any) -> None:
        """Run one emission, swallowing any exporter/instrument failure."""
        try:
            call(*args)
        except Exception:
            logger.debug("quota telemetry emission failed", exc_info=True)

    def decision(self, outcome: str, reason: str | None = None) -> None:
        """Count one reserve outcome (``reason`` only when there is one)."""
        attrs = {"outcome": outcome} | ({"reason": reason} if reason else {})
        self._safe(self._decisions.add, 1, attrs)

    def rejection(self, reason: str) -> None:
        """Count one 429 by reason."""
        self._safe(self._rejections.add, 1, {"reason": reason})

    def lease(self, credits: int) -> None:
        """Record one buffer lease and its size."""
        self._safe(self._leases.add, 1)
        self._safe(self._lease.record, credits)

    def reserved(self, credits: int) -> None:
        """Record the credits reserved for an admitted request."""
        self._safe(self._reserved.record, credits)

    def reconciled(self, applied: bool, credits: int) -> None:
        """Count a reconcile and record the actual credits when it applied."""
        self._safe(self._reconciles.add, 1, {"applied": str(applied).lower()})
        if applied:
            self._safe(self._actual.record, credits)

    def overage(self, kind: str, amount: int) -> None:
        """Count an overage (``kind`` = buffer | override); ``amount`` is informational."""
        self._safe(self._overages.add, 1, {"kind": kind})

    def fault(self, kind: str) -> None:
        """Count a reconcile script error, lost refund or fail-open so it is visible, not silent."""
        self._safe(self._faults.add, 1, {"kind": kind})

    def reserve_seconds(self, seconds: float) -> None:
        """Record reserve latency."""
        self._safe(self._latency.record, seconds)

    @contextmanager
    def span(self, name: str) -> Iterator[Any]:
        """Open a span named ``name``; tracer failure yields a no-op span, body errors propagate."""
        try:
            cm = self.tracer.start_as_current_span(name)
            span = cm.__enter__()
        except Exception:
            logger.debug("quota telemetry: span %s unavailable", name, exc_info=True)
            yield INVALID_SPAN
            return
        try:
            yield span
        except BaseException as exc:
            self._safe(cm.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            self._safe(cm.__exit__, None, None, None)
