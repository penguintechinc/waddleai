//! Own test binary so the tracing capture is the only subscriber (callsite interest is
//! process-global and cached by whichever test ran first).

mod common;

use std::sync::{Arc, Mutex};

use common::{ctx, simple};
use connectors::DispatchStage;
use pipeline::Stage;
use serde_json::json;
use tracing_subscriber::layer::SubscriberExt;
use wiremock::matchers::{body_partial_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

/// Collects every tracing event's fields so a test can assert nothing leaked into logs.
#[derive(Clone, Default)]
struct Capture(Arc<Mutex<String>>);

struct Visit<'a>(&'a mut String);

impl tracing::field::Visit for Visit<'_> {
    fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={v:?} ", f.name()));
    }
}

impl<S: tracing::Subscriber> tracing_subscriber::Layer<S> for Capture {
    fn on_event(&self, e: &tracing::Event<'_>, _: tracing_subscriber::layer::Context<'_, S>) {
        let mut s = String::new();
        e.record(&mut Visit(&mut s));
        self.0.lock().unwrap().push_str(&s);
    }
}

#[tokio::test]
async fn dispatch_error_sets_status_and_records_without_leaking_upstream_body_secrets() {
    let cap = Capture::default();
    let _g = tracing::subscriber::set_default(tracing_subscriber::registry().with(cap.clone()));
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/chat"))
        .and(body_partial_json(json!({"model": "m"})))
        .respond_with(ResponseTemplate::new(500).set_body_string("SECRET-UPSTREAM-TOKEN-xyz"))
        .mount(&srv)
        .await;
    let stage = DispatchStage::new(Arc::new(simple(&srv.uri())));
    let mut c = ctx("m");
    stage.call(&mut c).await.unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 502);
    assert_eq!(c.block_reason.as_deref(), Some("provider_error_500"));
    let logged = cap.0.lock().unwrap().clone();
    assert!(
        logged.contains("dispatch: upstream error"),
        "expected a warn event, got {logged:?}"
    );
    let everything = format!("{logged}{:?}{:?}{}", c.block_reason, c, c.response_text);
    assert!(
        !everything.contains("SECRET-UPSTREAM"),
        "upstream body leaked: {everything}"
    );
}
