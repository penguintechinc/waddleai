//! The latency histogram gets exactly one sample per attempt, including a stream dropped by a
//! client disconnect (own binary: the global meter provider is process-wide).

mod common;

use common::{ctx, raw_server, simple, After, Piece};
use futures_util::StreamExt;
use opentelemetry::global;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use pipeline::Dispatcher;

/// Collects `(status, count)` pairs from the exported latency histogram.
fn samples(exp: &InMemoryMetricExporter) -> Vec<(String, u64)> {
    let mut out = Vec::new();
    for rm in exp.get_finished_metrics().unwrap() {
        for sm in rm.scope_metrics() {
            for m in sm.metrics() {
                if m.name() != "llm_request_duration_seconds" {
                    continue;
                }
                if let AggregatedMetrics::F64(MetricData::Histogram(h)) = m.data() {
                    for dp in h.data_points() {
                        let status = dp
                            .attributes()
                            .find(|kv| kv.key.as_str() == "status")
                            .map(|kv| kv.value.to_string())
                            .unwrap_or_default();
                        out.push((status, dp.count()));
                    }
                }
            }
        }
    }
    out
}

#[tokio::test]
async fn disconnected_stream_still_records_a_latency_sample() {
    let exp = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exp.clone()).build())
        .build();
    global::set_meter_provider(provider.clone());

    let (url, closed) = raw_server(
        "application/x-ndjson",
        vec![Piece {
            bytes: b"{\"message\":{\"content\":\"one\"},\"done\":false}\n".to_vec(),
            delay_ms: 0,
        }],
        After::HoldUntilClientCloses,
    )
    .await;
    let d = simple(&url);
    let mut c = ctx("m");
    c.stream = true;
    {
        let mut s = d.stream_dispatch(&mut c);
        assert_eq!(s.next().await.unwrap().unwrap().delta, "one");
    }
    closed.await.unwrap();
    provider.force_flush().unwrap();
    let got = samples(&exp);
    assert_eq!(got, vec![("aborted".to_owned(), 1)], "got {got:?}");
}
