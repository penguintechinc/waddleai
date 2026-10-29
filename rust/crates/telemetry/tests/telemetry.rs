//! Telemetry init: a dead/garbage exporter must never error the app.
use std::time::{Duration, Instant};

use config::OtelCfg;
use telemetry::{init_telemetry, GenAiMetrics};

fn cfg(endpoint: Option<&str>, sampler: &str) -> OtelCfg {
    OtelCfg {
        endpoint: endpoint.map(str::to_owned),
        service_name: "waddleai-test".into(),
        environment: "test".into(),
        traces_sampler: sampler.into(),
    }
}

fn emit_everything() {
    let span = tracing::info_span!("t7.span", k = "v");
    let _e = span.enter();
    tracing::info!("event inside span");
    let g = GenAiMetrics::new();
    g.record_tokens("chat", "openai", "gpt-x", "input", 12);
    g.record_duration("chat", "openai", "gpt-x", 0.25);
}

#[tokio::test(flavor = "multi_thread")]
async fn dead_exporter_does_not_error() {
    // Nothing listens on port 1: connection refused on every export.
    let guard = init_telemetry(&cfg(Some("http://127.0.0.1:1"), "always_on"));
    assert!(guard.exporting_traces(), "pipeline should still be built");
    assert!(guard.exporting_metrics());
    emit_everything();
    let t = Instant::now();
    drop(guard);
    assert!(
        t.elapsed() < Duration::from_secs(8),
        "shutdown must be bounded"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn garbage_endpoint_does_not_error() {
    let guard = init_telemetry(&cfg(Some("not a url ::"), "always_on"));
    emit_everything();
    drop(guard);
}

#[tokio::test]
async fn no_endpoint_means_no_exporter() {
    let guard = init_telemetry(&cfg(None, "always_on"));
    assert!(!guard.exporting_traces());
    assert!(!guard.exporting_metrics());
    emit_everything();
}

#[tokio::test(flavor = "multi_thread")]
async fn sampler_variants_all_initialise() {
    for s in [
        "always_on",
        "always_off",
        "parentbased_always_on",
        "parentbased_always_off",
        "traceidratio",
    ] {
        let g = init_telemetry(&cfg(Some("http://127.0.0.1:1"), s));
        assert!(g.exporting_traces(), "{s}");
        emit_everything();
        drop(g);
    }
}

#[test]
fn signal_url_joins_without_double_slash() {
    assert_eq!(
        telemetry::init::signal_url("http://c:4318/", "traces"),
        "http://c:4318/v1/traces"
    );
    assert_eq!(
        telemetry::init::signal_url("http://c:4318", "metrics"),
        "http://c:4318/v1/metrics"
    );
}

#[test]
fn genai_names_follow_semconv() {
    use telemetry::genai::*;
    assert_eq!(TOKEN_USAGE, "gen_ai.client.token.usage");
    assert_eq!(OPERATION_DURATION, "gen_ai.client.operation.duration");
    for a in [
        ATTR_OPERATION_NAME,
        ATTR_PROVIDER_NAME,
        ATTR_REQUEST_MODEL,
        ATTR_TOKEN_TYPE,
    ] {
        assert!(a.starts_with("gen_ai."), "{a}");
    }
}
