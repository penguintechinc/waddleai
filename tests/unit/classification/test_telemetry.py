"""Tests for classification telemetry (T11): metrics, spans, trace context, PII canary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import grpc
import pytest
import pytest_asyncio
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs
from test_servicer import (
    ORG,
    FakeCache,
    FakeEngine,
    FakeGate,
    FakePolicy,
    req,
)

from services.classification.app import engine as eng
from services.classification.app import telemetry as tel
from services.classification.app.config import ClassificationConfig
from services.classification.app.engine import RawEntity
from services.classification.app.grpc_gen import classification_pb2 as pb
from services.classification.app.grpc_gen import classification_pb2_grpc as pb_grpc
from services.classification.app.servicer import ClassificationServicer

CANARY = "CANARY-zq81x7-PII-do-not-leak"
NEW_METRICS = {
    "waddleai_classification_requests_total": {"outcome"},
    "waddleai_classification_duration_seconds": set(),
    "waddleai_classification_cache_total": {"result"},
    "waddleai_classification_engine_queue_depth": set(),
    "waddleai_classification_entities_total": {"entity_type"},
}


@dataclass(slots=True)
class Rig:
    """Servicer + doubles + exporters wired to a client stub."""

    stub: Any
    engine: FakeEngine
    cache: FakeCache
    spans: InMemorySpanExporter
    reader: InMemoryMetricReader
    telemetry: tel.Telemetry


def make_telemetry() -> tuple[tel.Telemetry, InMemorySpanExporter, InMemoryMetricReader]:
    """Build an isolated Telemetry over in-memory exporters (no global state)."""
    spans = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    return tel.build_telemetry(tp, MeterProvider(metric_readers=[reader])), spans, reader


class DepthEngine(FakeEngine):
    """FakeEngine exposing the real engine's queue_depth surface."""

    queue_depth = 0


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    """Serve an instrumented servicer on an ephemeral port."""
    telemetry, spans, reader = make_telemetry()
    engine, cache = DepthEngine(), FakeCache()
    servicer = ClassificationServicer(
        engine, cache, FakeGate(), FakePolicy(), max_text_bytes=4096, telemetry=telemetry
    )
    server = grpc.aio.server()
    pb_grpc.add_ClassificationServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield Rig(
            pb_grpc.ClassificationServiceStub(channel), engine, cache, spans, reader, telemetry
        )
    await server.stop(None)


def metric_points(reader: InMemoryMetricReader) -> dict[str, list[Any]]:
    """Collect {metric name: data points} from the reader."""
    data = reader.get_metrics_data()
    out: dict[str, list[Any]] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = list(m.data.data_points)
    return out


def label_sets(points: list[Any]) -> list[dict[str, Any]]:
    """Return each data point's attribute dict."""
    return [dict(p.attributes) for p in points]


async def test_analyze_emits_expected_metric_names_and_labels(rig: Rig) -> None:
    """One Analyze emits the five new metrics with exactly the documented label keys."""
    await rig.stub.Analyze(req())
    points = metric_points(rig.reader)
    for name, keys in NEW_METRICS.items():
        assert name in points, name
        for attrs in label_sets(points[name]):
            assert set(attrs) == keys, (name, attrs)
    assert {"outcome": "ok"} in label_sets(points["waddleai_classification_requests_total"])
    assert {"result": "miss"} in label_sets(points["waddleai_classification_cache_total"])
    ents = {a["entity_type"] for a in label_sets(points["waddleai_classification_entities_total"])}
    assert ents == {"PERSON", "LOCATION"}
    # Second call is a cache hit.
    await rig.stub.Analyze(req())
    cache_labels = label_sets(metric_points(rig.reader)["waddleai_classification_cache_total"])
    assert {"result": "hit"} in cache_labels


async def test_org_and_text_never_metric_labels(rig: Rig) -> None:
    """Org id, flag id and text never appear as a label key or value."""
    await rig.stub.Analyze(req(text=f"Alice {CANARY}"))
    for pts in metric_points(rig.reader).values():
        for attrs in label_sets(pts):
            for k, v in attrs.items():
                assert k not in {"org", "org_id", "tenant", "text", "flag_distinct_id"}
                assert ORG not in str(v) and "flag-id" not in str(v) and CANARY not in str(v)


async def test_histogram_observed_at_least_once(rig: Rig) -> None:
    """The duration histogram records one observation per request."""
    await rig.stub.Analyze(req())
    pts = metric_points(rig.reader)["waddleai_classification_duration_seconds"]
    assert sum(p.count for p in pts) >= 1


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [({"text": ""}, "ok"), ({"language": "fr"}, "invalid"), ({"api_version": "v9"}, "invalid")],
)
async def test_request_outcomes(rig: Rig, kwargs: dict[str, Any], outcome: str) -> None:
    """Early returns and aborts are all counted under their outcome label."""
    try:
        await rig.stub.Analyze(req(**kwargs))
    except grpc.aio.AioRpcError:
        pass
    pts = metric_points(rig.reader)["waddleai_classification_requests_total"]
    assert {"outcome": outcome} in label_sets(pts)


async def test_degraded_and_gate_disabled_outcomes() -> None:
    """Engine failure -> degraded; gate off -> gate_disabled."""
    telemetry, _spans, reader = make_telemetry()
    engine, gate = FakeEngine(), FakeGate()
    engine.error = eng.EngineUnavailable("down")
    svc = ClassificationServicer(engine, FakeCache(), gate, FakePolicy(), 4096, telemetry=telemetry)
    await svc.Analyze(req(), _Ctx())
    gate.on = False
    await svc.Analyze(req(), _Ctx())
    pts = label_sets(metric_points(reader)["waddleai_classification_requests_total"])
    assert {"outcome": "degraded"} in pts and {"outcome": "gate_disabled"} in pts


class _Ctx:
    """Minimal gRPC context double."""

    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        """Hold the invocation metadata."""
        self._md = metadata

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        """Return the scripted metadata."""
        return self._md

    def time_remaining(self) -> float | None:
        """No deadline."""
        return None

    async def abort(self, code: Any, details: str) -> None:
        """Raise like grpc.aio does."""
        raise grpc.aio.AbortError(details)


def attr_text(span: ReadableSpan) -> str:
    """Flatten a span's attributes, events, name and status into one string."""
    parts = [span.name, str(dict(span.attributes or {})), str(span.status.description)]
    for ev in span.events:
        parts += [ev.name, str(dict(ev.attributes or {}))]
    return " | ".join(parts)


async def test_span_has_no_text_or_entity_text(rig: Rig) -> None:
    """PII canary: the canary is in NO span, log record, or metric label value."""
    rig.engine.raw = [RawEntity("PERSON", 0, 5, 0.9), RawEntity(f"X_{CANARY}", 6, 20, 0.9)]
    text = f"Alice {CANARY} lives here"
    with capture_logs() as logs:
        await rig.stub.Analyze(req(text=text, org_public_id=ORG))
        rig.engine.error = eng.EngineAnalysisError(f"boom {CANARY}")
        await rig.stub.Analyze(req(text=f"other {CANARY}", org_public_id=ORG))  # degraded path
    spans = rig.spans.get_finished_spans()
    assert len(spans) == 2 and all(s.name == "classification.analyze" for s in spans)
    for s in spans:
        assert CANARY not in attr_text(s) and ORG not in attr_text(s)
        assert "flag-id" not in attr_text(s)
    first = dict(spans[0].attributes or {})
    assert first["text.chars"] == len(text)
    assert set(first) >= {
        "cache.hit",
        "ner.backend",
        "ner.status",
        "ner.entity_count",
        "text.chars",
    }
    assert CANARY not in str(logs)
    assert ORG not in str(logs)
    for pts in metric_points(rig.reader).values():
        for attrs in label_sets(pts):
            assert CANARY not in str(attrs)
    assert logs, "degraded path must have logged (denominator)"


async def test_unexpected_exception_text_never_reaches_span() -> None:
    """A plain RuntimeError(canary) escaping _analyze leaves no canary on the span."""
    telemetry, spans, _r = make_telemetry()
    svc = ClassificationServicer(
        FakeEngine(), FakeCache(), FakeGate(), FakePolicy(), 4096, telemetry=telemetry
    )

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(f"unexpected {CANARY}")

    svc._analyze = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await svc.Analyze(req(text=f"x {CANARY}"), _Ctx())
    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert not span.events and span.status.description is None
    assert CANARY not in attr_text(span)


def test_attribute_builder_failure_is_swallowed() -> None:
    """A raising attribute builder cannot break the caller's finally block."""
    telemetry, _s, _r = make_telemetry()

    def bad() -> dict[str, Any]:
        raise RuntimeError("backend gone")

    telemetry.set_attributes(object(), bad)


async def test_metrics_endpoint_includes_classification_metrics() -> None:
    """/metrics (default prometheus registry) exposes the new OTel classification metrics."""
    from services.classification.app import create_app

    cfg = ClassificationConfig.from_env()
    telemetry = tel.init_telemetry(cfg, set_global=False, prometheus=True)
    try:
        telemetry.record_request("ok")
        telemetry.record_cache("hit")
        telemetry.observe_duration(0.01)
        resp = await create_app(cfg, engine=object()).test_client().get("/metrics")
        body = (await resp.get_data()).decode()
        for name in (
            "waddleai_classification_requests_total",
            "waddleai_classification_cache_total",
            "waddleai_classification_duration_seconds",
        ):
            assert name in body, name
        assert "classification_auth_failures_total" in body  # existing prometheus metrics too
    finally:
        telemetry.shutdown(timeout_ms=500)  # unregisters the reader's collector


async def test_trace_context_propagated_from_metadata(rig: Rig) -> None:
    """A W3C traceparent in gRPC metadata makes the span a child of the caller's span."""
    trace_id, parent_id = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"
    await rig.stub.Analyze(req(), metadata=(("traceparent", f"00-{trace_id}-{parent_id}-01"),))
    (span,) = rig.spans.get_finished_spans()
    assert format(span.context.trace_id, "032x") == trace_id
    assert span.parent is not None and format(span.parent.span_id, "016x") == parent_id


async def test_malformed_traceparent_does_not_fail_request(rig: Rig) -> None:
    """Garbage trace headers are ignored; the request still succeeds."""
    resp = await rig.stub.Analyze(req(), metadata=(("traceparent", "garbage"),))
    assert resp.ner_status == pb.NER_STATUS_OK


def test_entity_type_label_cardinality_bounded() -> None:
    """Unknown labels/enum values collapse to OTHER; known ones keep their enum name."""
    assert tel.entity_type_label(pb.ENTITY_TYPE_PERSON) == "PERSON"
    assert tel.entity_type_label(pb.ENTITY_TYPE_OTHER) == "OTHER"
    assert tel.entity_type_label(987654) == "OTHER"
    assert tel.entity_type_label(pb.ENTITY_TYPE_UNSPECIFIED) == "OTHER"
    telemetry, _s, reader = make_telemetry()
    spans = [pb.EntitySpan(entity_type=n) for n in (987650, 987651, 987652)]
    telemetry.record_entities(spans)
    pts = metric_points(reader)["waddleai_classification_entities_total"]
    assert label_sets(pts) == [{"entity_type": "OTHER"}]
    assert pts[0].value == 3


async def test_dead_otlp_endpoint_does_not_fail_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead OTLP exporter is non-fatal at init and never fails a request."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "50")
    telemetry = tel.init_telemetry(ClassificationConfig.from_env(), set_global=False)
    assert telemetry.exporting is True
    svc = ClassificationServicer(
        FakeEngine(), FakeCache(), FakeGate(), FakePolicy(), 4096, telemetry=telemetry
    )
    resp = await svc.Analyze(req(), _Ctx())
    assert resp.ner_status == pb.NER_STATUS_OK
    telemetry.shutdown(timeout_ms=500)


def test_init_without_endpoint_is_noop_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OTEL_* endpoint -> no exporter (nothing hardcoded), still a working Telemetry."""
    for n in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    ):
        monkeypatch.delenv(n, raising=False)
    telemetry = tel.init_telemetry(ClassificationConfig.from_env(), set_global=False)
    assert telemetry.exporting is False
    telemetry.record_request("ok")


def test_init_never_raises_when_exporter_build_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken exporter constructor degrades to no export instead of raising."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bad exporter")

    monkeypatch.setattr(tel, "_make_exporters", boom)
    telemetry = tel.init_telemetry(ClassificationConfig.from_env(), set_global=False)
    assert telemetry.exporting is False


def test_sdk_disabled_skips_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """OTEL_SDK_DISABLED=true turns exporting off even with an endpoint."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert tel.init_telemetry(ClassificationConfig.from_env(), set_global=False).exporting is False


def test_emit_failures_are_swallowed() -> None:
    """Instrument failures never propagate to the request path."""
    telemetry, _s, _r = make_telemetry()

    class Boom:
        def add(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("instrument down")

        record = add

    telemetry.requests = Boom()  # type: ignore[assignment]
    telemetry.duration = Boom()  # type: ignore[assignment]
    telemetry.cache = Boom()  # type: ignore[assignment]
    telemetry.entities = Boom()  # type: ignore[assignment]
    telemetry.record_request("ok")
    telemetry.observe_duration(0.1)
    telemetry.record_cache("hit")
    telemetry.record_entities([pb.EntitySpan(entity_type=1)])
    telemetry.extract_context(object())  # no invocation_metadata -> root context, no raise


async def test_queue_depth_gauge_reports_engine_depth() -> None:
    """The gauge reads the bound engine's queue_depth at collection time."""
    telemetry, _s, reader = make_telemetry()

    class E(DepthEngine):
        queue_depth = 7

    ClassificationServicer(E(), FakeCache(), FakeGate(), FakePolicy(), 4096, telemetry=telemetry)
    pts = metric_points(reader)["waddleai_classification_engine_queue_depth"]
    assert pts[0].value == 7


async def test_cache_error_counted_via_cache_error_counter() -> None:
    """A cache whose cache_error counter moves during get/put is labelled result=error."""
    telemetry, _s, reader = make_telemetry()

    class ErrCache(FakeCache):
        cache_error = 0

        async def get(self, key: str) -> list[RawEntity] | None:
            self.cache_error += 1
            return None

    svc = ClassificationServicer(
        FakeEngine(), ErrCache(), FakeGate(), FakePolicy(), 4096, telemetry=telemetry
    )
    await svc.Analyze(req(), _Ctx())
    assert {"result": "error"} in label_sets(
        metric_points(reader)["waddleai_classification_cache_total"]
    )


async def test_unauth_outcome_from_interceptor() -> None:
    """A rejected (unauthenticated) call is counted as outcome=unauth."""
    from services.classification.app.auth import JwtAuthInterceptor

    telemetry, _s, reader = make_telemetry()

    class NoVerify:
        def verify_token(self, raw_token: str) -> Any:
            raise AssertionError("not reached")

    server = grpc.aio.server(interceptors=[JwtAuthInterceptor(NoVerify(), telemetry=telemetry)])
    pb_grpc.add_ClassificationServiceServicer_to_server(
        ClassificationServicer(FakeEngine(), FakeCache(), FakeGate(), FakePolicy(), 4096), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
        with pytest.raises(grpc.aio.AioRpcError) as ei:
            await pb_grpc.ClassificationServiceStub(ch).Analyze(req())
    await server.stop(None)
    assert ei.value.code() == grpc.StatusCode.UNAUTHENTICATED
    pts = metric_points(reader)["waddleai_classification_requests_total"]
    assert {"outcome": "unauth"} in label_sets(pts)
