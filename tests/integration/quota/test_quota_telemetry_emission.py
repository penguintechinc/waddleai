"""Telemetry emission gate (testing.md): logs, metrics, histograms and spans all > 0."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shared.quota.telemetry import QuotaTelemetry
from tests.integration.quota.world import MSG, build_world, user_of


def _collect(reader):
    """Name -> data points, across every scope/resource."""
    out = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


async def test_scenario_emits_logs_metrics_histograms_and_spans(team_world, valkey_async, caplog):
    """Allow x7, buffer leases, one 429, a reconcile and an orphan: every signal is received."""
    reader, exporter = InMemoryMetricReader(), InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tele = QuotaTelemetry(
        meter=MeterProvider(metric_readers=[reader]).get_meter("t"),
        tracer=tracer_provider.get_tracer("t"),
    )
    w = build_world(team_world, valkey_async, credits=4, tele=tele)
    mia = user_of(w, "mia")
    with caplog.at_level("DEBUG", logger="shared.quota"):
        outcomes = [await w.engine.reserve(mia, "gpt-4o", MSG) for _ in range(8)]
        first = outcomes[0].reservation
        applied = await w.engine.reconcile(
            first, actual_credits=3, provider="openai", model="gpt-4o"
        )
        orphan = await w.engine.reconcile(
            first, actual_credits=3, provider="openai", model="gpt-4o"
        )
    assert [o.blocked for o in outcomes] == [False] * 7 + [
        True
    ]  # 20/4=5 reservation + 2 buffer leases, 8th is a 429
    assert applied.applied is True and orphan.applied is False

    logs = [r for r in caplog.records if r.name.startswith("shared.quota")]
    points = _collect(reader)
    metric_points = sum(len(v) for v in points.values())
    histogram_points = sum(len(v) for k, v in points.items() if hasattr(v[0], "bucket_counts"))
    spans = exporter.get_finished_spans()
    summary = (
        f"logs={len(logs)} metric_points={metric_points} "
        f"histogram_points={histogram_points} spans={len(spans)}"
    )
    print(f"quota telemetry emission: {summary}")
    assert len(logs) >= 1, summary
    assert metric_points >= 1, summary
    assert histogram_points >= 1, summary
    assert len(spans) >= 1, summary
    assert {p.attributes["outcome"] for p in points["waddleai.quota.decisions"]} >= {
        "allowed",
        "denied",
    }
    assert sum(p.value for p in points["waddleai.quota.rejections"]) == 1
    assert sum(p.value for p in points["waddleai.quota.buffer_leases"]) == 2
    assert {p.attributes["applied"] for p in points["waddleai.quota.reconciles"]} == {
        "true",
        "false",
    }
