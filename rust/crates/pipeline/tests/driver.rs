//! Driver tests: ordering, flag/blocked precedence, quota release, cancellation, spans.
#![allow(clippy::field_reassign_with_default)]

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use pipeline::{
    Depseudonymizer, Extensions, FlagResolver, IdentityDepseudonymizer, NerClient, NerError,
    NerRequest, NoNer, PipelineContext, PosthogFlagResolver, ProxyPipeline, PseudonymMap,
    QuotaGuard, QuotaReservation, Stage, StageError, StaticFlagResolver,
};

/// Test stage with scripted behavior and call counting.
struct Probe {
    name: &'static str,
    flag: Option<&'static str>,
    block: bool,
    fail: bool,
    calls: Arc<AtomicUsize>,
}

impl Probe {
    fn new(name: &'static str) -> Self {
        Self {
            name,
            flag: None,
            block: false,
            fail: false,
            calls: Arc::new(AtomicUsize::new(0)),
        }
    }
    fn flag(mut self, f: &'static str) -> Self {
        self.flag = Some(f);
        self
    }
    fn block(mut self) -> Self {
        self.block = true;
        self
    }
    fn fail(mut self) -> Self {
        self.fail = true;
        self
    }
}

#[async_trait::async_trait]
impl Stage for Probe {
    fn name(&self) -> &'static str {
        self.name
    }
    fn flag(&self) -> Option<&'static str> {
        self.flag
    }
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.fail {
            return Err(StageError::Internal("boom".into()));
        }
        if self.block {
            ctx.blocked = true;
            ctx.status_code = 429;
        }
        Ok(())
    }
}

/// Guard counting releases; optionally slow or failing.
struct Guard {
    releases: Arc<AtomicUsize>,
    delay: Duration,
    fail: bool,
}

#[async_trait::async_trait]
impl QuotaGuard for Guard {
    async fn release(self: Box<Self>) -> Result<(), StageError> {
        tokio::time::sleep(self.delay).await;
        self.releases.fetch_add(1, Ordering::SeqCst);
        if self.fail {
            Err(StageError::Upstream("refund failed".into()))
        } else {
            Ok(())
        }
    }
}

fn guard(releases: &Arc<AtomicUsize>) -> Option<Box<dyn QuotaGuard>> {
    Some(Box::new(Guard {
        releases: releases.clone(),
        delay: Duration::ZERO,
        fail: false,
    }))
}

fn pipe(stages: Vec<Probe>, on: &[&str]) -> ProxyPipeline {
    let stages: Vec<Arc<dyn Stage>> = stages.into_iter().map(|p| Arc::new(p) as _).collect();
    ProxyPipeline::new(
        stages,
        Arc::new(StaticFlagResolver::new(on.iter().copied())),
    )
}

#[tokio::test]
async fn runs_stages_in_order_and_logs_ran() {
    let p = pipe(
        vec![
            Probe::new("auth"),
            Probe::new("cache"),
            Probe::new("dispatch"),
        ],
        &[],
    );
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(ctx.stage_log, ["ran:auth", "ran:cache", "ran:dispatch"]);
    assert_eq!(ctx.status_code, 200);
}

#[tokio::test]
async fn flag_off_skips_with_skipped_log() {
    let gated = Probe::new("cache").flag("waddleai.cache");
    let calls = gated.calls.clone();
    let p = pipe(vec![Probe::new("auth"), gated], &[]);
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(ctx.stage_log, ["ran:auth", "skipped:cache"]);
    assert_eq!(calls.load(Ordering::SeqCst), 0);

    let on = pipe(
        vec![Probe::new("cache").flag("waddleai.cache")],
        &["waddleai.cache"],
    );
    let mut ctx = PipelineContext::default();
    on.run(&mut ctx).await.unwrap();
    assert_eq!(ctx.stage_log, ["ran:cache"]);
}

#[tokio::test]
async fn flag_skip_is_evaluated_before_blocked_short_circuit() {
    let p = pipe(
        vec![
            Probe::new("auth").block(),
            Probe::new("cache").flag("f.off"),
            Probe::new("meter"),
        ],
        &[],
    );
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(
        ctx.stage_log,
        ["ran:auth", "skipped:cache", "short-circuit:meter"]
    );
}

#[tokio::test]
async fn blocked_ctx_short_circuits_remaining_stages() {
    let call = Probe::new("dispatch");
    let calls = call.calls.clone();
    let p = pipe(
        vec![Probe::new("auth").block(), call, Probe::new("meter")],
        &[],
    );
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(
        ctx.stage_log,
        ["ran:auth", "short-circuit:dispatch", "short-circuit:meter"]
    );
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(ctx.status_code, 429);
}

#[tokio::test]
async fn stage_error_releases_quota_once_then_propagates() {
    let rel = Arc::new(AtomicUsize::new(0));
    let p = pipe(
        vec![
            Probe::new("auth"),
            Probe::new("dispatch").fail(),
            Probe::new("meter"),
        ],
        &[],
    );
    let mut ctx = PipelineContext::default();
    ctx.quota_release = guard(&rel);
    ctx.quota_reservation = Some(QuotaReservation {
        id: "r1".into(),
        tokens: 10,
    });
    let err = p.run(&mut ctx).await.unwrap_err();
    assert_eq!(err, StageError::Internal("boom".into()));
    assert_eq!(rel.load(Ordering::SeqCst), 1);
    assert!(ctx.quota_release.is_none() && ctx.quota_reservation.is_none());
    assert_eq!(ctx.stage_log, ["ran:auth"]);
}

#[tokio::test]
async fn blocked_after_stage_releases_quota_once() {
    let rel = Arc::new(AtomicUsize::new(0));
    let p = pipe(
        vec![
            Probe::new("quota").block(),
            Probe::new("dispatch"),
            Probe::new("meter"),
        ],
        &[],
    );
    let mut ctx = PipelineContext::default();
    ctx.quota_release = guard(&rel);
    p.run(&mut ctx).await.unwrap();
    // Released after the blocking stage; later short-circuits find the guard already taken.
    assert_eq!(rel.load(Ordering::SeqCst), 1);
    assert!(ctx.quota_release.is_none());
}

#[tokio::test]
async fn release_failure_logged_never_raised() {
    let rel = Arc::new(AtomicUsize::new(0));
    let p = pipe(vec![Probe::new("quota").block()], &[]);
    let mut ctx = PipelineContext::default();
    ctx.quota_release = Some(Box::new(Guard {
        releases: rel.clone(),
        delay: Duration::ZERO,
        fail: true,
    }));
    p.run(&mut ctx).await.unwrap();
    assert_eq!(rel.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn release_survives_task_cancellation() {
    let rel = Arc::new(AtomicUsize::new(0));
    let p = Arc::new(pipe(vec![Probe::new("quota").block()], &[]));
    let rel2 = rel.clone();
    let outer = tokio::spawn(async move {
        let mut ctx = PipelineContext::default();
        ctx.quota_release = Some(Box::new(Guard {
            releases: rel2,
            delay: Duration::from_millis(150),
            fail: false,
        }));
        let _ = p.run(&mut ctx).await;
    });
    tokio::time::sleep(Duration::from_millis(30)).await;
    outer.abort();
    assert!(outer.await.unwrap_err().is_cancelled());
    assert_eq!(
        rel.load(Ordering::SeqCst),
        0,
        "settle still in flight at abort"
    );
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert_eq!(
        rel.load(Ordering::SeqCst),
        1,
        "settle completed after outer abort"
    );
}

#[tokio::test]
async fn run_until_exclusive_and_run_after_exclusive() {
    let mk = || {
        pipe(
            vec![
                Probe::new("auth"),
                Probe::new("cache"),
                Probe::new("dispatch"),
                Probe::new("meter"),
            ],
            &[],
        )
    };
    let full = {
        let mut c = PipelineContext::default();
        mk().run(&mut c).await.unwrap();
        c.stage_log
    };
    let p = mk();
    let mut c = PipelineContext::default();
    p.run_until(&mut c, "dispatch").await.unwrap();
    assert_eq!(c.stage_log, ["ran:auth", "ran:cache"]);
    p.run_after(&mut c, "dispatch").await.unwrap();
    assert_eq!(c.stage_log, ["ran:auth", "ran:cache", "ran:meter"]);
    // head + dispatch + tail == run
    let mut joined = c.stage_log.clone();
    joined.insert(2, "ran:dispatch".into());
    assert_eq!(joined, full);
}

#[tokio::test]
async fn unknown_stage_name_fails_loudly() {
    let p = pipe(vec![Probe::new("auth")], &[]);
    let mut c = PipelineContext::default();
    assert!(matches!(
        p.run_until(&mut c, "nope").await,
        Err(StageError::Internal(_))
    ));
    assert!(matches!(
        p.run_after(&mut c, "nope").await,
        Err(StageError::Internal(_))
    ));
    assert!(c.stage_log.is_empty());
}

#[tokio::test]
async fn run_after_on_blocked_ctx_releases_quota_first() {
    let rel = Arc::new(AtomicUsize::new(0));
    let p = pipe(
        vec![
            Probe::new("dispatch"),
            Probe::new("security_out"),
            Probe::new("meter"),
        ],
        &[],
    );
    let mut ctx = PipelineContext::default();
    ctx.blocked = true;
    ctx.quota_release = guard(&rel);
    p.run_after(&mut ctx, "dispatch").await.unwrap();
    assert_eq!(rel.load(Ordering::SeqCst), 1);
    assert_eq!(
        ctx.stage_log,
        ["short-circuit:security_out", "short-circuit:meter"]
    );
}

#[tokio::test]
async fn meter_after_block_short_circuits_like_python() {
    // Python meter docstring claims it runs when blocked, but _run_stage short-circuits it.
    // Characterization: the ported driver does the same -- meter `call` is NOT invoked.
    let meter = Probe::new("meter");
    let calls = meter.calls.clone();
    let p = pipe(vec![Probe::new("dispatch").block(), meter], &[]);
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(ctx.stage_log, ["ran:dispatch", "short-circuit:meter"]);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn flag_distinct_id_is_user_id_string() {
    struct Spy(Mutex<Vec<Option<String>>>);
    #[async_trait::async_trait]
    impl FlagResolver for Spy {
        async fn enabled(&self, _f: &str, id: Option<&str>) -> bool {
            self.0.lock().unwrap().push(id.map(str::to_owned));
            true
        }
    }
    let spy = Arc::new(Spy(Mutex::new(vec![])));
    let stages: Vec<Arc<dyn Stage>> = vec![Arc::new(Probe::new("cache").flag("f"))];
    let p = ProxyPipeline::new(stages, spy.clone());
    let mut ctx = PipelineContext::default();
    p.run(&mut ctx).await.unwrap();
    assert_eq!(spy.0.lock().unwrap()[0], None);
    ctx.user = Some(auth::UserContext {
        org_id: 1,
        user_id: 42,
        tenant: "1".into(),
        scopes: Default::default(),
        teams: vec![],
        auth_mode: auth::AuthMode::ApiKey,
        api_key_id: None,
        team_id: None,
        team_role: None,
        role: None,
    });
    p.run(&mut ctx).await.unwrap();
    assert_eq!(spy.0.lock().unwrap()[1].as_deref(), Some("42"));
}

#[tokio::test]
async fn posthog_resolver_fails_safe_off_without_installed_client() {
    assert!(
        !PosthogFlagResolver::default()
            .enabled("any.flag", Some("1"))
            .await
    );
}

#[tokio::test]
async fn seam_impls_noner_identity_and_extensions() {
    let req = NerRequest {
        text: "x",
        entities: &[],
        org_id: 1,
        content_hash: [0; 32],
    };
    assert_eq!(
        NoNer.analyze(&req).await.unwrap_err(),
        NerError::Unavailable
    );

    let map = PseudonymMap::default();
    assert!(map.is_empty());
    assert_eq!(IdentityDepseudonymizer.restore_text(&map, "hi"), "hi");
    let mut t = IdentityDepseudonymizer.restore_stream(&map);
    assert_eq!(t.push("ab"), "ab");
    assert_eq!(t.finish(), "");

    let mut e = Extensions::default();
    assert_eq!(e.insert(5u32), None);
    assert_eq!(e.insert(6u32), Some(5));
    *e.get_mut::<u32>().unwrap() += 1;
    assert_eq!(e.get::<u32>(), Some(&7));
    assert_eq!(e.remove::<u32>(), Some(7));
    assert!(e.get::<u32>().is_none());

    let mut m = PseudonymMap::default();
    m.insert("<P1>".into(), "Alice".into());
    assert_eq!(
        (m.get("<P1>"), m.len(), m.iter().count()),
        (Some("Alice"), 1, 1)
    );
}
