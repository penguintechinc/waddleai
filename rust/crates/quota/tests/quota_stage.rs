//! `QuotaStage` logic against a fake backend: no Valkey needed. A separate binary with ONE
//! process-global tracing subscriber so log assertions are reliable.

use std::collections::BTreeSet;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use auth::{AuthMode as UserAuthMode, UserContext};
use pipeline::{AuthMode, FlagResolver, PipelineContext, Stage, StaticFlagResolver};
use quota::{
    CreditError, CreditModel, Outcome, QuotaBackend, QuotaError, QuotaStage, ReconcileResult,
    Reservation, POOLED_QUOTA_FLAG,
};
use serde_json::{json, Value};
use tracing::field::{Field, Visit};
use tracing_subscriber::layer::{Context, SubscriberExt};
use tracing_subscriber::Layer;

#[derive(Default)]
struct Calls {
    reserve: AtomicUsize,
    release: AtomicUsize,
    reconcile: Mutex<Vec<(i64, String, String)>>,
}

struct Fake {
    result: Mutex<Option<Result<Outcome, QuotaError>>>,
    calls: Arc<Calls>,
}

struct NoCredits;
#[async_trait::async_trait]
impl CreditModel for NoCredits {
    fn estimate_credits(&self, _m: &str, _i: u64, _o: u64) -> u64 {
        1
    }
    fn providers_for_model(&self, _m: &str) -> std::collections::HashSet<String> {
        Default::default()
    }
    fn count_tokens(&self, _t: &str, _p: &str, _m: &str) -> Result<u64, CreditError> {
        Ok(10)
    }
    fn calculate_waddleai_tokens(
        &self,
        i: u64,
        o: u64,
        _p: &str,
        _m: &str,
    ) -> Result<u64, CreditError> {
        Ok(i + o)
    }
}

#[async_trait::async_trait]
impl QuotaBackend for Fake {
    async fn reserve(
        &self,
        _u: &UserContext,
        _m: Option<&str>,
        _msgs: &[Value],
    ) -> Result<Outcome, QuotaError> {
        self.calls.reserve.fetch_add(1, Ordering::SeqCst);
        self.result
            .lock()
            .unwrap()
            .take()
            .unwrap_or(Ok(Outcome::proceed()))
    }
    async fn reconcile(
        &self,
        _r: &Reservation,
        actual: i64,
        p: &str,
        m: &str,
        _c: bool,
    ) -> ReconcileResult {
        self.calls
            .reconcile
            .lock()
            .unwrap()
            .push((actual, p.into(), m.into()));
        ReconcileResult::default()
    }
    async fn release(&self, _r: &Reservation) {
        self.calls.release.fetch_add(1, Ordering::SeqCst);
    }
    fn credits(&self) -> &dyn CreditModel {
        &NoCredits
    }
    fn fault(&self, _k: &str) {}
}

fn fake(result: Result<Outcome, QuotaError>) -> (Arc<dyn QuotaBackend>, Arc<Calls>) {
    let calls = Arc::new(Calls::default());
    (
        Arc::new(Fake {
            result: Mutex::new(Some(result)),
            calls: calls.clone(),
        }),
        calls,
    )
}

fn user(org: i32) -> UserContext {
    UserContext {
        org_id: org,
        user_id: 7,
        tenant: org.to_string(),
        scopes: BTreeSet::new(),
        teams: vec![],
        auth_mode: UserAuthMode::Jwt,
        api_key_id: None,
        team_id: Some(3),
        team_role: None,
        role: None,
    }
}

fn ctx(org: Option<i32>) -> PipelineContext {
    let mut c = PipelineContext::new(json!({}));
    c.user = org.map(user);
    c.model = Some("llama3.2".into());
    c.messages = vec![json!({"content": "hi"})];
    c
}

fn on() -> Arc<dyn FlagResolver> {
    Arc::new(StaticFlagResolver::new([POOLED_QUOTA_FLAG]))
}

fn reservation() -> Reservation {
    Reservation {
        reservation_id: "rid1".into(),
        org_id: 5,
        team_id: 3,
        user_id: 7,
        cycle: 0,
        credits_reserved: 6,
        from_reservation: 6,
        from_buffer: 0,
        model_requested: Some("llama3.2".into()),
    }
}

#[test]
fn stage_name_is_quota() {
    assert_eq!(QuotaStage::new(None, on()).name(), "quota");
}

#[tokio::test]
async fn skips_when_engine_none_or_org_none_or_cache_hit() {
    // no engine
    let mut c = ctx(Some(5));
    QuotaStage::new(None, on()).call(&mut c).await.unwrap();
    assert!(c.quota_reservation.is_none() && !c.blocked);
    // no user / org
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(None);
    QuotaStage::new(Some(b.clone()), on())
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(calls.reserve.load(Ordering::SeqCst), 0);
    // cache hit is free
    let mut c = ctx(Some(5));
    c.cache_hit = true;
    QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
    assert_eq!(calls.reserve.load(Ordering::SeqCst), 0);
    assert!(c.quota_reservation.is_none() && c.quota_release.is_none());
}

/// Records the (flag, distinct_id) it is asked about and answers a fixed value.
struct Recording(Mutex<Vec<(String, Option<String>)>>, bool);

#[async_trait::async_trait]
impl FlagResolver for Recording {
    async fn enabled(&self, flag: &str, distinct_id: Option<&str>) -> bool {
        self.0
            .lock()
            .unwrap()
            .push((flag.into(), distinct_id.map(Into::into)));
        self.1
    }
}

#[tokio::test]
async fn flag_resolved_with_org_id_distinct_id_default_false() {
    let rec = Arc::new(Recording(Mutex::new(vec![]), false));
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(Some(42));
    QuotaStage::new(Some(b), rec.clone())
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(
        rec.0.lock().unwrap().clone(),
        vec![("waddleai.pooled_quota".to_owned(), Some("42".to_owned()))]
    );
    assert_eq!(
        calls.reserve.load(Ordering::SeqCst),
        0,
        "flag OFF never reserves"
    );
    assert!(c.quota_release.is_none());
    // an unseeded resolver (all OFF) is the same default-false path
    let (b, calls) = fake(Ok(Outcome::proceed()));
    let mut c = ctx(Some(42));
    QuotaStage::new(Some(b), Arc::new(StaticFlagResolver::default()))
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(calls.reserve.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn org_zero_is_not_skipped_gh_347_followup() {
    // regression: gh-347 -- only an ABSENT org skips the stage (Python `org_id is None`); the
    // fall-through id 0 is a real org that is flagged and reserved like any other.
    let (b, calls) = fake(Ok(Outcome::proceed()));
    let rec = Arc::new(Recording(Mutex::new(vec![]), true));
    let mut c = ctx(Some(0));
    QuotaStage::new(Some(b), rec.clone())
        .call(&mut c)
        .await
        .unwrap();
    assert_eq!(rec.0.lock().unwrap()[0].1.as_deref(), Some("0"));
    assert_eq!(calls.reserve.load(Ordering::SeqCst), 1);
}

type Captured = Arc<Mutex<Vec<(tracing::Level, String)>>>;
static LOGS: OnceLock<Captured> = OnceLock::new();

struct Msg(String);
impl Visit for Msg {
    fn record_debug(&mut self, f: &Field, v: &dyn std::fmt::Debug) {
        if f.name() == "message" {
            self.0 = format!("{v:?}");
        }
    }
}

struct Cap(Captured);
impl<S: tracing::Subscriber> Layer<S> for Cap {
    fn on_event(&self, e: &tracing::Event<'_>, _c: Context<'_, S>) {
        let mut m = Msg(String::new());
        e.record(&mut m);
        self.0.lock().unwrap().push((*e.metadata().level(), m.0));
    }
}

fn logs() -> Captured {
    LOGS.get_or_init(|| {
        let cap: Captured = Arc::default();
        let sub = tracing_subscriber::registry().with(Cap(cap.clone()));
        tracing::subscriber::set_global_default(sub).expect("single global subscriber");
        cap
    })
    .clone()
}

#[tokio::test]
async fn fail_open_on_engine_error_logged() {
    let cap = logs();
    let (b, _) = fake(Err(QuotaError::Backend("valkey exploded".into())));
    let mut c = ctx(Some(5));
    QuotaStage::new(Some(b), on())
        .call(&mut c)
        .await
        .expect("fail open: never a stage fault");
    assert!(!c.blocked && c.status_code == 200);
    assert!(c.quota_reservation.is_none() && c.quota_release.is_none());
    let seen = cap.lock().unwrap().clone();
    assert!(
        seen.iter()
            .any(|(l, m)| *l == tracing::Level::ERROR && m.contains("quota stage failed open")),
        "the bypass must be logged at ERROR, got {seen:?}"
    );
}

#[tokio::test]
async fn deny_sets_429_with_block_message() {
    for (reason, msg) in [
        (
            Some("quota_exhausted"),
            "Token quota exhausted for the current billing cycle",
        ),
        (Some("daily_subcap_exceeded"), "Daily token limit reached"),
        (
            Some("override_exceeded"),
            "Token limit reached for this user, model or provider",
        ),
        (Some("something_new"), "Token quota exceeded"),
        (None, "Token quota exceeded"),
    ] {
        let (b, _) = fake(Ok(Outcome {
            blocked: true,
            reason: reason.map(Into::into),
            reservation: None,
        }));
        let mut c = ctx(Some(5));
        QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
        assert!(c.blocked);
        assert_eq!(c.status_code, 429);
        assert_eq!(c.block_reason.as_deref(), Some(msg));
        assert!(c.quota_release.is_none());
    }
}

#[tokio::test]
async fn admit_stores_reservation_and_guard() {
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(Some(5));
    QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
    assert!(!c.blocked);
    let held = c.quota_reservation.clone().expect("reservation stored");
    assert_eq!((held.id.as_str(), held.tokens), ("rid1", 6));
    assert_eq!(c.extensions.get::<Reservation>().map(|r| r.cycle), Some(0));
    // The stored guard settles by value: a zero-usage abort refunds (release), once.
    let guard = c.quota_release.take().expect("guard stored");
    guard
        .settle_aborted(pipeline::AbortSnapshot::from_ctx(&c))
        .await
        .unwrap();
    assert_eq!(calls.release.load(Ordering::SeqCst), 1);
    assert!(calls.reconcile.lock().unwrap().is_empty());
}

#[tokio::test]
async fn guard_charges_consumed_with_reported_usage_and_falls_back_to_requested_model() {
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(Some(5));
    QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
    c.provider = Some("ollama".into());
    c.model = None; // falls back to the reservation's requested model
    c.response_text = "partial".into();
    c.usage = Some(pipeline::Usage {
        input_tokens: 4,
        output_tokens: 5,
        ..Default::default()
    });
    let guard = c.quota_release.take().unwrap();
    guard
        .settle_aborted(pipeline::AbortSnapshot::from_ctx(&c))
        .await
        .unwrap();
    assert_eq!(
        calls.release.load(Ordering::SeqCst),
        0,
        "consumed spend is never refunded"
    );
    assert_eq!(
        calls.reconcile.lock().unwrap().clone(),
        vec![(9, "ollama".into(), "llama3.2".into())]
    );

    // no usage: counted locally from messages / response text (fake counts 10 each)
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(Some(5));
    QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
    c.provider = Some("ollama".into());
    c.response_text = "partial".into();
    let guard = c.quota_release.take().unwrap();
    guard
        .settle_aborted(pipeline::AbortSnapshot::from_ctx(&c))
        .await
        .unwrap();
    assert_eq!(calls.reconcile.lock().unwrap()[0].0, 20);
}

#[tokio::test]
async fn passthrough_mode_rate_limit_only_never_debits() {
    // Passthrough charges NO WaddleAI credits (the user pays the provider): even with the
    // flag ON and an admitting backend, no reserve happens and nothing is held.
    let (b, calls) = fake(Ok(Outcome {
        reservation: Some(reservation()),
        ..Default::default()
    }));
    let mut c = ctx(Some(5));
    c.auth_mode = AuthMode::Passthrough;
    QuotaStage::new(Some(b), on()).call(&mut c).await.unwrap();
    assert_eq!(calls.reserve.load(Ordering::SeqCst), 0, "never debits");
    assert!(c.quota_reservation.is_none() && c.quota_release.is_none());
    assert!(!c.blocked && c.status_code == 200);
}
