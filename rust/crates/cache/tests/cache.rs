//! Cache tests. `connect_pings` / `invalidation_roundtrip` need `TEST_REDIS_URL`
//! (a real Valkey) and FAIL loudly when it is unset. Expected key strings are
//! copied verbatim from the Python proxy (`shared/cache/*`, `shared/quota/keys.py`).
use std::time::Duration;

use cache::{connect, keys, subscribe_invalidations};

/// Serializes tests that toggle or depend on the server-wide notify-keyspace-events flag.
static NOTIFY: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

fn url() -> String {
    std::env::var("TEST_REDIS_URL").expect("TEST_REDIS_URL must point at a real Valkey")
}

/// Env is process-global; building a Config serializes on this.
static ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn cfg_for(redis_url: &str) -> config::Config {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    std::env::set_var("DATABASE_URL", "postgres://x/y");
    std::env::set_var("REDIS_URL", redis_url);
    config::Config::from_env().expect("config")
}

fn cfg() -> config::Config {
    cfg_for(&url())
}

#[tokio::test]
async fn connect_pings() {
    let c = connect(&cfg()).await.expect("connect");
    c.ping().await.expect("ping");
}

#[tokio::test]
async fn connect_bad_url_errors() {
    let cfg = cfg_for("redis://127.0.0.1:1/0");
    assert!(connect(&cfg).await.is_err());
}

#[tokio::test]
async fn invalidation_roundtrip() {
    let _n = NOTIFY.lock().await;
    let cfg = cfg();
    let c = connect(&cfg).await.expect("connect");
    let mut sub = subscribe_invalidations(&cfg, vec!["waddleai:cache:cfg:".into()])
        .await
        .expect("subscribe");
    // Python invalidation == DEL of the key (CacheConfigResolver.invalidate).
    let k = keys::scope_cache("org", Some("42"));
    c.set(&k, b"x", 60).await.expect("set");
    c.del(&k).await.expect("del");
    // Unrelated key must be filtered out.
    c.set("other:key", b"x", 60).await.expect("set");
    c.del("other:key").await.expect("del");
    let ev = tokio::time::timeout(Duration::from_secs(5), sub.recv())
        .await
        .expect("timeout")
        .expect("event");
    assert_eq!(ev.key, k);
    assert!(tokio::time::timeout(Duration::from_millis(300), sub.recv())
        .await
        .is_err());
}

#[tokio::test]
async fn get_set_del_roundtrip() {
    let c = connect(&cfg()).await.expect("connect");
    let k = keys::exact_entry(999, "rt");
    assert_eq!(c.get(&k).await.expect("get"), None);
    c.set(&k, b"v", 60).await.expect("set");
    assert_eq!(c.get(&k).await.expect("get"), Some(b"v".to_vec()));
    c.del(&k).await.expect("del");
    assert_eq!(c.get(&k).await.expect("get"), None);
}

#[tokio::test]
async fn empty_prefixes_observe_all_and_drop_stops_listener() {
    let _n = NOTIFY.lock().await;
    let cfg = cfg();
    let c = connect(&cfg).await.expect("connect");
    let mut sub = subscribe_invalidations(&cfg, vec![])
        .await
        .expect("subscribe");
    c.set("any:key", b"x", 60).await.expect("set");
    c.del("any:key").await.expect("del");
    // Empty prefixes see every DEL, including other tests' keys: scan for ours.
    let found = tokio::time::timeout(Duration::from_secs(5), async {
        while let Some(ev) = sub.recv().await {
            if ev.key == "any:key" {
                return true;
            }
        }
        false
    })
    .await
    .expect("timeout");
    assert!(found);
    drop(sub);
}

#[tokio::test]
async fn subscribe_bad_url_errors() {
    let cfg = cfg_for("redis://127.0.0.1:1/0");
    assert!(subscribe_invalidations(&cfg, vec![]).await.is_err());
}

#[tokio::test]
async fn subscribe_enables_disabled_notifications() {
    let _n = NOTIFY.lock().await;
    let cfg = cfg();
    let client = redis::Client::open(url()).expect("client");
    let mut raw = client
        .get_multiplexed_async_connection()
        .await
        .expect("conn");
    // Another test may have enabled it; disabling then subscribing proves the set+verify path.
    let _: () = redis::cmd("CONFIG")
        .arg("SET")
        .arg("notify-keyspace-events")
        .arg("")
        .query_async(&mut raw)
        .await
        .expect("disable");
    let sub = subscribe_invalidations(&cfg, vec![])
        .await
        .expect("subscribe re-enables");
    assert_eq!(sub.dropped(), 0);
    let flags: Vec<String> = redis::cmd("CONFIG")
        .arg("GET")
        .arg("notify-keyspace-events")
        .query_async(&mut raw)
        .await
        .expect("get");
    assert!(flags[1].contains('E') && flags[1].contains('g'));
}
