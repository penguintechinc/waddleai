//! `TokenLimiter` against a LIVE Valkey. Ignored by default (needs infrastructure); run with
//! `WADDLEAI_TEST_VALKEY_URL=redis://127.0.0.1:6379 cargo test -p tokens -- --ignored`.
//! When explicitly run without the URL these tests FAIL rather than skip.

use std::sync::Arc;

use chrono::{TimeZone, Utc};
use pipeline::StaticFlagResolver;
use redis::AsyncCommands;
use tokens::{BudgetGate, KeyLimits, TokenLimiter, NATIVE_RATE_LIMIT_FLAG};

async fn conn() -> redis::aio::MultiplexedConnection {
    let url = std::env::var("WADDLEAI_TEST_VALKEY_URL")
        .expect("WADDLEAI_TEST_VALKEY_URL must be set for --ignored valkey tests");
    let client = redis::Client::open(url).expect("valid url");
    client
        .get_multiplexed_async_connection()
        .await
        .expect("valkey reachable")
}

fn limiter(c: redis::aio::MultiplexedConnection, flag_on: bool) -> TokenLimiter {
    let on: Vec<&str> = if flag_on {
        vec![NATIVE_RATE_LIMIT_FLAG]
    } else {
        vec![]
    };
    TokenLimiter::new(c, Arc::new(StaticFlagResolver::new(on)))
}

fn uniq_vkey() -> i64 {
    // Unique per test run so reruns never see stale counters.
    (uuid::Uuid::new_v4().as_u128() % 1_000_000_000) as i64 + 1_000_000
}

fn t0() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2031, 3, 4, 5, 6, 7).unwrap()
}

#[tokio::test]
#[ignore = "needs live Valkey (WADDLEAI_TEST_VALKEY_URL)"]
async fn tpm_denies_over_limit_and_allows_under() {
    let l = limiter(conn().await, true);
    let v = uniq_vkey();
    let limits = KeyLimits {
        tpm_limit: Some(100),
        ..Default::default()
    };
    let first = l.reserve_at(v, 60, 0.0, &limits, t0()).await;
    assert!(first.allowed);
    assert!(first.reservation_id.is_some());
    let second = l.reserve_at(v, 60, 0.0, &limits, t0()).await;
    assert!(!second.allowed);
    assert_eq!(second.reason.as_deref(), Some("tpm_exceeded"));
    assert_eq!(second.reservation_id, None);
}

#[tokio::test]
#[ignore = "needs live Valkey (WADDLEAI_TEST_VALKEY_URL)"]
async fn monthly_token_and_usd_limits_deny_with_reason() {
    let l = limiter(conn().await, true);
    let v = uniq_vkey();
    let tok = KeyLimits {
        monthly_token_limit: Some(10),
        ..Default::default()
    };
    assert_eq!(
        l.reserve_at(v, 11, 0.0, &tok, t0()).await.reason.as_deref(),
        Some("monthly_tokens_exceeded")
    );
    let v2 = uniq_vkey();
    let usd = KeyLimits {
        monthly_usd_limit: Some(5),
        ..Default::default()
    };
    assert_eq!(
        l.reserve_at(v2, 1, 6.0, &usd, t0()).await.reason.as_deref(),
        Some("monthly_usd_exceeded")
    );
}

#[tokio::test]
#[ignore = "needs live Valkey (WADDLEAI_TEST_VALKEY_URL)"]
async fn reconcile_adjusts_reserved_window_and_drops_reservation() {
    let c = conn().await;
    let l = limiter(c.clone(), true);
    let v = uniq_vkey();
    let limits = KeyLimits {
        tpm_limit: Some(1000),
        monthly_token_limit: Some(100000),
        ..Default::default()
    };
    let d = l.reserve_at(v, 100, 0.0, &limits, t0()).await;
    let rid = d.reservation_id.expect("allowed with reservation");
    let mut r = c.clone();
    let tpm_key = format!("waddleai:tpm:{v}:203103040506");
    let before: i64 = r.get(&tpm_key).await.unwrap();
    assert_eq!(before, 100);
    l.reconcile(&rid, 40, 0.0).await;
    let after: i64 = r.get(&tpm_key).await.unwrap();
    assert_eq!(after, 40);
    let gone: Option<String> = r.get(format!("waddleai:resv:{rid}")).await.unwrap();
    assert!(gone.is_none());
    // Reconciling an unknown reservation is a no-op, not an error.
    l.reconcile("does-not-exist", 1, 0.0).await;
    assert_eq!(l.fail_open_count("reconcile"), 0);
}

#[tokio::test]
#[ignore = "needs live Valkey (WADDLEAI_TEST_VALKEY_URL)"]
async fn flag_off_and_unlimited_allow_without_reservation() {
    let c = conn().await;
    let off = limiter(c.clone(), false);
    let limits = KeyLimits {
        tpm_limit: Some(1),
        ..Default::default()
    };
    let d = off.reserve(uniq_vkey(), 1_000_000, 0.0, &limits).await;
    assert!(d.allowed && d.reservation_id.is_none());
    let on = limiter(c, true);
    let d = on
        .reserve(uniq_vkey(), 1_000_000, 0.0, &KeyLimits::default())
        .await;
    assert!(d.allowed && d.reservation_id.is_none());
    assert_eq!(on.fail_open_count("reserve"), 0);
    assert_eq!(on.fail_open_count("nope"), 0);
}
