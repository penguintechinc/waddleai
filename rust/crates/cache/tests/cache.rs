//! Cache tests. `connect_pings` / `invalidation_roundtrip` need `TEST_REDIS_URL`
//! (a real Valkey) and FAIL loudly when it is unset. Expected key strings are
//! copied verbatim from the Python proxy (`shared/cache/*`, `shared/quota/keys.py`).
use std::time::Duration;

use cache::{connect, keys, subscribe_invalidations};

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

#[test]
fn key_formats_match_python() {
    // shared/cache/exact.py
    assert_eq!(keys::exact_entry(7, "abc"), "waddleai:cache:exact:7:abc");
    assert_eq!(keys::exact_idx(7), "waddleai:cache:idx:7");
    assert_eq!(keys::exact_bytes(7), "waddleai:cache:bytes:7");
    // shared/cache/response_cache.py + singleflight.py
    assert_eq!(keys::exact_lease(7, "abc"), "waddleai:cache:sf:exact:7:abc");
    assert_eq!(
        keys::semantic_lease(7, "gpt-4o", "h1"),
        "waddleai:cache:sf:semantic:7:gpt-4o:h1"
    );
    assert_eq!(
        keys::lock("waddleai:cache:sf:exact:7:abc"),
        "waddleai:cache:sf:exact:7:abc:lock"
    );
    // shared/cache/config.py
    assert_eq!(
        keys::scope_cache("org", Some("5")),
        "waddleai:cache:cfg:org:5"
    );
    assert_eq!(
        keys::scope_cache("global", None),
        "waddleai:cache:cfg:global:-"
    );
    // shared/cache/affinity.py, upstream.py
    assert_eq!(keys::affinity(7, "sess"), "waddleai:affinity:7:sess");
    assert_eq!(
        keys::gemini_mapping(3, "sha"),
        "waddleai:cache:gemini:3:sha"
    );
    assert_eq!(
        keys::prefix_counter(3, "sha"),
        "waddleai:cache:prefix:3:sha"
    );
    // shared/auth/rbac.py
    assert_eq!(keys::auth_apikey("kid"), "waddleai:auth:apikey:kid");
    // shared/quota/keys.py
    assert_eq!(keys::quota::user_counter(1, 2), "wa:quota:user:1:2");
    assert_eq!(
        keys::quota::buffer_counter(1, 2),
        "wa:quota:team_buffer:1:2"
    );
    assert_eq!(keys::quota::override_counter(1, 2), "wa:quota:override:1:2");
    assert_eq!(
        keys::quota::daily_counter(1, "20261028"),
        "wa:quota:userday:1:20261028"
    );
    assert_eq!(keys::quota::reservation("r1"), "wa:quota:resv:r1");
    assert_eq!(keys::quota::org_config(9), "wa:quota:cfg:org:9");
    assert_eq!(keys::quota::team_config(9), "wa:quota:cfg:team:9");
    assert_eq!(keys::quota::RESERVATION_INDEX, "wa:quota:resv_idx");
    assert_eq!(keys::quota::PREFIX, "wa:quota");
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
    let cfg = cfg();
    let c = connect(&cfg).await.expect("connect");
    let mut sub = subscribe_invalidations(&cfg, vec![])
        .await
        .expect("subscribe");
    c.set("any:key", b"x", 60).await.expect("set");
    c.del("any:key").await.expect("del");
    let ev = tokio::time::timeout(Duration::from_secs(5), sub.recv())
        .await
        .expect("timeout")
        .expect("event");
    assert_eq!(ev.key, "any:key");
    drop(sub);
}

#[tokio::test]
async fn subscribe_bad_url_errors() {
    let cfg = cfg_for("redis://127.0.0.1:1/0");
    assert!(subscribe_invalidations(&cfg, vec![]).await.is_err());
}
