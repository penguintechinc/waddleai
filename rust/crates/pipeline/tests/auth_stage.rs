//! AuthStage tests: auth modes, passthrough opt-in gate, tenant check, credential hygiene.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use auth::{AuthError, AuthMode as UserAuthMode, UserContext};
use http::{HeaderMap, HeaderValue};
use pipeline::stages::{
    detect_auth_mode, AuthStage, Authenticator, CredentialKind, DenyPassthrough, KeyLimitsSource,
    NoKeyLimits, PassthroughPolicy, RequestHeaders,
};
use pipeline::{AuthMode, KeyLimits, PipelineContext, Stage, StageError, VkeyBudget};
use secrecy::ExposeSecret;

const WA: &str = "wa-key123-supersecretvalue";
const PROVIDER: &str = "sk-provider-TOPSECRET-9999";

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

struct Limits;
#[async_trait]
impl KeyLimitsSource for Limits {
    async fn limits_for(&self, _k: &UserContext) -> Option<(i64, Option<KeyLimits>)> {
        Some((
            11,
            Some(KeyLimits {
                tpm_limit: Some(100),
                ..Default::default()
            }),
        ))
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
async fn none_user_blocks_401_unauthenticated() {
    let mut c = PipelineContext::new(serde_json::json!({}));
    stage(Ok(user("7")), Arc::new(DenyPassthrough))
        .call(&mut c)
        .await
        .unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 401);
    assert_eq!(c.block_reason.as_deref(), Some("unauthenticated"));
}

#[tokio::test]
async fn user_without_tenant_blocks_403_missing_organization() {
    let mut c = ctx(Some(WA), None);
    stage(Ok(user("")), Arc::new(DenyPassthrough))
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(c.status_code, 403);
    assert_eq!(c.block_reason.as_deref(), Some("missing_organization"));
}

#[tokio::test]
async fn wa_key_alone_sets_virtual() {
    let mut c = ctx(Some(WA), None);
    stage(Ok(user("7")), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    assert!(!c.blocked);
    assert_eq!(c.auth_mode, AuthMode::Virtual);
    assert!(c.passthrough_credential.is_none());
    assert!(c.user.is_some());
}

#[tokio::test]
async fn wa_key_plus_provider_key_sets_passthrough_when_enabled() {
    let mut c = ctx(Some(WA), Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    assert!(!c.blocked);
    assert_eq!(c.auth_mode, AuthMode::Passthrough);
    assert_eq!(
        c.passthrough_credential.as_ref().unwrap().expose_secret(),
        PROVIDER
    );
    // deferred to T10: dispatch rejection of passthrough (DispatchStage does not exist yet)
}

#[tokio::test]
async fn provider_key_when_not_enabled_rejected_401() {
    let mut c = ctx(Some(WA), Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(DenyPassthrough))
        .call(&mut c)
        .await
        .unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 401);
    assert!(c.passthrough_credential.is_none());
    assert_eq!(c.auth_mode, AuthMode::Virtual);
}

#[tokio::test]
async fn provider_key_without_wa_key_rejected() {
    let mut c = ctx(None, Some(PROVIDER));
    stage(Ok(user("7")), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 401);
    assert!(c.passthrough_credential.is_none());
}

#[tokio::test]
async fn malformed_wa_key_rejects_no_panic() {
    let mut c = ctx(Some("wa-"), None);
    stage(Err(AuthError::InvalidApiKey), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 401);
}

#[tokio::test]
async fn backend_fault_fails_closed_as_stage_error_and_overload_is_503() {
    let mut c = ctx(Some(WA), None);
    let e = stage(Err(AuthError::Db), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap_err();
    assert!(matches!(e, StageError::Upstream(_)));
    let mut c = ctx(Some(WA), None);
    stage(Err(AuthError::Overloaded), Arc::new(Allow))
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(c.status_code, 503);
}

#[tokio::test]
async fn limits_populate_vkey_budget_extension() {
    let mut c = ctx(Some(WA), None);
    AuthStage::new(
        Arc::new(FakeAuth(Ok(user("7")))),
        Arc::new(DenyPassthrough),
        Arc::new(Limits),
    )
    .call(&mut c)
    .await
    .unwrap();
    let b = c.extensions.get::<VkeyBudget>().unwrap();
    assert_eq!(b.vkey_id, 11);
    assert_eq!(b.limits.as_ref().unwrap().tpm_limit, Some(100));
    // no limits source -> nothing inserted
    let mut c = ctx(Some(WA), None);
    stage(Ok(user("7")), Arc::new(DenyPassthrough))
        .call(&mut c)
        .await
        .unwrap();
    assert!(c.extensions.get::<VkeyBudget>().is_none());
}

#[tokio::test]
async fn preauthenticated_user_without_headers_is_virtual() {
    let mut c = PipelineContext::new(serde_json::json!({}));
    c.user = Some(user("7"));
    stage(Err(AuthError::Db), Arc::new(DenyPassthrough))
        .call(&mut c)
        .await
        .unwrap();
    assert!(!c.blocked);
    assert_eq!(c.auth_mode, AuthMode::Virtual);
}

#[test]
fn detect_covers_all_kinds_and_x_api_key() {
    let mut h = HeaderMap::new();
    assert!(matches!(detect_auth_mode(&h), CredentialKind::None));
    h.insert("x-api-key", HeaderValue::from_static("wa-a-b"));
    assert!(matches!(detect_auth_mode(&h), CredentialKind::WaddleKey(_)));
    // a non-wa bearer is never treated as a provider key
    let mut h = HeaderMap::new();
    h.insert("authorization", HeaderValue::from_static("Bearer sk-xyz"));
    assert!(matches!(detect_auth_mode(&h), CredentialKind::None));
    h.insert("x-provider-key", HeaderValue::from_static("sk-xyz"));
    assert!(matches!(
        detect_auth_mode(&h),
        CredentialKind::ProviderKey(_)
    ));
    h.insert("x-api-key", HeaderValue::from_static("wa-a-b"));
    let k = detect_auth_mode(&h);
    assert!(matches!(k, CredentialKind::Passthrough { .. }));
    assert!(!format!("{k:?}").contains("sk-xyz"));
}

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

#[tokio::test]
async fn passthrough_credential_never_in_debug_or_logs() {
    use tracing_subscriber::layer::SubscriberExt;
    let sink = Sink::default();
    let sub = tracing_subscriber::registry().with(sink.clone());
    let _g = tracing::subscriber::set_default(sub);

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
