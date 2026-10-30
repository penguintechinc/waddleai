//! Span-attribute tests. A separate binary with ONE process-global subscriber so the
#![allow(clippy::field_reassign_with_default)]
//! tracing callsite-interest cache is never poisoned by subscriber-less tests.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};

use pipeline::{PipelineContext, ProxyPipeline, Stage, StageError, StaticFlagResolver, Usage};
use tracing::field::{Field, Visit};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::Layer;

/// Stage that optionally fails.
struct Probe {
    name: &'static str,
    fail: bool,
}

#[async_trait::async_trait]
impl Stage for Probe {
    fn name(&self) -> &'static str {
        self.name
    }
    async fn call(&self, _ctx: &mut PipelineContext) -> Result<(), StageError> {
        if self.fail {
            Err(StageError::Internal("boom".into()))
        } else {
            Ok(())
        }
    }
}

fn pipe(name: &'static str, fail: bool) -> ProxyPipeline {
    let stages: Vec<Arc<dyn Stage>> = vec![Arc::new(Probe { name, fail })];
    ProxyPipeline::new(stages, Arc::new(StaticFlagResolver::default()))
}

static CAPTURE: OnceLock<Capture> = OnceLock::new();
static SERIAL: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// Installs the global capture subscriber once and clears prior fields.
fn capture() -> Capture {
    let c = CAPTURE.get_or_init(|| {
        let c = Capture::default();
        let sub = tracing_subscriber::registry().with(c.clone());
        tracing::subscriber::set_global_default(sub).unwrap();
        c
    });
    c.0.lock().unwrap().clear();
    c.clone()
}

/// Records span fields into a shared map.
#[derive(Clone, Default)]
struct Capture(Arc<Mutex<BTreeMap<String, String>>>);

struct V<'a>(&'a Mutex<BTreeMap<String, String>>);
impl Visit for V<'_> {
    fn record_debug(&mut self, f: &Field, v: &dyn std::fmt::Debug) {
        self.0
            .lock()
            .unwrap()
            .insert(f.name().into(), format!("{v:?}"));
    }
    fn record_str(&mut self, f: &Field, v: &str) {
        self.0.lock().unwrap().insert(f.name().into(), v.into());
    }
    fn record_u64(&mut self, f: &Field, v: u64) {
        self.0
            .lock()
            .unwrap()
            .insert(f.name().into(), v.to_string());
    }
}

impl<S: tracing::Subscriber + for<'a> tracing_subscriber::registry::LookupSpan<'a>> Layer<S>
    for Capture
{
    fn on_new_span(
        &self,
        attrs: &tracing::span::Attributes<'_>,
        _: &tracing::Id,
        _: Context<'_, S>,
    ) {
        attrs.record(&mut V(&self.0));
    }
    fn on_record(&self, _: &tracing::Id, values: &tracing::span::Record<'_>, _: Context<'_, S>) {
        values.record(&mut V(&self.0));
    }
}

#[tokio::test]
async fn dispatch_span_sets_genai_attrs() {
    let _s = SERIAL.lock().await;
    let cap = capture();

    let p = pipe("dispatch", false);
    let mut ctx = PipelineContext::default();
    ctx.prompt_text = "SECRET PROMPT".into();
    ctx.response_text = "SECRET REPLY".into();
    ctx.provider = Some("ollama".into());
    ctx.requested_model = Some("gemma:req".into());
    ctx.model = Some("gemma:resp".into());
    ctx.usage = Some(Usage {
        input_tokens: 7,
        output_tokens: 11,
        ..Default::default()
    });
    ctx.finish_reason = Some("stop".into());
    p.run(&mut ctx).await.unwrap();

    let m = cap.0.lock().unwrap();
    let get = |k: &str| m.get(k).cloned();
    assert_eq!(get("gen_ai.system").as_deref(), Some("ollama"));
    assert_eq!(get("gen_ai.request.model").as_deref(), Some("gemma:req"));
    assert_eq!(get("gen_ai.response.model").as_deref(), Some("gemma:resp"));
    assert_eq!(get("gen_ai.usage.input_tokens").as_deref(), Some("7"));
    assert_eq!(get("gen_ai.usage.output_tokens").as_deref(), Some("11"));
    assert_eq!(
        get("gen_ai.response.finish_reason").as_deref(),
        Some("stop")
    );
    assert!(
        m.values().all(|v| !v.contains("SECRET")),
        "no content in span attrs"
    );
}

#[tokio::test]
async fn non_dispatch_span_has_no_genai_attrs_and_error_is_recorded() {
    let _s = SERIAL.lock().await;
    let cap = capture();
    let p = pipe("auth", true);
    let mut ctx = PipelineContext::default();
    ctx.provider = Some("ollama".into());
    let _ = p.run(&mut ctx).await;
    let m = cap.0.lock().unwrap();
    assert!(!m.contains_key("gen_ai.system"));
    assert_eq!(
        m.get("error.message").map(String::as_str),
        Some("internal: boom")
    );
}
