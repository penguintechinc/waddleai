//! Degraded-path behaviour: every backend fault is visible (log + metric) and never fails the
//! request; kill-switch, lease and semantic edge cases. No external services.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use common::*;
use pipeline::{CacheStatus, ResponseFormat, StaticFlagResolver};
use respcache::{
    CachedResponse, ConfigSource, ExactStore, LeaseStore, PutLimits, ResolvedCacheConfig,
    ResponseCache, SemanticCache, SemanticEntry, SemanticStore, SingleFlightConfig, StoreError,
    DISABLE_SINGLEFLIGHT_FLAG,
};
use serde_json::{json, Value};

struct FailingConfig;

#[async_trait]
impl ConfigSource for FailingConfig {
    async fn resolve(&self, _o: i64, _v: Option<i64>) -> Result<ResolvedCacheConfig, StoreError> {
        Err(StoreError("config db down".into()))
    }
}

#[derive(Default)]
struct FlakyExact {
    fail_get: AtomicBool,
    fail_put: AtomicBool,
    inner: MemExact,
}

#[async_trait]
impl ExactStore for FlakyExact {
    async fn get(&self, o: i64, k: &str) -> Result<Option<CachedResponse>, StoreError> {
        if self.fail_get.load(Ordering::SeqCst) {
            return Err(StoreError("valkey down".into()));
        }
        self.inner.get(o, k).await
    }
    async fn put(
        &self,
        o: i64,
        k: &str,
        v: &CachedResponse,
        l: PutLimits,
    ) -> Result<bool, StoreError> {
        if self.fail_put.load(Ordering::SeqCst) {
            return Err(StoreError("valkey down".into()));
        }
        self.inner.put(o, k, v, l).await
    }
}

fn cache_with(
    exact: Arc<dyn ExactStore>,
    m: Arc<telemetry::Metrics>,
    f: Arc<StaticFlagResolver>,
) -> ResponseCache {
    ResponseCache::new(
        exact,
        Arc::new(FixedConfig(ResolvedCacheConfig::default())),
        f,
        m,
    )
}

#[tokio::test]
async fn config_failure_skips_caching_visibly() {
    let m = metrics();
    let c = ResponseCache::new(
        Arc::new(MemExact::default()),
        Arc::new(FailingConfig),
        flags(),
        m.clone(),
    );
    let res = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert_eq!(res.status, CacheStatus::Miss);
    assert!(
        res.write_back.is_none(),
        "no write-back when config is unknown"
    );
    assert_eq!(lookups(&m, "response", "bypass"), 1.0);
}

#[tokio::test]
async fn exact_store_read_failure_degrades_with_bypass_metric_and_no_write_back() {
    let m = metrics();
    let ex = Arc::new(FlakyExact::default());
    ex.fail_get.store(true, Ordering::SeqCst);
    let c = cache_with(ex, m.clone(), flags());
    let res = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert_eq!(res.status, CacheStatus::Miss);
    assert!(res.write_back.is_none());
    assert_eq!(lookups(&m, "exact", "bypass"), 1.0);
}

#[tokio::test]
async fn exact_write_failure_is_swallowed_and_followers_are_still_released() {
    let m = metrics();
    let ex = Arc::new(FlakyExact::default());
    ex.fail_put.store(true, Ordering::SeqCst);
    let c = Arc::new(cache_with(ex.clone(), m.clone(), flags()));
    let first = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
    let leader = c.lookup(&first).await;
    let c2 = c.clone();
    let follower = tokio::spawn(async move {
        c2.lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
            .await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    leader
        .write_back
        .unwrap()
        .commit(&openai_body("answer"), &json!({}))
        .await; // put fails: logged, not raised
    let res = tokio::time::timeout(Duration::from_secs(2), follower)
        .await
        .unwrap()
        .unwrap();
    // the leader's value is handed to the follower in-process even though Valkey refused it
    assert_eq!(res.status, CacheStatus::Exact);
}

#[tokio::test]
async fn org_zero_is_never_cached() {
    let r = rig();
    let res = r
        .cache
        .lookup(&ctx(0, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert_eq!(res.status, CacheStatus::Miss);
    assert!(res.write_back.is_none());
    assert_eq!(r.exact.gets.load(Ordering::SeqCst), 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn killswitch_on_makes_every_miss_its_own_leader() {
    let m = metrics();
    let on = Arc::new(StaticFlagResolver::new([DISABLE_SINGLEFLIGHT_FLAG]));
    let c = cache_with(Arc::new(MemExact::default()), m.clone(), on);
    let a = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    let b = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert!(
        a.write_back.is_some() && b.write_back.is_some(),
        "both dispatch"
    );
    assert_eq!(lookups(&m, "exact", "bypass"), 2.0);
}

struct FakeLease {
    refuse: bool,
    fail: bool,
    held: Mutex<Vec<String>>,
}

#[async_trait]
impl LeaseStore for FakeLease {
    async fn try_acquire(&self, key: &str, _t: Duration, _tok: &str) -> Result<bool, StoreError> {
        if self.fail {
            return Err(StoreError("valkey down".into()));
        }
        if !self.refuse {
            self.held.lock().unwrap().push(key.to_owned());
        }
        Ok(!self.refuse)
    }
    async fn release(&self, key: &str, _tok: &str) {
        self.held.lock().unwrap().retain(|k| k != key);
    }
}

#[tokio::test]
async fn lease_denied_follower_finds_peer_value_by_polling() {
    let m = metrics();
    let ex = Arc::new(MemExact::default());
    let lease = Arc::new(FakeLease {
        refuse: true,
        fail: false,
        held: Mutex::default(),
    });
    let c = cache_with(ex.clone(), m.clone(), flags())
        .with_lease(lease)
        .with_flight_config(SingleFlightConfig {
            wait_timeout: Duration::from_secs(2),
            poll_interval: Duration::from_millis(10),
            ..SingleFlightConfig::default()
        });
    let probe = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
    // a peer process writes the value while this lookup polls
    let key = {
        let miss = ResponseCache::new(
            Arc::new(MemExact::default()),
            Arc::new(FixedConfig(ResolvedCacheConfig::default())),
            flags(),
            metrics(),
        )
        .lookup(&probe)
        .await;
        miss.write_back.unwrap().key
    };
    let ex2 = ex.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(60)).await;
        let body = openai_body("peer answer");
        ex2.put(
            7,
            &key,
            &CachedResponse {
                usage: body["usage"].clone(),
                response: body,
                stored_at: 0.0,
            },
            PutLimits {
                ttl_secs: 60,
                max_entry_kb: 256,
                org_quota_kb: 1024,
            },
        )
        .await
        .unwrap();
    });
    let res = c.lookup(&probe).await;
    assert_eq!(res.status, CacheStatus::Exact);
    assert_eq!(lookups(&m, "exact", "stampede_wait"), 1.0);
}

#[tokio::test]
async fn lease_failure_and_grant_both_still_yield_a_write_back() {
    for (fail, refuse) in [(true, false), (false, false)] {
        let lease = Arc::new(FakeLease {
            refuse,
            fail,
            held: Mutex::default(),
        });
        let l2 = lease.clone();
        let c = cache_with(Arc::new(MemExact::default()), metrics(), flags()).with_lease(lease);
        let res = c
            .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
            .await;
        assert!(res.write_back.is_some());
        assert_eq!(
            l2.held.lock().unwrap().len(),
            usize::from(!fail),
            "lease held only when granted"
        );
        res.write_back
            .unwrap()
            .commit(&openai_body("x"), &json!({}))
            .await;
        assert!(
            l2.held.lock().unwrap().is_empty(),
            "lease released after commit"
        );
    }
}

struct FailingSemantic;

#[async_trait]
impl SemanticStore for FailingSemantic {
    async fn candidates(
        &self,
        _o: i64,
        _m: &str,
        _c: &str,
    ) -> Result<Vec<SemanticEntry>, StoreError> {
        Err(StoreError("pg down".into()))
    }
    async fn insert(
        &self,
        _o: i64,
        _m: &str,
        _c: &str,
        _e: &[f32],
        _r: &Value,
        _t: u64,
    ) -> Result<(), StoreError> {
        Err(StoreError("pg down".into()))
    }
    async fn increment_hit(&self, _id: i64) -> Result<(), StoreError> {
        Err(StoreError("pg down".into()))
    }
}

#[tokio::test]
async fn semantic_store_failure_is_a_visible_bypass_miss() {
    let m = metrics();
    let emb = FakeEmbedder::with(&[("What is Rust?", vec![1.0, 0.0])]);
    let sem = Arc::new(SemanticCache::new(
        Arc::new(FailingSemantic),
        emb,
        m.clone(),
    ));
    let cfg = ResolvedCacheConfig {
        exact_enabled: false,
        semantic_enabled: true,
        ..Default::default()
    };
    let c = ResponseCache::new(
        Arc::new(MemExact::default()),
        Arc::new(FixedConfig(cfg)),
        flags(),
        m.clone(),
    )
    .with_semantic(sem);
    let res = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert_eq!(res.status, CacheStatus::Miss);
    assert_eq!(lookups(&m, "semantic", "bypass"), 1.0);
    // commit with a failing insert must not raise
    res.write_back
        .expect("semantic write-back")
        .commit(&openai_body("x"), &json!({}))
        .await;
}

#[tokio::test]
async fn embedding_over_budget_is_a_visible_bypass() {
    struct Slow;
    #[async_trait]
    impl respcache::Embedder for Slow {
        async fn embed(&self, _t: &str) -> Result<Vec<f32>, respcache::EmbedError> {
            tokio::time::sleep(Duration::from_secs(5)).await;
            Ok(vec![1.0])
        }
    }
    struct Unhealthy;
    #[async_trait]
    impl respcache::Embedder for Unhealthy {
        async fn embed(&self, _t: &str) -> Result<Vec<f32>, respcache::EmbedError> {
            Ok(vec![1.0])
        }
        fn is_healthy(&self) -> bool {
            false
        }
    }
    let m = metrics();
    let store = Arc::new(MemSemantic::default());
    let slow = SemanticCache::new(store.clone(), Arc::new(Slow), m.clone())
        .with_embed_timeout(Duration::from_millis(30));
    assert!(slow.embed("q?").await.is_none());
    assert_eq!(lookups(&m, "semantic", "bypass"), 1.0);
    let sick = SemanticCache::new(store, Arc::new(Unhealthy), m.clone());
    assert!(sick.embed("q?").await.is_none());
    assert_eq!(lookups(&m, "semantic", "bypass"), 2.0);
}

#[tokio::test]
async fn semantic_entries_are_scoped_by_conversation_prefix_and_skip_memory_injection() {
    let emb = FakeEmbedder::with(&[("What is Rust?", vec![1.0, 0.0])]);
    let (r, store) = semantic_rig(emb);
    let one = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
    let miss = r.cache.lookup(&one).await;
    miss.write_back
        .unwrap()
        .commit(&openai_body("answer"), &json!({}))
        .await;
    assert_eq!(store.rows.lock().unwrap().len(), 1);

    // memory injection: messages differ from the client's body -> semantic skipped entirely
    let mut injected = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
    injected.messages.insert(
        0,
        json!({"role": "system", "content": "memory: likes Rust"}),
    );
    let res = r.cache.lookup(&injected).await;
    assert_eq!(res.status, CacheStatus::Miss);
    // multi-turn is ineligible
    let mut multi = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
    multi
        .messages
        .insert(0, json!({"role": "user", "content": "hi"}));
    multi.body["messages"] = json!(multi.messages);
    assert_eq!(r.cache.lookup(&multi).await.status, CacheStatus::Miss);
    // the original still hits semantically (exact layer also holds it; drop it to force semantic)
    r.exact.map.lock().unwrap().clear();
    assert_eq!(r.cache.lookup(&one).await.status, CacheStatus::Semantic);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn semantic_misses_collapse_in_process_too() {
    let emb = FakeEmbedder::with(&[("What is Rust?", vec![1.0, 0.0])]);
    let m = metrics();
    let store = Arc::new(MemSemantic::default());
    let sem = Arc::new(SemanticCache::new(store.clone(), emb.clone(), m.clone()));
    let cfg = ResolvedCacheConfig {
        exact_enabled: false,
        semantic_enabled: true,
        ..Default::default()
    };
    let c = Arc::new(
        ResponseCache::new(
            Arc::new(MemExact::default()),
            Arc::new(FixedConfig(cfg)),
            flags(),
            m.clone(),
        )
        .with_semantic(sem),
    );
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    for _ in 0..4 {
        let (c, tx) = (c.clone(), tx.clone());
        tokio::spawn(async move {
            let _ = tx.send(
                c.lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
                    .await,
            );
        });
    }
    drop(tx);
    let leader = rx.recv().await.unwrap();
    assert_eq!(leader.status, CacheStatus::Miss);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        rx.try_recv().is_err(),
        "followers wait on the semantic leader"
    );
    leader
        .write_back
        .unwrap()
        .commit(&openai_body("answer"), &json!({}))
        .await;
    let mut hits = 0;
    while let Some(r) = rx.recv().await {
        assert_eq!(r.status, CacheStatus::Semantic);
        hits += 1;
    }
    assert_eq!(hits, 3);
    assert_eq!(
        store.rows.lock().unwrap().len(),
        1,
        "one embedding write, not four"
    );
    assert_eq!(lookups(&m, "semantic", "stampede_wait"), 3.0);
}

#[test]
fn store_error_wraps_driver_errors_without_content() {
    let e: StoreError = redis::RedisError::from((redis::ErrorKind::Io, "conn refused")).into();
    assert!(e.0.contains("conn refused"));
    let d: StoreError = sea_orm::DbErr::Custom("pool closed".into()).into();
    assert!(d.0.contains("pool closed"));
    assert_eq!(StoreError("x".into()).to_string(), "cache backend error: x");
}

#[tokio::test]
async fn different_concurrent_semantic_queries_never_receive_each_others_response() {
    // regression: security review -- Python's semantic flight key omits the query, so a follower
    // for a DIFFERENT question would be handed the leader's answer (cross-user leak).
    let emb = FakeEmbedder::with(&[
        ("What is Rust?", vec![1.0, 0.0]),
        ("What is Go?", vec![0.0, 1.0]),
    ]);
    let m = metrics();
    let store = Arc::new(MemSemantic::default());
    let sem = Arc::new(SemanticCache::new(store.clone(), emb, m.clone()));
    let cfg = ResolvedCacheConfig {
        exact_enabled: false,
        semantic_enabled: true,
        ..Default::default()
    };
    let c = ResponseCache::new(
        Arc::new(MemExact::default()),
        Arc::new(FixedConfig(cfg)),
        flags(),
        m.clone(),
    )
    .with_semantic(sem);
    let a = c
        .lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?"))
        .await;
    assert_eq!(a.status, CacheStatus::Miss);
    let b = tokio::time::timeout(
        Duration::from_millis(500),
        c.lookup(&ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Go?")),
    )
    .await
    .expect("a different query must not wait on the Rust leader");
    assert_eq!(
        b.status,
        CacheStatus::Miss,
        "must not be served the other query's answer"
    );
    assert!(b.write_back.is_some(), "B is its own leader");
    a.write_back
        .unwrap()
        .commit(&openai_body("Rust answer"), &json!({}))
        .await;
    b.write_back
        .unwrap()
        .commit(&openai_body("Go answer"), &json!({}))
        .await;
    assert_eq!(lookups(&m, "semantic", "stampede_wait"), 0.0);
    let rows = store.rows.lock().unwrap().len();
    assert_eq!(rows, 2);
}

#[tokio::test]
async fn requests_with_unkeyed_shaping_fields_neither_read_nor_write_the_cache() {
    // regression: security review -- Python's key omits system/stop/seed/...; an otherwise
    // identical request lacking the field must not exchange entries with one carrying it.
    for (field, value) in [
        ("system", json!("You are a pirate")),
        ("stop", json!(["\n"])),
        ("seed", json!(7)),
        ("tool_choice", json!("none")),
        ("n", json!(3)),
    ] {
        let r = rig();
        let plain = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
        let seeded = r.cache.lookup(&plain).await;
        seeded
            .write_back
            .unwrap()
            .commit(&openai_body("plain answer"), &json!({}))
            .await;
        assert_eq!(r.exact.puts.load(Ordering::SeqCst), 1);
        let gets = r.exact.gets.load(Ordering::SeqCst);

        let mut shaped = ctx(7, "gpt-4o", ResponseFormat::Openai, "What is Rust?");
        shaped.body[field] = value;
        let res = r.cache.lookup(&shaped).await;
        assert_eq!(
            res.status,
            CacheStatus::Miss,
            "{field}: must not be served the plain entry"
        );
        assert!(res.write_back.is_none(), "{field}: must not be written");
        assert_eq!(
            r.exact.gets.load(Ordering::SeqCst),
            gets,
            "{field}: no read at all"
        );
        assert_eq!(r.exact.puts.load(Ordering::SeqCst), 1);
        // and the plain request is unaffected
        assert_eq!(r.cache.lookup(&plain).await.status, CacheStatus::Exact);
    }
}
