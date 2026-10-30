//! Passthrough-credential redaction test, isolated in its own test binary.
//!
//! It owns the only tracing subscriber in this process (installed globally
//! once), so no other test can poison tracing's callsite-interest cache and
//! make log capture nondeterministic. Mirrors the isolation in `tests/spans.rs`.

use std::sync::{Arc, Mutex, OnceLock};

use async_trait::async_trait;
use auth::{AuthError, AuthMode as UserAuthMode, UserContext};
use http::{HeaderMap, HeaderValue};
use pipeline::stages::{
    AuthStage, Authenticator, DenyPassthrough, NoKeyLimits, PassthroughPolicy, RequestHeaders,
};
use pipeline::{PipelineContext, Stage};
use tracing_subscriber::layer::SubscriberExt;

const WA: &str = "wa-key123-supersecretvalue";
const PROVIDER: &str = "sk-provider-TOPSECRET-9999";

/// Tracing sink capturing every formatted event field.
#[derive(Clone, Default)]
struct Sink(Arc<Mutex<String>>);

impl<S: tracing::Subscriber> tracing_subscriber::Layer<S> for Sink {
    fn on_event(&self, e: &tracing::Event<'_>, _: tracing_subscriber::layer::Context<'_, S>) {
        struct V<'a>(&'a mut String);
        impl tracing::field::Visit for V<'_> {
            fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
                self.0.push_str(&format!("{}={v:?} ", f.name()));
            }
        }
        let mut g = self.0.lock().unwrap();
        e.record(&mut V(&mut g));
    }
}

/// Installs the process-wide sink exactly once and returns it.
fn sink() -> &'static Sink {
    static SINK: OnceLock<Sink> = OnceLock::new();
    SINK.get_or_init(|| {
        let s = Sink::default();
        tracing::subscriber::set_global_default(tracing_subscriber::registry().with(s.clone()))
            .expect("sole subscriber in this test binary");
        s
    })
}

fn user(tenant: &str) -> UserContext {
    UserContext {
        org_id: 7,
        user_id: 3,
        tenant: tenant.into(),
        scopes: Default::default(),
        teams: vec![],
        auth_mode: UserAuthMode::ApiKey,
        api_key_id: Some(11),
        team_id: None,
        team_role: None,
        role: None,
    }
}

struct FakeAuth(Result<UserContext, AuthError>);

#[async_trait]
impl Authenticator for FakeAuth {
    async fn authenticate(&self, k: &str) -> Result<UserContext, AuthError> {
        if k.starts_with("wa-") {
            self.0.clone()
        } else {
            Err(AuthError::InvalidApiKey)
        }
    }
}

struct Allow;
#[async_trait]
impl PassthroughPolicy for Allow {
    async fn enabled(&self, _k: &UserContext) -> bool {
        true
    }
}

fn stage(a: Result<UserContext, AuthError>, p: Arc<dyn PassthroughPolicy>) -> AuthStage {
    AuthStage::new(Arc::new(FakeAuth(a)), p, Arc::new(NoKeyLimits))
}

fn ctx(wa: Option<&str>, provider: Option<&str>) -> PipelineContext {
    let mut h = HeaderMap::new();
    if let Some(w) = wa {
        h.insert(
            "authorization",
            HeaderValue::from_str(&format!("Bearer {w}")).unwrap(),
        );
    }
    if let Some(p) = provider {
        h.insert("x-provider-key", HeaderValue::from_str(p).unwrap());
    }
    let mut c = PipelineContext::new(serde_json::json!({}));
    c.extensions.insert(RequestHeaders(h));
    c
}

#[tokio::test]
async fn passthrough_credential_never_in_debug_or_logs() {
    let sink = sink();

    // enabled path, the denied path and the rejected path all log
    let mut c = ctx(Some(WA), Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    let dbg = format!("{c:?}");
    let mut d = ctx(Some(WA), Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(DenyPassthrough))
        .call(&mut d)
        .await
        .unwrap();
    let mut e = ctx(None, Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(Allow))
        .call(&mut e)
        .await
        .unwrap();

    let logs = sink.0.lock().unwrap().clone();
    assert!(!logs.is_empty(), "denominator: no events captured");
    for hay in [&dbg, &logs, &format!("{d:?}"), &format!("{e:?}")] {
        assert!(!hay.contains("TOPSECRET"), "{hay}");
        assert!(!hay.contains("supersecretvalue"), "{hay}");
    }
    assert!(dbg.contains("<redacted>"));
}
