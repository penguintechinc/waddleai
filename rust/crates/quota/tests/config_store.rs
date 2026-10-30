//! Config JSON codec (Python wire format, no infrastructure) and the Valkey read-through
//! store (`#[ignore]`, needs WADDLEAI_TEST_VALKEY_URL; CI provides Valkey).

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use chrono::NaiveDate;
use quota::{
    org_config_from_json, org_config_to_json, team_config_from_json, team_config_to_json,
    ConfigError, ConfigLoader, ConfigStore, OrgQuotaConfig, OverrideRule, TeamQuotaConfig,
    UserReservation, ValkeyConfigStore,
};
use redis::AsyncCommands;

/// Exactly what Python `json.dumps` emits for an org config (note the `", "` separators).
const PY_ORG: &str = r#"{"org_id": 9, "enabled": true, "anchor": "2031-03-01", "pool": 500, "overrides": [{"id": 4, "cap": 70, "user_id": null, "model": "m", "provider": null}]}"#;
const PY_TEAM: &str = r#"{"team_id": 3, "org_id": 9, "pool": 100, "buffer": 40, "reservations": [{"user_id": 7, "guaranteed": 25, "daily": null}]}"#;

fn org() -> OrgQuotaConfig {
    OrgQuotaConfig {
        org_id: 9,
        enabled: true,
        cycle_anchor_date: NaiveDate::from_ymd_opt(2031, 3, 1),
        pool_credits: 500,
        overrides: vec![OverrideRule {
            id: 4,
            cap_credits: 70,
            user_id: None,
            model: Some("m".into()),
            provider: None,
        }],
    }
}

fn team() -> TeamQuotaConfig {
    let mut reservations = HashMap::new();
    reservations.insert(
        7,
        UserReservation {
            user_id: 7,
            guaranteed_credits: 25,
            daily_subcap: None,
        },
    );
    TeamQuotaConfig {
        team_id: 3,
        org_id: 9,
        pool_credits: 100,
        shared_buffer_credits: 40,
        reservations,
    }
}

#[test]
fn decodes_python_written_json_and_roundtrips() {
    assert_eq!(
        org_config_from_json(PY_ORG.as_bytes()).unwrap(),
        Some(org())
    );
    assert_eq!(
        team_config_from_json(PY_TEAM.as_bytes()).unwrap(),
        Some(team())
    );
    assert_eq!(
        org_config_from_json(org_config_to_json(&org()).as_bytes()).unwrap(),
        Some(org())
    );
    assert_eq!(
        team_config_from_json(team_config_to_json(&team()).as_bytes()).unwrap(),
        Some(team())
    );
    // the encoder keeps Python's field names so Python can read what Rust caches
    let v: serde_json::Value = serde_json::from_str(&org_config_to_json(&org())).unwrap();
    assert_eq!(v["anchor"], "2031-03-01");
    assert_eq!(v["overrides"][0]["cap"], 70);
}

#[test]
fn null_decodes_to_none_and_garbage_errors() {
    assert_eq!(org_config_from_json(b"null").unwrap(), None);
    assert_eq!(team_config_from_json(b"null").unwrap(), None);
    for bad in [
        &b"{"[..],
        b"[]",
        br#"{"org_id": 1}"#,
        br#"{"org_id":1,"enabled":true,"anchor":"nope","pool":1,"overrides":[]}"#,
    ] {
        assert!(org_config_from_json(bad).is_err());
    }
    for bad in [
        &b"{"[..],
        br#"{"team_id": 1}"#,
        br#"{"team_id":1,"org_id":1,"pool":1,"buffer":1,"reservations":[{"user_id":1}]}"#,
    ] {
        assert!(team_config_from_json(bad).is_err());
    }
    // an unanchored org (anchor null) decodes with no anchor
    let unanchored = org_config_from_json(
        br#"{"org_id":1,"enabled":false,"anchor":null,"pool":0,"overrides":[]}"#,
    )
    .unwrap()
    .unwrap();
    assert!(unanchored.cycle_anchor_date.is_none() && !unanchored.enabled);
}

struct CountingLoader {
    org_calls: AtomicUsize,
    fail: bool,
    present: bool,
}

#[async_trait::async_trait]
impl ConfigLoader for CountingLoader {
    async fn load_org(&self, _o: i64) -> Result<Option<OrgQuotaConfig>, ConfigError> {
        self.org_calls.fetch_add(1, Ordering::SeqCst);
        if self.fail {
            return Err(ConfigError("db down".into()));
        }
        Ok(self.present.then(org))
    }
    async fn load_team(&self, _t: i64) -> Result<Option<TeamQuotaConfig>, ConfigError> {
        Ok(self.present.then(team))
    }
}

async fn store(
    loader: Arc<CountingLoader>,
) -> (ValkeyConfigStore, redis::aio::MultiplexedConnection) {
    let url =
        std::env::var("WADDLEAI_TEST_VALKEY_URL").expect("WADDLEAI_TEST_VALKEY_URL must be set");
    std::env::set_var("DATABASE_URL", "postgres://x/y");
    std::env::set_var("REDIS_URL", &url);
    let cfg = config::Config::from_env().expect("config");
    let cache = cache::connect(&cfg).await.expect("connect");
    let conn = redis::Client::open(url)
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    (ValkeyConfigStore::new(cache, loader), conn)
}

fn uniq() -> i64 {
    (uuid::Uuid::new_v4().as_u128() % 1_000_000_000) as i64 + 1_000_000
}

#[tokio::test]
#[ignore = "needs WADDLEAI_TEST_VALKEY_URL (CI provides Valkey)"]
async fn read_through_caches_hits_negative_caches_and_fails_open() {
    let loader = Arc::new(CountingLoader {
        org_calls: AtomicUsize::new(0),
        fail: false,
        present: true,
    });
    let (s, mut conn) = store(loader.clone()).await;
    let id = uniq();
    assert_eq!(s.get_org(id).await.unwrap(), Some(org()));
    assert_eq!(s.get_org(id).await.unwrap(), Some(org()));
    assert_eq!(
        loader.org_calls.load(Ordering::SeqCst),
        1,
        "second read is a cache hit"
    );
    let ttl: i64 = conn.ttl(cache::keys::quota::org_config(id)).await.unwrap();
    assert!(ttl > 0 && ttl <= 300);
    assert_eq!(s.get_team(id).await.unwrap(), Some(team()));

    // a Python-written entry is read as-is
    let other = uniq();
    let _: () = conn
        .set(cache::keys::quota::org_config(other), PY_ORG)
        .await
        .unwrap();
    assert_eq!(s.get_org(other).await.unwrap(), Some(org()));
    assert_eq!(loader.org_calls.load(Ordering::SeqCst), 1);

    // a malformed entry reloads from the loader and repairs the cache
    let _: () = conn
        .set(cache::keys::quota::org_config(other), "{garbage")
        .await
        .unwrap();
    assert_eq!(s.get_org(other).await.unwrap(), Some(org()));
    assert_eq!(loader.org_calls.load(Ordering::SeqCst), 2);

    // absent config is negative-cached with the short TTL
    let none = Arc::new(CountingLoader {
        org_calls: AtomicUsize::new(0),
        fail: false,
        present: false,
    });
    let (s2, mut conn2) = store(none.clone()).await;
    let nid = uniq();
    assert_eq!(s2.get_org(nid).await.unwrap(), None);
    assert_eq!(s2.get_org(nid).await.unwrap(), None);
    assert_eq!(none.org_calls.load(Ordering::SeqCst), 1);
    let raw: String = conn2
        .get(cache::keys::quota::org_config(nid))
        .await
        .unwrap();
    assert_eq!(raw, "null");
    let ttl: i64 = conn2
        .ttl(cache::keys::quota::org_config(nid))
        .await
        .unwrap();
    assert!(ttl > 0 && ttl <= 30);

    // a loader failure fails open as "unconfigured" and is not cached
    let bad = Arc::new(CountingLoader {
        org_calls: AtomicUsize::new(0),
        fail: true,
        present: true,
    });
    let (s3, mut conn3) = store(bad).await;
    let fid = uniq();
    assert_eq!(s3.get_org(fid).await.unwrap(), None);
    let exists: bool = conn3
        .exists(cache::keys::quota::org_config(fid))
        .await
        .unwrap();
    assert!(!exists);
}
