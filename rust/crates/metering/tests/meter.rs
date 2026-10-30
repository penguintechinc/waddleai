//! `MeterStage` + `MeteringBuffer` behaviour with fake writers/gates/backends (no DB, no Valkey).
//! The column-for-column Python parity test needs a live Postgres and is `#[ignore]`d.

use std::collections::{BTreeSet, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use auth::{AuthMode as UserAuthMode, UserContext};
use chrono::NaiveDate;
use metering::{
    aggregate_events, Aggregate, BufferConfig, EventUsage, MeterStage, MeteringBuffer,
    MeteringEvent, SeaOrmLedger, SeaOrmUsageWriter, UsageWriter, WriteError,
};
use pipeline::{AbortSnapshot, CacheStatus, PipelineContext, Stage, StageError, Usage, VkeyBudget};
use quota::{
    CreditError, CreditModel, Ledger, LedgerEntry, LedgerScope, Outcome, QuotaBackend, QuotaError,
    ReconcileResult, Reservation,
};
use sea_orm::{DatabaseBackend, MockDatabase, MockExecResult};
use serde_json::{json, Value};
use telemetry::Metrics;
use tokens::{BudgetGate, GateDecision, KeyLimits};

// ---------- fakes ----------

#[derive(Default)]
struct RecWriter {
    rows: Mutex<Vec<Aggregate>>,
    fail: Mutex<bool>,
    attempts: AtomicUsize,
}

impl RecWriter {
    fn set_fail(&self, v: bool) {
        *self.fail.lock().unwrap() = v;
    }
    fn rows(&self) -> Vec<Aggregate> {
        self.rows.lock().unwrap().clone()
    }
}

#[async_trait::async_trait]
impl UsageWriter for RecWriter {
    async fn write(&self, agg: &Aggregate) -> Result<(), WriteError> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        if *self.fail.lock().unwrap() {
            return Err(WriteError("db down".into()));
        }
        self.rows.lock().unwrap().push(agg.clone());
        Ok(())
    }
}

#[derive(Default)]
struct Gate {
    reconciles: Mutex<Vec<(String, u64)>>,
}

#[async_trait::async_trait]
impl BudgetGate for Gate {
    async fn reserve(&self, _v: i64, _t: u64, _u: f64, _l: &KeyLimits) -> GateDecision {
        GateDecision::allow(None)
    }
    async fn reconcile(&self, id: &str, actual_tokens: u64, _usd: f64) {
        self.reconciles
            .lock()
            .unwrap()
            .push((id.to_owned(), actual_tokens));
    }
}

struct Credits {
    fail: bool,
}

#[async_trait::async_trait]
impl CreditModel for Credits {
    fn estimate_credits(&self, _m: &str, _i: u64, _o: u64) -> u64 {
        1
    }
    fn providers_for_model(&self, _m: &str) -> HashSet<String> {
        HashSet::new()
    }
    fn count_tokens(&self, _t: &str, _p: &str, _m: &str) -> Result<u64, CreditError> {
        Ok(0)
    }
    fn calculate_waddleai_tokens(
        &self,
        i: u64,
        o: u64,
        _p: &str,
        _m: &str,
    ) -> Result<u64, CreditError> {
        if self.fail {
            return Err(CreditError("no rate".into()));
        }
        Ok((i + o) * 2)
    }
}

/// `(reservation_id, actual_credits, provider, model, cache_hit)` per reconcile call.
type ReconcileCall = (String, i64, String, String, bool);

#[derive(Default)]
struct Backend {
    reconciles: Mutex<Vec<ReconcileCall>>,
    faults: Mutex<Vec<String>>,
    credits_fail: bool,
}

#[async_trait::async_trait]
impl QuotaBackend for Backend {
    async fn reserve(
        &self,
        _u: &UserContext,
        _m: Option<&str>,
        _msgs: &[Value],
    ) -> Result<Outcome, QuotaError> {
        Ok(Outcome::proceed())
    }
    async fn reconcile(
        &self,
        r: &Reservation,
        actual: i64,
        p: &str,
        m: &str,
        c: bool,
    ) -> ReconcileResult {
        self.reconciles.lock().unwrap().push((
            r.reservation_id.clone(),
            actual,
            p.into(),
            m.into(),
            c,
        ));
        ReconcileResult::default()
    }
    async fn release(&self, _r: &Reservation) {}
    fn credits(&self) -> &dyn CreditModel {
        if self.credits_fail {
            &Credits { fail: true }
        } else {
            &Credits { fail: false }
        }
    }
    fn fault(&self, k: &str) {
        self.faults.lock().unwrap().push(k.to_owned());
    }
}

#[derive(Default)]
struct GuardCalls {
    aborted: AtomicUsize,
}

struct Guard(Arc<GuardCalls>);

#[async_trait::async_trait]
impl pipeline::QuotaGuard for Guard {
    async fn release(self: Box<Self>) -> Result<(), StageError> {
        Ok(())
    }
    async fn settle_aborted(self: Box<Self>, _s: AbortSnapshot) -> Result<(), StageError> {
        self.0.aborted.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}

fn reservation() -> Reservation {
    Reservation {
        reservation_id: "rid1".into(),
        org_id: 5,
        team_id: 3,
        user_id: 7,
        cycle: 2,
        credits_reserved: 50,
        from_reservation: 30,
        from_buffer: 20,
        model_requested: Some("llama3.2".into()),
    }
}

fn user() -> UserContext {
    UserContext {
        org_id: 5,
        user_id: 7,
        tenant: "5".into(),
        scopes: BTreeSet::new(),
        teams: vec![],
        auth_mode: UserAuthMode::Jwt,
        api_key_id: None,
        team_id: Some(3),
        team_role: None,
        role: None,
    }
}

fn ctx(vkey: Option<i64>) -> PipelineContext {
    let mut c = PipelineContext::new(json!({}));
    c.user = Some(user());
    c.model = Some("llama3.2".into());
    c.provider = Some("ollama".into());
    c.usage = Some(Usage {
        input_tokens: 11,
        output_tokens: 4,
        ..Default::default()
    });
    if let Some(v) = vkey {
        c.extensions.insert(VkeyBudget {
            vkey_id: v,
            limits: None,
        });
    }
    c
}

fn buffer(w: Arc<RecWriter>) -> Arc<MeteringBuffer> {
    MeteringBuffer::new(w, BufferConfig::default(), None)
}

fn stage(w: &Arc<RecWriter>, gate: &Arc<Gate>) -> (MeterStage, Arc<MeteringBuffer>) {
    let b = buffer(w.clone());
    (MeterStage::new(b.clone(), gate.clone()), b)
}

fn at(h: u32, m: u32, s: u32) -> chrono::NaiveDateTime {
    NaiveDate::from_ymd_opt(2026, 10, 30)
        .unwrap()
        .and_hms_opt(h, m, s)
        .unwrap()
}

fn event(vkey: i64, usage: Option<(u64, u64)>) -> MeteringEvent {
    MeteringEvent {
        virtual_key_id: vkey,
        model: "gpt-4".into(),
        provider: "openai".into(),
        usage: usage.map(|(i, o)| EventUsage {
            input_tokens: i,
            output_tokens: o,
        }),
        timestamp: at(12, 0, 5),
        estimated: false,
        cache_status: None,
        tokens_saved: 0,
    }
}

// ---------- stage ----------

#[test]
fn stage_name_is_meter() {
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    assert_eq!(s.name(), "meter");
}

#[tokio::test]
async fn skips_without_vkey() {
    let w = Arc::new(RecWriter::default());
    let g = Arc::new(Gate::default());
    let (s, b) = stage(&w, &g);
    let mut c = ctx(None);
    c.reservation_id = Some("r".into());
    s.call(&mut c).await.unwrap();
    b.flush().await;
    assert!(w.rows().is_empty());
    assert!(g.reconciles.lock().unwrap().is_empty());
}

#[tokio::test]
async fn records_event_when_usage_provider_model_present_incl_cache_provider() {
    let w = Arc::new(RecWriter::default());
    let g = Arc::new(Gate::default());
    let (s, b) = stage(&w, &g);
    s.call(&mut ctx(Some(9))).await.unwrap();
    // A cache hit: provider "cache", cache_status + tokens_saved carried to the row.
    let mut hit = ctx(Some(9));
    hit.provider = Some("cache".into());
    hit.cache_hit = true;
    hit.cache_status = CacheStatus::Exact;
    hit.tokens_saved = 40;
    s.call(&mut hit).await.unwrap();
    b.flush().await;
    let rows = w.rows();
    assert_eq!(rows.len(), 2);
    let live = rows.iter().find(|r| r.provider == "ollama").unwrap();
    assert_eq!(
        (
            live.virtual_key_id,
            live.model.as_str(),
            live.total_input_tokens,
            live.total_output_tokens,
            live.request_count
        ),
        (9, "llama3.2", 11, 4, 1)
    );
    assert!(!live.has_estimated_usage);
    assert_eq!(live.cache_status.as_deref(), Some("miss"));
    let cached = rows.iter().find(|r| r.provider == "cache").unwrap();
    assert_eq!(cached.cache_status.as_deref(), Some("exact"));
    assert_eq!(cached.total_tokens_saved, 40);
}

#[tokio::test]
async fn no_event_without_usage() {
    let w = Arc::new(RecWriter::default());
    let g = Arc::new(Gate::default());
    let (s, b) = stage(&w, &g);
    let mut no_usage = ctx(Some(9));
    no_usage.usage = None;
    s.call(&mut no_usage).await.unwrap();
    let mut no_provider = ctx(Some(9));
    no_provider.provider = Some(String::new());
    s.call(&mut no_provider).await.unwrap();
    let mut no_model = ctx(Some(9));
    no_model.model = None;
    s.call(&mut no_model).await.unwrap();
    b.flush().await;
    assert!(w.rows().is_empty());
}

#[tokio::test]
async fn reconciles_token_budget_reservation_with_input_plus_output() {
    let w = Arc::new(RecWriter::default());
    let g = Arc::new(Gate::default());
    let (s, _) = stage(&w, &g);
    let mut c = ctx(Some(9));
    c.reservation_id = Some("tb1".into());
    s.call(&mut c).await.unwrap();
    assert_eq!(*g.reconciles.lock().unwrap(), vec![("tb1".to_owned(), 15)]);
    // No usage -> Python skips the reconcile (`if ctx.reservation_id and ctx.usage`).
    let mut none = ctx(Some(9));
    none.usage = None;
    none.reservation_id = Some("tb2".into());
    s.call(&mut none).await.unwrap();
    assert_eq!(g.reconciles.lock().unwrap().len(), 1);
}

fn quota_ctx(calls: &Arc<GuardCalls>) -> PipelineContext {
    let mut c = ctx(Some(9));
    c.quota_reservation = Some(pipeline::QuotaReservation {
        id: "rid1".into(),
        tokens: 50,
    });
    c.quota_release = Some(Box::new(Guard(calls.clone())));
    c.extensions.insert(reservation());
    c
}

#[tokio::test]
async fn quota_reconcile_called_once_then_guard_cleared() {
    let w = Arc::new(RecWriter::default());
    let g = Arc::new(Gate::default());
    let backend = Arc::new(Backend::default());
    let (s, _) = stage(&w, &g);
    let s = s.with_quota(backend.clone());
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    s.call(&mut c).await.unwrap();
    // Actual credits = (11 + 4) * 2 from the credit model, ACTUAL provider/model.
    assert_eq!(
        *backend.reconciles.lock().unwrap(),
        vec![("rid1".into(), 30, "ollama".into(), "llama3.2".into(), false)]
    );
    assert!(c.quota_release.is_none() && c.quota_reservation.is_none());
    assert!(c.extensions.get::<Reservation>().is_none());
    // The normal path never also settles the abort guard (no double reconcile).
    assert_eq!(calls.aborted.load(Ordering::SeqCst), 0);
    // A second pass is a no-op: nothing left to reconcile.
    s.call(&mut c).await.unwrap();
    assert_eq!(backend.reconciles.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn quota_reconciles_even_without_vkey() {
    let w = Arc::new(RecWriter::default());
    let backend = Arc::new(Backend::default());
    let (s, _) = stage(&w, &Arc::new(Gate::default()));
    let s = s.with_quota(backend.clone());
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    c.extensions.remove::<VkeyBudget>();
    s.call(&mut c).await.unwrap();
    assert_eq!(backend.reconciles.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn quota_without_usage_reconciles_zero_credits_like_python() {
    let backend = Arc::new(Backend::default());
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    let s = s.with_quota(backend.clone());
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    c.usage = None;
    s.call(&mut c).await.unwrap();
    assert_eq!(backend.reconciles.lock().unwrap()[0].1, 0);
}

#[tokio::test]
async fn quota_credit_failure_charges_the_reservation_and_faults() {
    let backend = Arc::new(Backend {
        credits_fail: true,
        ..Default::default()
    });
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    let s = s.with_quota(backend.clone());
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    s.call(&mut c).await.unwrap();
    assert_eq!(backend.reconciles.lock().unwrap()[0].1, 50);
    assert_eq!(*backend.faults.lock().unwrap(), vec!["reconcile_error"]);
}

#[tokio::test]
async fn quota_without_full_reservation_settles_the_guard_instead_of_leaking() {
    let backend = Arc::new(Backend::default());
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    let s = s.with_quota(backend.clone());
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    c.extensions.remove::<Reservation>();
    s.call(&mut c).await.unwrap();
    assert!(backend.reconciles.lock().unwrap().is_empty());
    assert_eq!(calls.aborted.load(Ordering::SeqCst), 1);
    assert!(c.quota_release.is_none());
}

#[tokio::test]
async fn quota_guard_without_backend_is_settled_not_leaked() {
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    let calls = Arc::new(GuardCalls::default());
    let mut c = quota_ctx(&calls);
    s.call(&mut c).await.unwrap();
    assert_eq!(calls.aborted.load(Ordering::SeqCst), 1);
}

// ---------- aggregation / estimate ----------

#[test]
fn estimated_flag_false_for_provider_usage_true_when_estimated() {
    // Provider-reported usage: not estimated.
    let real = aggregate_events(vec![event(1, Some((10, 5)))]);
    assert!(!real[0].has_estimated_usage);
    assert_eq!(
        (real[0].total_input_tokens, real[0].total_output_tokens),
        (10, 5)
    );
    // Missing usage: Python's placeholder estimate (1 in / 1 out) and estimated = true.
    let est = aggregate_events(vec![event(1, None)]);
    assert!(est[0].has_estimated_usage);
    assert_eq!(
        (est[0].total_input_tokens, est[0].total_output_tokens),
        (1, 1)
    );
}

#[test]
fn aggregates_by_vkey_model_provider_minute() {
    let mut a = event(1, Some((10, 5)));
    a.tokens_saved = 3;
    a.cache_status = Some("exact".into());
    let mut b = event(1, Some((1, 1)));
    b.timestamp = at(12, 0, 59); // same minute
    b.cache_status = Some("miss".into());
    let mut c = event(1, Some((2, 2)));
    c.timestamp = at(12, 1, 0); // next minute
    let d = event(2, Some((7, 7))); // other vkey
    let out = aggregate_events(vec![a, b, c, d]);
    assert_eq!(out.len(), 3);
    assert_eq!(out[0].request_count, 2);
    assert_eq!(out[0].total_input_tokens, 11);
    assert_eq!(out[0].total_tokens_saved, 3);
    assert_eq!(out[0].cache_status.as_deref(), Some("miss")); // last non-None wins
    assert_eq!(out[0].minute_bucket, at(12, 0, 0));
}

// ---------- buffer ----------

#[tokio::test(start_paused = true)]
async fn buffer_flushes_on_size_interval_and_shutdown_no_loss() {
    let w = Arc::new(RecWriter::default());
    let cfg = BufferConfig {
        interval: Duration::from_secs(1),
        size_threshold: 3,
        max_pending: 1000,
    };
    let b = MeteringBuffer::new(w.clone(), cfg, None);
    b.start();
    // Size: reaching the threshold wakes the flusher without waiting for the interval.
    for i in 0..3 {
        b.record(event(100 + i, Some((1, 1))));
    }
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert_eq!(w.rows().len(), 3, "size threshold flush");
    // Interval: a lone event goes out on the timer.
    b.record(event(200, Some((1, 1))));
    assert_eq!(w.rows().len(), 3);
    tokio::time::sleep(Duration::from_millis(1100)).await;
    assert_eq!(w.rows().len(), 4, "interval flush");
    // Shutdown: whatever is still buffered is flushed, nothing lost.
    b.record(event(300, Some((1, 1))));
    b.record(event(301, Some((1, 1))));
    b.shutdown().await;
    assert_eq!(w.rows().len(), 6, "shutdown flush");
    let total: u64 = w.rows().iter().map(|r| r.request_count).sum();
    assert_eq!(total, 6);
}

#[tokio::test]
async fn db_down_buffers_and_retries_never_fails_response() {
    let w = Arc::new(RecWriter::default());
    let m = Arc::new(Metrics::new("test").unwrap());
    let b = MeteringBuffer::new(w.clone(), BufferConfig::default(), Some(m.clone()));
    let g = Arc::new(Gate::default());
    let s = MeterStage::new(b.clone(), g).with_metrics(m.clone());
    w.set_fail(true);
    // The stage (the response path) never fails or blocks on a dead DB.
    let mut c = ctx(Some(9));
    s.call(&mut c).await.unwrap();
    assert!(!c.blocked && c.status_code == 200);
    b.flush().await;
    assert_eq!(b.pending_len(), 1, "failed aggregate is kept for retry");
    assert!(w.rows().is_empty());
    let err = m
        .counter("waddleai_database_operations_total")
        .unwrap()
        .with_label_values(&["upsert", "token_usage", "error"])
        .get();
    assert_eq!(err, 1.0);
    // DB back: the retry lands and nothing was lost.
    w.set_fail(false);
    b.flush().await;
    assert_eq!(b.pending_len(), 0);
    assert_eq!(w.rows().len(), 1);
    assert_eq!(w.rows()[0].total_input_tokens, 11);
    let ok = m
        .counter("waddleai_database_operations_total")
        .unwrap()
        .with_label_values(&["upsert", "token_usage", "success"])
        .get();
    assert_eq!(ok, 1.0);
}

#[tokio::test]
async fn retry_queue_is_bounded_dropping_oldest() {
    let w = Arc::new(RecWriter::default());
    w.set_fail(true);
    let cfg = BufferConfig {
        max_pending: 2,
        ..BufferConfig::default()
    };
    let b = MeteringBuffer::new(w.clone(), cfg, None);
    for i in 0..4 {
        b.record(event(i, Some((1, 1))));
    }
    b.flush().await;
    assert_eq!(b.pending_len(), 2);
    w.set_fail(false);
    b.flush().await;
    let kept: Vec<i64> = w.rows().iter().map(|r| r.virtual_key_id).collect();
    assert_eq!(kept, vec![2, 3], "oldest dropped");
}

// ---------- metrics parity ----------

#[tokio::test]
async fn metric_names_labels_match_python() {
    let m = Arc::new(Metrics::new("test").unwrap());
    let (s, _) = stage(&Arc::new(RecWriter::default()), &Arc::new(Gate::default()));
    let backend = Arc::new(Backend::default());
    let s = s.with_metrics(m.clone()).with_quota(backend);
    s.call(&mut ctx(Some(9))).await.unwrap();
    // `_finalize`'s record_llm_request: names + label order from shared/utils/metrics.py.
    let req = m
        .counter("waddleai_llm_requests_total")
        .unwrap()
        .with_label_values(&["ollama", "llama3.2", "success"])
        .get();
    assert_eq!(req, 1.0);
    let tok = m.counter("waddleai_llm_tokens_total").unwrap();
    assert_eq!(
        tok.with_label_values(&["ollama", "llama3.2", "input"])
            .get(),
        11.0
    );
    assert_eq!(
        tok.with_label_values(&["ollama", "llama3.2", "output"])
            .get(),
        4.0
    );
    // Per-user label is dropped in Python (`user` = "unknown"); organization is bounded.
    let norm = m
        .counter("waddleai_normalized_tokens_total")
        .unwrap()
        .with_label_values(&["5", "unknown", "ollama"])
        .get();
    assert_eq!(norm, 30.0);
    let text = m.render().unwrap();
    assert!(text.contains(
        "waddleai_llm_requests_total{model=\"llama3.2\",provider=\"ollama\",status=\"success\"} 1"
    ));
}

// ---------- SeaORM writer / ledger (mock DB: SQL shape, real DB parity is #[ignore]) ----------

fn usage_row(id: i32, input: i32, output: i32, reqs: i32) -> db::entities::token_usage::Model {
    db::entities::token_usage::Model {
        id,
        virtual_key_id: Some(1),
        user_id: None,
        organization_id: None,
        date: Some(at(0, 0, 0)),
        waddleai_tokens: Some(0),
        llm_tokens: Some(Value::String(
            r#"{"openai_gpt_4": {"input": 10, "output": 5}}"#.into(),
        )),
        tokens_input_total: Some(input),
        tokens_output_total: Some(output),
        request_count: Some(reqs),
        cost_usd_total: Some(0),
        last_updated: None,
        source: "aiproxy".into(),
        estimated: false,
        cache_status: Some("exact".into()),
        tokens_saved: 2,
        api_key_id: None,
    }
}

/// Every executed statement (SQL text then bound values), flattened for substring asserts.
fn sql_log(db: sea_orm::DatabaseConnection) -> String {
    db.into_transaction_log()
        .iter()
        .flat_map(|t| t.statements().iter())
        .map(|s| format!("{}\n{:?}", s.sql, s.values))
        .collect::<Vec<_>>()
        .join("\n")
}

fn one_agg() -> Aggregate {
    aggregate_events(vec![event(1, Some((10, 5)))]).remove(0)
}

#[tokio::test]
async fn writer_inserts_when_no_row_for_vkey_and_day() {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([Vec::<db::entities::token_usage::Model>::new()])
        .append_exec_results([MockExecResult {
            last_insert_id: 1,
            rows_affected: 1,
        }])
        .into_connection();
    let w = SeaOrmUsageWriter::new(db.clone());
    w.write(&one_agg()).await.unwrap();
    let log = sql_log(db);
    assert!(
        log.contains("INSERT INTO \"public\".\"token_usage\""),
        "{log}"
    );
    assert!(log.contains("aiproxy"), "{log}");
    assert!(log.contains("BEGIN") || log.contains("SELECT"), "{log}");
}

#[tokio::test]
async fn writer_updates_existing_row_merging_breakdown_and_totals() {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![usage_row(7, 100, 50, 4)]])
        .append_exec_results([MockExecResult {
            last_insert_id: 7,
            rows_affected: 1,
        }])
        .into_connection();
    let w = SeaOrmUsageWriter::new(db.clone());
    w.write(&one_agg()).await.unwrap();
    let log = sql_log(db);
    assert!(log.contains("UPDATE \"public\".\"token_usage\""), "{log}");
    assert!(log.contains("FOR UPDATE"), "row lock on the read: {log}");
    // 100 + 10 in, 50 + 5 out, 4 + 1 requests; the breakdown is a JSON *string* like Python.
    for needle in ["110", "55"] {
        assert!(log.contains(needle), "{needle} in {log}");
    }
    assert!(log.contains("openai_gpt_4"), "{log}");
}

#[tokio::test]
async fn writer_surfaces_db_errors() {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_errors([sea_orm::DbErr::Custom("boom".into())])
        .into_connection();
    let w = SeaOrmUsageWriter::new(db);
    assert!(w.write(&one_agg()).await.is_err());
}

#[tokio::test]
async fn ledger_upserts_additively_per_scope_matching_python() {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_exec_results([
            MockExecResult {
                last_insert_id: 1,
                rows_affected: 1,
            },
            MockExecResult {
                last_insert_id: 2,
                rows_affected: 1,
            },
        ])
        .into_connection();
    let l = SeaOrmLedger::new(db.clone());
    l.add(
        5,
        2,
        &[
            LedgerEntry {
                scope: LedgerScope::User,
                scope_id: 7,
                credits: 30,
            },
            LedgerEntry {
                scope: LedgerScope::TeamBuffer,
                scope_id: 3,
                credits: 20,
            },
            LedgerEntry {
                scope: LedgerScope::Override,
                scope_id: 9,
                credits: 0,
            }, // skipped
        ],
    )
    .await
    .unwrap();
    let log = sql_log(db);
    assert_eq!(
        log.matches("INSERT INTO \"public\".\"quota_usage_ledger\"")
            .count(),
        2,
        "{log}"
    );
    assert!(log.contains("ON CONFLICT"), "{log}");
    assert!(
        log.contains("\"credits_used\" = \"quota_usage_ledger\".\"credits_used\" + "),
        "{log}"
    );
    assert!(
        log.contains("team_buffer") && log.contains("\"user\""),
        "{log}"
    );
    assert!(
        !log.contains("override"),
        "non-positive credits are skipped: {log}"
    );
}

#[tokio::test]
async fn ledger_failure_is_an_error_not_a_silent_noop() {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_exec_errors([sea_orm::DbErr::Custom("down".into())])
        .into_connection();
    let l = SeaOrmLedger::new(db);
    let r = l
        .add(
            1,
            0,
            &[LedgerEntry {
                scope: LedgerScope::User,
                scope_id: 1,
                credits: 5,
            }],
        )
        .await;
    assert!(r.is_err());
}

#[tokio::test]
async fn ledger_rejects_ids_beyond_the_int4_columns() {
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    let l = SeaOrmLedger::new(db);
    let r = l
        .add(
            i64::from(i32::MAX) + 1,
            0,
            &[LedgerEntry {
                scope: LedgerScope::User,
                scope_id: 1,
                credits: 5,
            }],
        )
        .await;
    assert!(r.is_err());
}

// ---------- live DB (CI provides Postgres) ----------

/// Column-for-column parity with the Python writer. Both writers insert one aggregate for the
/// same (model, provider, day) under different vkeys; every column but the vkey, the surrogate
/// id and `last_updated` must match, with `llm_tokens` compared after JSON-string decoding.
#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_DATABASE_URL (migrated Postgres) + python (WADDLEAI_TEST_PYTHON, default python3) with repo deps; CI provides"]
async fn row_schema_equals_python_written_row() {
    use sea_orm::{ConnectionTrait, Database, Statement};
    let url = std::env::var("WADDLEAI_TEST_DATABASE_URL")
        .expect("WADDLEAI_TEST_DATABASE_URL must be set for --ignored db tests");
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let db = Database::connect(&url).await.unwrap();
    let (rust_vk, py_vk) = (900_001_i64, 900_002_i64);
    for vk in [rust_vk, py_vk] {
        db.execute_unprepared(&format!(
            "DELETE FROM token_usage WHERE virtual_key_id = {vk}"
        ))
        .await
        .unwrap();
    }
    // Rust write.
    let mut a = one_agg();
    a.virtual_key_id = rust_vk;
    a.total_tokens_saved = 3;
    a.cache_status = Some("exact".into());
    SeaOrmUsageWriter::new(db.clone()).write(&a).await.unwrap();
    // Python write (the oracle) through its own writer.
    let script = format!(
        "import os,sys;from datetime import datetime;from penguin_dal import DAL;\
         from shared.utils.metering import AggregatedMetrics,PenguinDALUsageWriter;\
         db=DAL(os.environ['WADDLEAI_TEST_DATABASE_URL'].replace('postgres://','postgresql://'));\
         PenguinDALUsageWriter(db).write_aggregated_row(AggregatedMetrics(virtual_key_id={py_vk},\
         model='gpt-4',provider='openai',minute_bucket=datetime(2026,10,30,12,0),\
         total_input_tokens=10,total_output_tokens=5,request_count=1,total_tokens_saved=3,\
         cache_status='exact'))"
    );
    let py = std::env::var("WADDLEAI_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let out = std::process::Command::new(py)
        .args(["-c", &script])
        .env("PYTHONPATH", &repo)
        .env("WADDLEAI_TEST_DATABASE_URL", &url)
        .current_dir(&repo)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "python writer failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let q = |vk: i64| {
        Statement::from_string(
            DatabaseBackend::Postgres,
            format!(
                "SELECT user_id, organization_id, date::date::text AS d, waddleai_tokens, \
                 llm_tokens #>> '{{}}' AS llm, tokens_input_total, tokens_output_total, \
                 request_count, cost_usd_total, source, estimated, cache_status, tokens_saved, \
                 api_key_id FROM token_usage WHERE virtual_key_id = {vk}"
            ),
        )
    };
    let (r, p) = (
        db.query_all_raw(q(rust_vk)).await.unwrap(),
        db.query_all_raw(q(py_vk)).await.unwrap(),
    );
    assert_eq!((r.len(), p.len()), (1, 1));
    let cols = ["d", "source", "cache_status"];
    for c in cols {
        assert_eq!(
            r[0].try_get::<Option<String>>("", c).ok(),
            p[0].try_get::<Option<String>>("", c).ok(),
            "{c}"
        );
    }
    let ints = [
        "waddleai_tokens",
        "tokens_input_total",
        "tokens_output_total",
        "request_count",
        "cost_usd_total",
        "tokens_saved",
    ];
    for c in ints {
        assert_eq!(
            r[0].try_get::<Option<i32>>("", c).unwrap(),
            p[0].try_get::<Option<i32>>("", c).unwrap(),
            "{c}"
        );
    }
    for c in ["user_id", "organization_id", "api_key_id"] {
        assert_eq!(r[0].try_get::<Option<i32>>("", c).unwrap(), None, "{c}");
        assert_eq!(p[0].try_get::<Option<i32>>("", c).unwrap(), None, "{c}");
    }
    assert_eq!(
        r[0].try_get::<bool>("", "estimated").unwrap(),
        p[0].try_get::<bool>("", "estimated").unwrap()
    );
    let parse = |row: &sea_orm::QueryResult| -> Value {
        serde_json::from_str(&row.try_get::<String>("", "llm").unwrap()).unwrap()
    };
    assert_eq!(parse(&r[0]), parse(&p[0]));
    // Byte-identical JSON text too (same separators as Python json.dumps).
    assert_eq!(
        r[0].try_get::<String>("", "llm").unwrap(),
        p[0].try_get::<String>("", "llm").unwrap()
    );
    for vk in [rust_vk, py_vk] {
        db.execute_unprepared(&format!(
            "DELETE FROM token_usage WHERE virtual_key_id = {vk}"
        ))
        .await
        .unwrap();
    }
}

/// Connects to the live test database or panics with the env-var hint.
async fn live_db() -> sea_orm::DatabaseConnection {
    let url = std::env::var("WADDLEAI_TEST_DATABASE_URL")
        .expect("WADDLEAI_TEST_DATABASE_URL must be set for --ignored db tests");
    sea_orm::Database::connect(&url).await.unwrap()
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_DATABASE_URL (migrated Postgres); CI provides"]
async fn live_writer_inserts_then_adds_to_the_same_day_row() {
    use sea_orm::ConnectionTrait;
    let db = live_db().await;
    db.execute_unprepared("DELETE FROM token_usage WHERE virtual_key_id = 900003")
        .await
        .unwrap();
    let w = SeaOrmUsageWriter::new(db.clone());
    let mut a = one_agg();
    a.virtual_key_id = 900_003;
    w.write(&a).await.unwrap();
    w.write(&a).await.unwrap(); // second flush of the day adds, never duplicates
    let rows = db
        .query_all_raw(sea_orm::Statement::from_string(
            DatabaseBackend::Postgres,
            "SELECT tokens_input_total, request_count, llm_tokens #>> '{}' AS llm FROM token_usage WHERE virtual_key_id = 900003",
        ))
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(
        rows[0].try_get::<i32>("", "tokens_input_total").unwrap(),
        20
    );
    assert_eq!(rows[0].try_get::<i32>("", "request_count").unwrap(), 2);
    let llm: Value = serde_json::from_str(&rows[0].try_get::<String>("", "llm").unwrap()).unwrap();
    assert_eq!(llm["openai_gpt_4"], json!({"input": 20, "output": 10}));
    db.execute_unprepared("DELETE FROM token_usage WHERE virtual_key_id = 900003")
        .await
        .unwrap();
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_DATABASE_URL (migrated Postgres); CI provides"]
async fn live_ledger_upsert_adds_atomically_under_concurrency() {
    use sea_orm::ConnectionTrait;
    let db = live_db().await;
    db.execute_unprepared("DELETE FROM quota_usage_ledger WHERE organization_id = 900004")
        .await
        .unwrap();
    let l = Arc::new(SeaOrmLedger::new(db.clone()));
    let mut tasks = Vec::new();
    for _ in 0..20 {
        let l = l.clone();
        tasks.push(tokio::spawn(async move {
            l.add(
                900_004,
                1,
                &[LedgerEntry {
                    scope: LedgerScope::TeamBuffer,
                    scope_id: 3,
                    credits: 5,
                }],
            )
            .await
        }));
    }
    for t in tasks {
        t.await.unwrap().unwrap();
    }
    let rows = db
        .query_all_raw(sea_orm::Statement::from_string(
            DatabaseBackend::Postgres,
            "SELECT credits_used FROM quota_usage_ledger WHERE organization_id = 900004 AND scope_type = 'team_buffer'",
        ))
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].try_get::<i64>("", "credits_used").unwrap(), 100);
    db.execute_unprepared("DELETE FROM quota_usage_ledger WHERE organization_id = 900004")
        .await
        .unwrap();
}
