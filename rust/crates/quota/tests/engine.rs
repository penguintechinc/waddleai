//! `QuotaEngine`/`QuotaLimiter`/`QuotaGuard` against a LIVE Valkey. All tests are `#[ignore]`
//! (they need infrastructure; CI provides Valkey): run with
//! `WADDLEAI_TEST_VALKEY_URL=redis://127.0.0.1:6379 cargo test -p quota --test engine -- --ignored`.
//! When explicitly run without the URL they FAIL rather than skip. Every test uses unique
//! user/team/org/override ids so reruns and parallel tests never share counters.

use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};

use auth::{AuthMode, UserContext};
use chrono::NaiveDate;
use pipeline::{AbortSnapshot, QuotaGuard as _, StaticFlagResolver, Usage};
use quota::{
    ConfigError, ConfigStore, CreditError, CreditModel, Ledger, LedgerEntry, LedgerError,
    LedgerScope, OrgQuotaConfig, OverrideRule, QuotaBackend, QuotaEngine, QuotaGuard, QuotaLimiter,
    ReserveRequest, TeamQuotaConfig, TokenCredits, UserReservation, POOLED_QUOTA_FLAG,
};
use redis::AsyncCommands;
use serde_json::json;
use tokens::ConversionRates;

type Conn = redis::aio::MultiplexedConnection;

async fn conn() -> Conn {
    let url = std::env::var("WADDLEAI_TEST_VALKEY_URL")
        .expect("WADDLEAI_TEST_VALKEY_URL must be set for --ignored valkey tests");
    redis::Client::open(url)
        .expect("valid url")
        .get_multiplexed_async_connection()
        .await
        .expect("valkey reachable")
}

/// Unique positive id that fits an `i32` (UserContext ids).
fn uniq() -> i64 {
    (uuid::Uuid::new_v4().as_u128() % 1_000_000_000) as i64 + 1_000_000
}

fn today() -> NaiveDate {
    NaiveDate::from_ymd_opt(2031, 3, 10).unwrap()
}

fn anchor() -> NaiveDate {
    NaiveDate::from_ymd_opt(2031, 3, 1).unwrap()
}

struct MemConfig {
    org: Mutex<Option<OrgQuotaConfig>>,
    team: Mutex<Option<TeamQuotaConfig>>,
    fail: bool,
}

#[async_trait::async_trait]
impl ConfigStore for MemConfig {
    async fn get_org(&self, _o: i64) -> Result<Option<OrgQuotaConfig>, ConfigError> {
        if self.fail {
            return Err(ConfigError("down".into()));
        }
        Ok(self.org.lock().unwrap().clone())
    }
    async fn get_team(&self, _t: i64) -> Result<Option<TeamQuotaConfig>, ConfigError> {
        Ok(self.team.lock().unwrap().clone())
    }
}

#[derive(Default)]
struct MemLedger {
    rows: Mutex<Vec<LedgerEntry>>,
    fail: bool,
}

#[async_trait::async_trait]
impl Ledger for MemLedger {
    async fn add(&self, _o: i64, _c: i64, entries: &[LedgerEntry]) -> Result<(), LedgerError> {
        if self.fail {
            return Err(LedgerError("db down".into()));
        }
        self.rows.lock().unwrap().extend_from_slice(entries);
        Ok(())
    }
}

/// Real rates, but token counting/conversion fails: models an abort-time estimate failure.
struct BrokenCounting(TokenCredits);

#[async_trait::async_trait]
impl CreditModel for BrokenCounting {
    fn estimate_credits(&self, m: &str, i: u64, o: u64) -> u64 {
        self.0.estimate_credits(m, i, o)
    }
    fn providers_for_model(&self, m: &str) -> std::collections::HashSet<String> {
        self.0.providers_for_model(m)
    }
    fn count_tokens(&self, _t: &str, _p: &str, _m: &str) -> Result<u64, CreditError> {
        Err(CreditError("tokenizer exploded".into()))
    }
    fn calculate_waddleai_tokens(
        &self,
        _i: u64,
        _o: u64,
        _p: &str,
        _m: &str,
    ) -> Result<u64, CreditError> {
        Err(CreditError("rates exploded".into()))
    }
}

struct World {
    engine: Arc<QuotaEngine>,
    ledger: Arc<MemLedger>,
    conn: Conn,
    user: UserContext,
    org: i64,
    team: i64,
    uid: i64,
}

struct Setup {
    guaranteed: i64,
    buffer: i64,
    daily: Option<i64>,
    overrides: Vec<OverrideRule>,
    flag_on: bool,
    team_id: bool,
    broken_counting: bool,
    ledger_fails: bool,
    config_fails: bool,
    org_zero: bool,
}

impl Default for Setup {
    fn default() -> Self {
        Self {
            guaranteed: 0,
            buffer: 0,
            daily: None,
            overrides: vec![],
            flag_on: true,
            team_id: true,
            broken_counting: false,
            ledger_fails: false,
            config_fails: false,
            org_zero: false,
        }
    }
}

async fn world(s: Setup) -> World {
    let conn = conn().await;
    let (org, team, uid) = (if s.org_zero { 0 } else { uniq() }, uniq(), uniq());
    let org_cfg = OrgQuotaConfig {
        org_id: org,
        enabled: true,
        cycle_anchor_date: Some(anchor()),
        pool_credits: 1_000_000,
        overrides: s.overrides,
    };
    let mut reservations = HashMap::new();
    reservations.insert(
        uid,
        UserReservation {
            user_id: uid,
            guaranteed_credits: s.guaranteed,
            daily_subcap: s.daily,
        },
    );
    let team_cfg = TeamQuotaConfig {
        team_id: team,
        org_id: org,
        pool_credits: 1_000_000,
        shared_buffer_credits: s.buffer,
        reservations,
    };
    let cfg = MemConfig {
        org: Mutex::new(Some(org_cfg)),
        team: Mutex::new(Some(team_cfg)),
        fail: s.config_fails,
    };
    let ledger = Arc::new(MemLedger {
        fail: s.ledger_fails,
        ..Default::default()
    });
    let rates = TokenCredits::new(ConversionRates::defaults());
    let credits: Arc<dyn CreditModel> = if s.broken_counting {
        Arc::new(BrokenCounting(rates))
    } else {
        Arc::new(rates)
    };
    let on: Vec<&str> = if s.flag_on {
        vec![POOLED_QUOTA_FLAG]
    } else {
        vec![]
    };
    let engine = Arc::new(
        QuotaEngine::new(
            Arc::new(QuotaLimiter::new(conn.clone())),
            Arc::new(cfg),
            ledger.clone(),
            credits,
            Arc::new(StaticFlagResolver::new(on)),
        )
        .with_today(Arc::new(today)),
    );
    let user = UserContext {
        org_id: org as i32,
        user_id: uid as i32,
        tenant: org.to_string(),
        scopes: BTreeSet::new(),
        teams: vec![],
        auth_mode: AuthMode::Jwt,
        api_key_id: None,
        team_id: s.team_id.then_some(team as i32),
        team_role: None,
        role: None,
    };
    World {
        engine,
        ledger,
        conn,
        user,
        org,
        team,
        uid,
    }
}

/// 400 chars -> 100 estimated input tokens, 200 output; llama3.2 (50/50) reserves 2 + 4 = 6.
fn msgs() -> Vec<serde_json::Value> {
    vec![json!({"role": "user", "content": "x".repeat(400)})]
}

async fn counter(w: &World, key: String) -> Option<i64> {
    let mut c = w.conn.clone();
    c.get(key).await.unwrap()
}

fn user_key(w: &World) -> String {
    cache::keys::quota::user_counter(w.uid, 0)
}

fn buf_key(w: &World) -> String {
    cache::keys::quota::buffer_counter(w.team, 0)
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn reserve_admits_then_counters_incremented_exactly_as_python() {
    let w = world(Setup {
        guaranteed: 4,
        buffer: 10,
        ..Default::default()
    })
    .await;
    let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
    assert!(!out.blocked);
    let res = out.reservation.expect("admitted");
    assert_eq!(
        (res.credits_reserved, res.from_reservation, res.from_buffer),
        (6, 4, 2)
    );
    assert_eq!(res.cycle, 0);
    assert_eq!(counter(&w, user_key(&w)).await, Some(4));
    assert_eq!(counter(&w, buf_key(&w)).await, Some(2));
    let mut c = w.conn.clone();
    let raw: String = c
        .get(cache::keys::quota::reservation(&res.reservation_id))
        .await
        .unwrap();
    let rec: serde_json::Value = serde_json::from_str(&raw).unwrap();
    assert_eq!(rec["user_key"], user_key(&w));
    assert_eq!(rec["buf_key"], buf_key(&w));
    assert_eq!(
        (rec["from_resv"].as_i64(), rec["from_buf"].as_i64()),
        (Some(4), Some(2))
    );
    assert_eq!(rec["daily_on"], 0);
    let ttl: i64 = c.ttl(user_key(&w)).await.unwrap();
    assert!(ttl > quota::COUNTER_TTL_S - 60 && ttl <= quota::COUNTER_TTL_S);
    let score: Option<f64> = c
        .zscore(cache::keys::quota::RESERVATION_INDEX, &res.reservation_id)
        .await
        .unwrap();
    assert!(score.is_some(), "reservation indexed for the sweeper");
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn reserve_denies_quota_exhausted_daily_subcap_override_exceeded() {
    // quota_exhausted: nothing guaranteed, buffer smaller than the 6-credit lease.
    let w = world(Setup {
        guaranteed: 0,
        buffer: 3,
        ..Default::default()
    })
    .await;
    let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
    assert!(out.blocked && out.reservation.is_none());
    assert_eq!(out.reason.as_deref(), Some("quota_exhausted"));
    assert_eq!(
        counter(&w, buf_key(&w)).await,
        None,
        "a denied request leaves no residue"
    );

    let w = world(Setup {
        guaranteed: 100,
        daily: Some(5),
        ..Default::default()
    })
    .await;
    let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
    assert!(out.blocked);
    assert_eq!(out.reason.as_deref(), Some("daily_subcap_exceeded"));
    assert_eq!(counter(&w, user_key(&w)).await, None);

    let rule = OverrideRule {
        id: uniq(),
        cap_credits: 5,
        user_id: None,
        model: Some("llama3.2".into()),
        provider: None,
    };
    let w = world(Setup {
        guaranteed: 100,
        overrides: vec![rule],
        ..Default::default()
    })
    .await;
    let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
    assert!(out.blocked);
    assert_eq!(out.reason.as_deref(), Some("override_exceeded"));
    assert_eq!(counter(&w, user_key(&w)).await, None);
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn daily_subcap_and_override_counters_written_and_reconciled() {
    let rule = OverrideRule {
        id: uniq(),
        cap_credits: 7,
        user_id: None,
        model: None,
        provider: Some("ollama".into()),
    };
    let rid_key = cache::keys::quota::override_counter(rule.id, 0);
    let w = world(Setup {
        guaranteed: 100,
        daily: Some(500),
        overrides: vec![rule],
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let day = cache::keys::quota::daily_counter(w.uid, "20310310");
    assert_eq!(counter(&w, day.clone()).await, Some(6));
    // Provider rule is enforced at reconcile only (actual provider known): debit actual 9 > cap 7.
    let r = w
        .engine
        .reconcile(&res, 9, "ollama", "llama3.2", false)
        .await;
    assert!(r.applied);
    assert_eq!(r.over_cap_override_ids.len(), 1);
    assert_eq!(counter(&w, rid_key).await, Some(9));
    assert_eq!(counter(&w, day).await, Some(9));
    let rows = w.ledger.rows.lock().unwrap().clone();
    assert!(rows.contains(&LedgerEntry {
        scope: LedgerScope::User,
        scope_id: w.uid,
        credits: 9
    }));
    assert!(rows
        .iter()
        .any(|e| e.scope == LedgerScope::Override && e.credits == 9));
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn reconcile_adjusts_to_actual() {
    let w = world(Setup {
        guaranteed: 4,
        buffer: 10,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    // actual 3 < reserved 6: refund 3 -> buffer first (2), then the user's reservation (1).
    let r = w
        .engine
        .reconcile(&res, 3, "ollama", "llama3.2", false)
        .await;
    assert!(r.applied);
    assert_eq!((r.from_reservation, r.from_buffer), (3, 0));
    assert_eq!(counter(&w, user_key(&w)).await, Some(3));
    assert_eq!(counter(&w, buf_key(&w)).await, Some(0));
    assert_eq!(
        w.ledger.rows.lock().unwrap().clone(),
        vec![LedgerEntry {
            scope: LedgerScope::User,
            scope_id: w.uid,
            credits: 3
        }]
    );
    // A second reconcile finds no record (consumed): never a double refund.
    let again = w
        .engine
        .reconcile(&res, 0, "ollama", "llama3.2", false)
        .await;
    assert!(!again.applied);
    assert_eq!(counter(&w, user_key(&w)).await, Some(3));

    // actual > reserved on a fresh reservation: grows into the buffer once the user's headroom is gone.
    let w = world(Setup {
        guaranteed: 4,
        buffer: 10,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let r = w
        .engine
        .reconcile(&res, 9, "ollama", "llama3.2", false)
        .await;
    assert_eq!((r.from_reservation, r.from_buffer, r.overage), (4, 5, 0));
    assert_eq!(counter(&w, buf_key(&w)).await, Some(5));
    // buffer overage beyond the cap is recorded, never refused
    let w = world(Setup {
        guaranteed: 0,
        buffer: 6,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let r = w
        .engine
        .reconcile(&res, 10, "ollama", "llama3.2", false)
        .await;
    assert_eq!(r.overage, 4);
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn cache_hit_reconcile_bills_zero() {
    let w = world(Setup {
        guaranteed: 50,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let r = w
        .engine
        .reconcile(&res, 99, "cache", "llama3.2", false)
        .await;
    assert!(r.applied);
    assert_eq!(counter(&w, user_key(&w)).await, Some(0));
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn release_refunds_full_reserve() {
    let w = world(Setup {
        guaranteed: 4,
        buffer: 10,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    w.engine.release(&res).await;
    assert_eq!(counter(&w, user_key(&w)).await, Some(0));
    assert_eq!(counter(&w, buf_key(&w)).await, Some(0));
    let mut c = w.conn.clone();
    let exists: bool = c
        .exists(cache::keys::quota::reservation(&res.reservation_id))
        .await
        .unwrap();
    assert!(!exists, "record consumed");
    assert!(
        w.ledger.rows.lock().unwrap().is_empty(),
        "release writes no ledger"
    );
}

fn snap(
    provider: Option<&str>,
    usage: Option<(u64, u64)>,
    text: &str,
    cache_hit: bool,
) -> AbortSnapshot {
    AbortSnapshot {
        provider: provider.map(str::to_owned),
        model: Some("llama3.2".into()),
        requested_model: Some("llama3.2".into()),
        cache_hit,
        response_empty: text.is_empty(),
        response_text: text.to_owned(),
        messages: msgs(),
        usage: usage.map(|(i, o)| Usage {
            input_tokens: i,
            output_tokens: o,
            ..Default::default()
        }),
    }
}

async fn guard(w: &World) -> Box<QuotaGuard> {
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let backend: Arc<dyn QuotaBackend> = w.engine.clone();
    Box::new(QuotaGuard::new(backend, res))
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn settle_aborted_charges_when_upstream_tokens_consumed() {
    let w = world(Setup {
        guaranteed: 1000,
        ..Default::default()
    })
    .await;
    let g = guard(&w).await;
    // ollama:llama3.2 at 50/50: 5000 in -> 100, 3000 out -> 60 = 160 credits charged.
    g.settle_aborted(snap(Some("ollama"), Some((5000, 3000)), "partial", false))
        .await
        .unwrap();
    assert_eq!(counter(&w, user_key(&w)).await, Some(160));

    // No usage reported: charged from a local count of prompt + partial response text.
    let w = world(Setup {
        guaranteed: 1000,
        ..Default::default()
    })
    .await;
    let g = guard(&w).await;
    g.settle_aborted(snap(Some("ollama"), None, "some partial text", false))
        .await
        .unwrap();
    let billed = counter(&w, user_key(&w)).await.unwrap();
    assert!(
        billed > 0,
        "consumed tokens must be charged, never refunded"
    );
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn settle_aborted_refunds_only_zero_usage_cache_hit_blocked_before_dispatch() {
    for s in [
        snap(None, None, "", false), // blocked before dispatch
        snap(Some("cache"), Some((10, 10)), "hit", false), // provider "cache"
        snap(Some("ollama"), Some((10, 10)), "hit", true), // cache hit flag
        snap(Some("ollama"), None, "", false), // zero-output error
    ] {
        let w = world(Setup {
            guaranteed: 1000,
            ..Default::default()
        })
        .await;
        let g = guard(&w).await;
        assert_eq!(counter(&w, user_key(&w)).await, Some(6));
        g.settle_aborted(s).await.unwrap();
        assert_eq!(counter(&w, user_key(&w)).await, Some(0), "fully refunded");
    }
    // release() is the same zero-usage refund
    let w = world(Setup {
        guaranteed: 1000,
        ..Default::default()
    })
    .await;
    guard(&w).await.release().await.unwrap();
    assert_eq!(counter(&w, user_key(&w)).await, Some(0));
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn settle_aborted_estimate_failure_never_refunds_consumed() {
    // Pinned (ported from Python): when the abort-time credit calculation FAILS but upstream
    // tokens were consumed, the reservation is CHARGED as the actual (credits_reserved = 6),
    // never refunded -- a refund of consumed spend is a quota bypass.
    let w = world(Setup {
        guaranteed: 1000,
        broken_counting: true,
        ..Default::default()
    })
    .await;
    let g = guard(&w).await;
    g.settle_aborted(snap(Some("ollama"), None, "partial", false))
        .await
        .unwrap();
    assert_eq!(
        counter(&w, user_key(&w)).await,
        Some(6),
        "charged, not refunded"
    );
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn overdraw_regression_gh_347_ported_as_is() {
    // regression: gh-347 -- documented CURRENT behavior, deliberately NOT fixed in the port:
    // reserve takes the MAX conversion rate across seeded rows for the model, but a reconcile
    // on unmatched provider traffic bills the heuristic (in + 2*out)//10, which can exceed
    // the held reservation. llama3.2, 5000 in / 3000 out: held 160, billed 1100.
    let rates = ConversionRates::defaults();
    assert_eq!(rates.estimate_credits("llama3.2", 5000, 3000), 160);
    let w = world(Setup {
        guaranteed: 100_000,
        ..Default::default()
    })
    .await;
    // 20000 chars -> 5000 est. input tokens; the reserve estimate itself is the seeded max.
    let big = vec![json!({"content": "y".repeat(20_000)})];
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &big)
        .await
        .reservation
        .unwrap();
    assert_eq!(res.credits_reserved, 100 + 200);
    // Unmatched provider -> heuristic (5000 + 3000*2)//10 = 1100, far above the 300 held.
    let credits = TokenCredits::new(ConversionRates::defaults())
        .calculate_waddleai_tokens(5000, 3000, "unseeded-provider", "llama3.2")
        .unwrap();
    assert_eq!(credits, 1100);
    let r = w
        .engine
        .reconcile(&res, credits as i64, "unseeded-provider", "llama3.2", false)
        .await;
    assert!(r.applied);
    assert_eq!(
        r.from_reservation, 1100,
        "billed more than was held: overdraw, as in Python"
    );
    assert_eq!(counter(&w, user_key(&w)).await, Some(1100));
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn org_zero_is_not_treated_as_absent_gh_347_followup() {
    // regression: gh-347 -- org id 0 (the Python gRPC fall-through id) is a REAL id to the
    // engine, exactly as in Python (`org_id is None` is the only absence check): a configured
    // org 0 reserves normally against org-0 config, with no special-casing and no rejection.
    let w = world(Setup {
        guaranteed: 100,
        org_zero: true,
        ..Default::default()
    })
    .await;
    assert_eq!(w.user.org_id, 0);
    let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
    let res = out
        .reservation
        .expect("org 0 is admitted like any other org");
    assert_eq!(res.org_id, 0);
    assert_eq!(counter(&w, user_key(&w)).await, Some(6));
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn noop_paths_write_nothing() {
    for s in [
        Setup {
            flag_on: false,
            ..Default::default()
        },
        Setup {
            team_id: false,
            ..Default::default()
        },
        Setup {
            config_fails: true,
            ..Default::default()
        },
    ] {
        let w = world(Setup {
            guaranteed: 100,
            ..s
        })
        .await;
        let out = w.engine.reserve(&w.user, Some("llama3.2"), &msgs()).await;
        assert!(!out.blocked && out.reservation.is_none());
        assert_eq!(counter(&w, user_key(&w)).await, None);
    }
    let w = world(Setup {
        guaranteed: 100,
        ..Default::default()
    })
    .await;
    // unconfigured / disabled / future-anchor org and mismatched team all no-op
    let cfgs = [
        (None, true),
        (Some((false, Some(anchor()))), true),
        (Some((true, None)), true),
        (Some((true, NaiveDate::from_ymd_opt(2040, 1, 1))), true),
    ];
    for (org, _) in cfgs {
        let conn = w.conn.clone();
        let cfg = MemConfig {
            org: Mutex::new(org.map(|(enabled, a)| OrgQuotaConfig {
                org_id: w.org,
                enabled,
                cycle_anchor_date: a,
                pool_credits: 0,
                overrides: vec![],
            })),
            team: Mutex::new(None),
            fail: false,
        };
        let e = QuotaEngine::new(
            Arc::new(QuotaLimiter::new(conn)),
            Arc::new(cfg),
            Arc::new(MemLedger::default()),
            Arc::new(TokenCredits::new(ConversionRates::defaults())),
            Arc::new(StaticFlagResolver::new([POOLED_QUOTA_FLAG])),
        )
        .with_today(Arc::new(today));
        let out = e.reserve(&w.user, Some("llama3.2"), &msgs()).await;
        assert!(!out.blocked && out.reservation.is_none());
    }
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn ledger_failure_does_not_fail_reconcile() {
    let w = world(Setup {
        guaranteed: 100,
        ledger_fails: true,
        ..Default::default()
    })
    .await;
    let res = w
        .engine
        .reserve(&w.user, Some("llama3.2"), &msgs())
        .await
        .reservation
        .unwrap();
    let r = w
        .engine
        .reconcile(&res, 5, "ollama", "llama3.2", false)
        .await;
    assert!(r.applied, "ledger is reporting, not enforcement");
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn ttl_sweeper_expires_orphan_reservations() {
    // A limiter with a clock in the distant past indexes the reservation as already expired.
    let c = conn().await;
    let limiter = QuotaLimiter::with_clock(c.clone(), Arc::new(|| 1.0));
    let (user, team) = (uniq(), uniq());
    let d = limiter
        .reserve(&ReserveRequest {
            user_id: user,
            team_id: team,
            cycle: 0,
            day: "20310310".into(),
            credits: 5,
            guaranteed: 100,
            buffer_cap: 0,
            daily_subcap: None,
            overrides: vec![],
        })
        .await;
    assert!(d.allowed && !d.degraded);
    let rid = d.reservation_id.unwrap();
    let mut cc = c.clone();
    assert_eq!(
        cc.get::<_, Option<i64>>(cache::keys::quota::user_counter(user, 0))
            .await
            .unwrap(),
        Some(5)
    );
    let swept = QuotaLimiter::new(c.clone()).sweep_expired(None, 1000).await;
    assert!(swept >= 1, "orphan swept");
    assert_eq!(
        cc.get::<_, Option<i64>>(cache::keys::quota::user_counter(user, 0))
            .await
            .unwrap(),
        Some(0)
    );
    let gone: bool = cc
        .exists(cache::keys::quota::reservation(&rid))
        .await
        .unwrap();
    assert!(!gone);
    // an unexpired reservation is NOT swept at an earlier `now`
    let d = QuotaLimiter::new(c.clone())
        .reserve(&ReserveRequest {
            user_id: user,
            team_id: team,
            cycle: 0,
            day: "20310310".into(),
            credits: 5,
            guaranteed: 100,
            buffer_cap: 0,
            daily_subcap: None,
            overrides: vec![],
        })
        .await;
    let rid2 = d.reservation_id.expect("admitted");
    QuotaLimiter::new(c).sweep_expired(Some(2.0), 1000).await;
    let still: bool = cc
        .exists(cache::keys::quota::reservation(&rid2))
        .await
        .unwrap();
    assert!(still, "an unexpired reservation is never swept");
    assert_eq!(
        cc.get::<_, Option<i64>>(cache::keys::quota::user_counter(user, 0))
            .await
            .unwrap(),
        Some(5)
    );
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn noscript_reloads_and_dead_valkey_fails_open() {
    let c = conn().await;
    let limiter = QuotaLimiter::new(c.clone());
    let req = ReserveRequest {
        user_id: uniq(),
        team_id: uniq(),
        cycle: 0,
        day: "20310310".into(),
        credits: 2,
        guaranteed: 10,
        buffer_cap: 0,
        daily_subcap: None,
        overrides: vec![],
    };
    assert!(limiter.reserve(&req).await.allowed);
    let mut cc = c.clone();
    let _: () = redis::cmd("SCRIPT")
        .arg("FLUSH")
        .query_async(&mut cc)
        .await
        .unwrap();
    let d = limiter.reserve(&req).await;
    assert!(
        d.allowed && !d.degraded && d.reservation_id.is_some(),
        "NOSCRIPT reloaded"
    );

    // Kill the limiter's own connection: every call must fail OPEN (D10), flagged degraded.
    let victim = conn().await;
    let mut v = victim.clone();
    let id: i64 = redis::cmd("CLIENT")
        .arg("ID")
        .query_async(&mut v)
        .await
        .unwrap();
    let dead = QuotaLimiter::new(victim);
    let _: i64 = redis::cmd("CLIENT")
        .arg("KILL")
        .arg("ID")
        .arg(id)
        .query_async(&mut cc)
        .await
        .unwrap();
    let d = dead.reserve(&req).await;
    assert!(d.allowed && d.degraded && d.reservation_id.is_none());
    assert!(!dead.reconcile("nope", 1, &[]).await.applied);
    assert_eq!(dead.sweep_expired(None, 10).await, 0);
}

fn py(script: &str, args: &[&str]) -> String {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let url = std::env::var("WADDLEAI_TEST_VALKEY_URL").expect("WADDLEAI_TEST_VALKEY_URL");
    let prelude = format!(
        "import sys, asyncio, json; sys.path.insert(0, {root:?}); import redis.asyncio as r; \
         from shared.quota.lease import QuotaLimiter; c = r.Redis.from_url({url:?}); l = QuotaLimiter(c)\n"
    );
    let out = Command::new("python3")
        .arg("-I")
        .arg("-c")
        .arg(format!("{prelude}{script}"))
        .args(args)
        .output()
        .expect("python3 must be available for the cross-language parity test");
    assert!(
        out.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL + python3 with redis (CI provides both)"]
async fn rust_and_python_share_counters() {
    let (user, team) = (uniq(), uniq());
    let rust = QuotaLimiter::new(conn().await);
    let mut c = conn().await;
    let req = |credits| ReserveRequest {
        user_id: user,
        team_id: team,
        cycle: 0,
        day: "20310310".into(),
        credits,
        guaranteed: 4,
        buffer_cap: 100,
        daily_subcap: None,
        overrides: vec![],
    };

    // Python reserves, Rust reconciles.
    let out = py(
        "async def m():\n d = await l.reserve(user_id=int(sys.argv[1]), team_id=int(sys.argv[2]), cycle=0, day='20310310', credits=6, guaranteed=4, buffer_cap=100, daily_subcap=None, overrides=[])\n print(json.dumps([d.allowed, d.reservation_id, d.from_reservation, d.from_buffer]))\nasyncio.run(m())",
        &[&user.to_string(), &team.to_string()],
    );
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!((v[2].as_i64(), v[3].as_i64()), (Some(4), Some(2)));
    let rid = v[1].as_str().unwrap().to_owned();
    let r = rust.reconcile(&rid, 3, &[]).await;
    assert!(r.applied);
    assert_eq!((r.from_reservation, r.from_buffer), (3, 0));
    assert_eq!(
        c.get::<_, Option<i64>>(cache::keys::quota::user_counter(user, 0))
            .await
            .unwrap(),
        Some(3)
    );
    assert_eq!(
        c.get::<_, Option<i64>>(cache::keys::quota::buffer_counter(team, 0))
            .await
            .unwrap(),
        Some(0)
    );

    // Rust reserves, Python reconciles (same record, same counters).
    let d = rust.reserve(&req(5)).await;
    assert!(d.allowed);
    let rid = d.reservation_id.unwrap();
    let out = py(
        "async def m():\n x = await l.reconcile(sys.argv[1], 1)\n print(json.dumps([x.applied, x.from_reservation, x.from_buffer]))\nasyncio.run(m())",
        &[&rid],
    );
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(v[0], true);
    // Rust reserved 5 with headroom 1 (user 3 -> 4) + 4 from the buffer; Python's reconcile
    // to actual 1 refunds the buffer share first (4) and leaves the user's 1 credit: 4 / 0.
    let used = c
        .get::<_, Option<i64>>(cache::keys::quota::user_counter(user, 0))
        .await
        .unwrap();
    assert_eq!(used, Some(4));
    let buf = c
        .get::<_, Option<i64>>(cache::keys::quota::buffer_counter(team, 0))
        .await
        .unwrap();
    assert_eq!(buf, Some(0));
}
