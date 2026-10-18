"""OTel instruments exist, record bounded attributes only, and spans are emitted."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shared.quota.telemetry import QuotaTelemetry


def _points(reader):
    """Name -> list of data points from an InMemoryMetricReader."""
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
    assert dict(points["waddleai.quota.decisions"][0].attributes) == {
        "outcome": "denied",
        "reason": "quota_exhausted",
    }
    assert (
        points["waddleai.quota.credits_reserved"][0].count,
        points["waddleai.quota.credits_reserved"][0].sum,
    ) == (1, 12)
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


def test_fault_counter_and_lease_have_no_attrs_beyond_enums():
    """fault() makes lost-refund/script-error/fail-open visible with a bounded kind."""
    reader = InMemoryMetricReader()
    tele = QuotaTelemetry(meter=MeterProvider(metric_readers=[reader]).get_meter("t"))
    tele.fault("reconcile_script_error")
    tele.decision("allowed")
    pts = _points(reader)
    assert dict(pts["waddleai.quota.faults"][0].attributes) == {"kind": "reconcile_script_error"}
    assert dict(pts["waddleai.quota.decisions"][0].attributes) == {"outcome": "allowed"}


class _Boom:
    """Instrument stand-in whose every call raises, like a dead exporter."""

    def add(self, *a, **k):
        raise RuntimeError("exporter dead")

    record = add


class _BoomMeter:
    """Meter whose instruments all raise on use."""

    def create_counter(self, *a, **k):
        return _Boom()

    create_histogram = create_counter


class _BoomTracer:
    """Tracer that raises when a span is started."""

    def start_as_current_span(self, name):
        raise RuntimeError("tracer dead")


def test_dead_exporter_is_swallowed():
    """No emission method or span raises to the caller when instruments/tracer fail."""
    tele = QuotaTelemetry(meter=_BoomMeter(), tracer=_BoomTracer())
    tele.decision("denied", "x")
    tele.rejection("x")
    tele.lease(1)
    tele.reserved(1)
    tele.reconciled(True, 1)
    tele.reconciled(False, 0)
    tele.overage("buffer", 1)
    tele.reserve_seconds(0.1)
    tele.fault("k")
    with tele.span("quota.reserve") as span:
        span.set_attribute("a", 1)


def test_span_body_exception_propagates_and_failed_meter_creation_is_survivable():
    """A caller's own exception still propagates; broken instrument creation does not raise."""
    import pytest

    class _NoMeter:
        def create_counter(self, *a, **k):
            raise RuntimeError("x")

        create_histogram = create_counter

    tele = QuotaTelemetry(meter=_NoMeter(), tracer=TracerProvider().get_tracer("t"))
    tele.decision("allowed")
    with pytest.raises(ValueError):
        with tele.span("s"):
            raise ValueError("body")
