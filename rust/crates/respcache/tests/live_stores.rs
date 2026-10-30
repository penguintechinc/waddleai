//! Live-infrastructure checks (all `#[ignore]`; CI provides the services):
//! `WADDLEAI_TEST_VALKEY_URL`, `WADDLEAI_TEST_DATABASE_URL` (Postgres with `response_cache_entries`
//! and `cache_configs`), `WADDLEAI_TEST_OLLAMA_URL`. Run with `-- --ignored`; a missing variable
//! FAILS the test rather than skipping it.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use common::{ctx, flags, metrics, openai_body};
use pipeline::{CacheStatus, ResponseFormat};
use respcache::{
    CacheConfigResolver, CacheConfigRow, ConfigRowStore, ConfigSource, EmbeddingClient,
    PgSemanticStore, ResolvedCacheConfig, ResponseCache, SeaConfigStore, SemanticCache,
    SemanticStore, StoreError, ValkeyExact, ValkeyLease,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, DbBackend, Statement};
use serde_json::json;

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| panic!("{name} must be set for --ignored live tests"))
}

async fn pg() -> DatabaseConnection {
    Database::connect(env("WADDLEAI_TEST_DATABASE_URL"))
        .await
        .expect("postgres reachable")
}

async fn valkey() -> redis::aio::MultiplexedConnection {
    redis::Client::open(env("WADDLEAI_TEST_VALKEY_URL"))
        .expect("valid url")
        .get_multiplexed_async_connection()
        .await
        .expect("valkey reachable")
}

fn uniq() -> i32 {
    (uuid::Uuid::new_v4().as_u128() % 1_000_000_000) as i32 + 1_000
}

#[tokio::test]
#[ignore = "needs live Postgres (WADDLEAI_TEST_DATABASE_URL)"]
async fn pg_semantic_store_round_trip_respects_scope_and_expiry() {
    let db = pg().await;
    let store = PgSemanticStore::new(db.clone());
    let org = i64::from(uniq());
    let emb = [0.25f32, -0.5, 1.0];
    let resp = json!({"id": "x", "usage": {"total_tokens": 4}});
    store
        .insert(org, "m::openai", "ctx1", &emb, &resp, 3600)
        .await
        .unwrap();
    store
        .insert(org, "m::openai", "ctx1", &emb, &resp, 0)
        .await
        .unwrap(); // already expired
    store
        .insert(org + 1, "m::openai", "ctx1", &emb, &resp, 3600)
        .await
        .unwrap(); // other org
    store
        .insert(org, "m::anthropic", "ctx1", &emb, &resp, 3600)
        .await
        .unwrap(); // other format
    let got = store.candidates(org, "m::openai", "ctx1").await.unwrap();
    assert_eq!(
        got.len(),
        1,
        "expired row, other org and other format excluded"
    );
    assert_eq!(got[0].embedding, emb.to_vec());
    assert_eq!(got[0].response, resp);
    store.increment_hit(got[0].id).await.unwrap();
    store.increment_hit(got[0].id).await.unwrap();
    let row = db
        .query_one_raw(Statement::from_string(
            DbBackend::Postgres,
            format!(
                "SELECT hit_count FROM response_cache_entries WHERE id = {}",
                got[0].id
            ),
        ))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row.try_get::<i32>("", "hit_count").unwrap(), 2);
    assert!(store
        .candidates(org, "m::openai", "other-ctx")
        .await
        .unwrap()
        .is_empty());
}

struct Counting(Arc<dyn ConfigRowStore>, Arc<AtomicUsize>);

#[async_trait]
impl ConfigRowStore for Counting {
    async fn fetch(&self, o: i64, v: Option<i64>) -> Result<Vec<CacheConfigRow>, StoreError> {
        self.1.fetch_add(1, Ordering::SeqCst);
        self.0.fetch(o, v).await
    }
}

#[tokio::test]
#[ignore = "needs live Postgres + Valkey"]
async fn config_resolver_merges_scopes_and_caches_negative_entries() {
    let db = pg().await;
    let org = uniq();
    for sql in [
        "DELETE FROM cache_configs WHERE scope_type = 'global'".to_owned(),
        "INSERT INTO cache_configs (scope_type, scope_ref, ttl_seconds, semantic_enabled) VALUES ('global', NULL, 111, false)".to_owned(),
        format!("INSERT INTO cache_configs (scope_type, scope_ref, semantic_enabled, max_entry_kb) VALUES ('org', '{org}', true, 8)"),
    ] {
        db.execute_raw(Statement::from_string(DbBackend::Postgres, sql)).await.unwrap();
    }
    let fetches = Arc::new(AtomicUsize::new(0));
    let rows: Arc<dyn ConfigRowStore> =
        Arc::new(Counting(Arc::new(SeaConfigStore::new(db)), fetches.clone()));
    let r = CacheConfigResolver::new(rows, Some(valkey().await));
    let want = |c: &ResolvedCacheConfig| (c.ttl_seconds, c.semantic_enabled, c.max_entry_kb);
    // The `global` row is shared across test runs, so assert on the org-scoped fields exactly and
    // on the global ttl only as "whatever global says".
    let c1 = r.resolve(i64::from(org), Some(424_242)).await.unwrap();
    assert!(
        c1.semantic_enabled && c1.max_entry_kb == 8,
        "{:?}",
        want(&c1)
    );
    assert_eq!(
        fetches.load(Ordering::SeqCst),
        1,
        "one combined read for all scopes"
    );
    // second resolve is Valkey-only (the missing key scope was negative-cached as null)
    let c2 = r.resolve(i64::from(org), Some(424_242)).await.unwrap();
    assert_eq!(want(&c1), want(&c2));
    assert_eq!(
        fetches.load(Ordering::SeqCst),
        1,
        "negative cache hit; no DB read"
    );
    r.invalidate("org", Some(&org.to_string())).await.unwrap();
    r.resolve(i64::from(org), Some(424_242)).await.unwrap();
    assert_eq!(
        fetches.load(Ordering::SeqCst),
        2,
        "invalidate forces a re-read"
    );
}

/// Everything real: Valkey exact + lease, Postgres semantic, Ollama embeddings, wired through the
/// `ResponseCache` facade (semantic-only so the semantic path is what answers).
#[tokio::test]
#[ignore = "needs live Valkey + Postgres + Ollama"]
async fn full_stack_semantic_hit_and_miss_with_real_services() {
    let m = metrics();
    let v = valkey().await;
    let exact = Arc::new(ValkeyExact::new(v.clone(), m.clone()));
    let embedder = Arc::new(
        EmbeddingClient::new(
            &env("WADDLEAI_TEST_OLLAMA_URL"),
            respcache::DEFAULT_EMBEDDING_MODEL,
            "chat_ollama",
            Some(m.clone()),
        )
        .unwrap(),
    );
    let semantic = Arc::new(SemanticCache::new(
        Arc::new(PgSemanticStore::new(pg().await)),
        embedder,
        m.clone(),
    ));
    let cfg = ResolvedCacheConfig {
        exact_enabled: false,
        semantic_enabled: true,
        semantic_threshold: 0.95,
        ..ResolvedCacheConfig::default()
    };
    let cache = ResponseCache::new(
        exact,
        Arc::new(common::FixedConfig(cfg)),
        flags(),
        m.clone(),
    )
    .with_semantic(semantic)
    .with_lease(Arc::new(ValkeyLease::new(v)));
    let org = uniq();
    let text = format!("What is the tallest mountain on Earth? ({org})");
    let c = ctx(org, "gpt-4o", ResponseFormat::Openai, &text);
    let miss = cache.lookup(&c).await;
    assert_eq!(miss.status, CacheStatus::Miss);
    miss.write_back
        .expect("semantic write-back")
        .commit(&openai_body("Mount Everest."), &json!({}))
        .await;
    let hit = cache.lookup(&c).await;
    assert_eq!(
        hit.status,
        CacheStatus::Semantic,
        "identical prompt embeds identically"
    );
    let other = ctx(
        org,
        "gpt-4o",
        ResponseFormat::Openai,
        "How do I bake sourdough bread at home?",
    );
    assert_eq!(cache.lookup(&other).await.status, CacheStatus::Miss);
    // the other wire format never sees it
    let anth = ctx(org, "gpt-4o", ResponseFormat::Anthropic, &text);
    assert_eq!(cache.lookup(&anth).await.status, CacheStatus::Miss);
}
