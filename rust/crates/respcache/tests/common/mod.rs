//! In-memory fakes for the cache stores so stage logic runs without Valkey/Postgres/Ollama.
#![allow(dead_code)]

use std::collections::{BTreeSet, HashMap};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use auth::{AuthMode as UserAuthMode, UserContext};
use pipeline::{PipelineContext, ResponseFormat, StaticFlagResolver};
use respcache::{
    AffinityStore, CachedResponse, ConfigSource, EmbedError, Embedder, ExactStore, PutLimits,
    ResolvedCacheConfig, ResponseCache, SemanticCache, SemanticEntry, SemanticStore, StoreError,
};
use serde_json::{json, Value};
use telemetry::Metrics;

/// Exact store backed by a map; counts puts so tests can prove when a write happened.
#[derive(Default)]
pub struct MemExact {
    pub map: Mutex<HashMap<(i64, String), CachedResponse>>,
    pub puts: AtomicUsize,
    pub gets: AtomicUsize,
}

#[async_trait]
impl ExactStore for MemExact {
    async fn get(&self, org_id: i64, key: &str) -> Result<Option<CachedResponse>, StoreError> {
        self.gets.fetch_add(1, Ordering::SeqCst);
        Ok(self
            .map
            .lock()
            .unwrap()
            .get(&(org_id, key.to_owned()))
            .cloned())
    }
    async fn put(
        &self,
        org_id: i64,
        key: &str,
        value: &CachedResponse,
        _limits: PutLimits,
    ) -> Result<bool, StoreError> {
        self.puts.fetch_add(1, Ordering::SeqCst);
        self.map
            .lock()
            .unwrap()
            .insert((org_id, key.to_owned()), value.clone());
        Ok(true)
    }
}

/// One stored row: (id, org, model_class, context_hash, embedding, response).
pub type Row = (i64, i64, String, String, Vec<f32>, Value);

/// Semantic store backed by a vec.
#[derive(Default)]
pub struct MemSemantic {
    pub rows: Mutex<Vec<Row>>,
    pub hits: Mutex<Vec<i64>>,
}

#[async_trait]
impl SemanticStore for MemSemantic {
    async fn candidates(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
    ) -> Result<Vec<SemanticEntry>, StoreError> {
        Ok(self
            .rows
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.1 == org_id && r.2 == model_class && r.3 == context_hash)
            .map(|r| SemanticEntry {
                id: r.0,
                embedding: r.4.clone(),
                response: r.5.clone(),
            })
            .collect())
    }
    async fn insert(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
        embedding: &[f32],
        response: &Value,
        _ttl_secs: u64,
    ) -> Result<(), StoreError> {
        let mut rows = self.rows.lock().unwrap();
        let id = rows.len() as i64 + 1;
        rows.push((
            id,
            org_id,
            model_class.into(),
            context_hash.into(),
            embedding.to_vec(),
            response.clone(),
        ));
        Ok(())
    }
    async fn increment_hit(&self, id: i64) -> Result<(), StoreError> {
        self.hits.lock().unwrap().push(id);
        Ok(())
    }
}

/// Embedder returning fixed vectors per text; can be switched to fail.
#[derive(Default)]
pub struct FakeEmbedder {
    pub vectors: Mutex<HashMap<String, Vec<f32>>>,
    pub fail: AtomicBool,
    pub calls: AtomicUsize,
}

impl FakeEmbedder {
    pub fn with(pairs: &[(&str, Vec<f32>)]) -> Arc<Self> {
        let e = Self::default();
        for (t, v) in pairs {
            e.vectors.lock().unwrap().insert((*t).into(), v.clone());
        }
        Arc::new(e)
    }
}

#[async_trait]
impl Embedder for FakeEmbedder {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.fail.load(Ordering::SeqCst) {
            return Err(EmbedError("embedder down".into()));
        }
        self.vectors
            .lock()
            .unwrap()
            .get(text)
            .cloned()
            .ok_or_else(|| EmbedError("no vector".into()))
    }
}

/// Affinity map backed by a hashmap.
#[derive(Default)]
pub struct MemAffinity(pub Mutex<HashMap<(i64, String), String>>);

#[async_trait]
impl AffinityStore for MemAffinity {
    async fn lookup(&self, org_id: i64, session_hash: &str) -> Option<String> {
        self.0
            .lock()
            .unwrap()
            .get(&(org_id, session_hash.to_owned()))
            .cloned()
    }
    async fn record(&self, org_id: i64, session_hash: &str, backend_id: &str) {
        self.0
            .lock()
            .unwrap()
            .insert((org_id, session_hash.to_owned()), backend_id.to_owned());
    }
}

/// Fixed config for every (org, key).
pub struct FixedConfig(pub ResolvedCacheConfig);

#[async_trait]
impl ConfigSource for FixedConfig {
    async fn resolve(
        &self,
        _org_id: i64,
        _vkey_id: Option<i64>,
    ) -> Result<ResolvedCacheConfig, StoreError> {
        Ok(self.0.clone())
    }
}

pub fn user(org: i32) -> UserContext {
    UserContext {
        org_id: org,
        user_id: 7,
        tenant: org.to_string(),
        scopes: BTreeSet::new(),
        teams: vec![],
        auth_mode: UserAuthMode::Jwt,
        api_key_id: Some(3),
        team_id: None,
        team_role: None,
        role: None,
    }
}

/// A single-turn, temperature-0 request context for `org`.
pub fn ctx(org: i32, model: &str, fmt: ResponseFormat, text: &str) -> PipelineContext {
    let messages = vec![json!({"role": "user", "content": text})];
    let body = json!({"model": model, "messages": messages, "temperature": 0.0});
    let mut c = PipelineContext::new(body);
    c.user = Some(user(org));
    c.model = Some(model.into());
    c.messages = messages;
    c.response_format = fmt;
    c
}

pub fn openai_body(text: &str) -> Value {
    json!({"id": "chatcmpl-1", "object": "chat.completion", "created": 1700000000,
        "model": "gpt-4o",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}})
}

pub fn metrics() -> Arc<Metrics> {
    Arc::new(Metrics::new("respcache-test").expect("metrics registry"))
}

pub fn flags() -> Arc<StaticFlagResolver> {
    Arc::new(StaticFlagResolver::default())
}

/// Counter value for `waddleai_cache_lookups_total{layer,result}`.
pub fn lookups(m: &Metrics, layer: &str, result: &str) -> f64 {
    m.counter("waddleai_cache_lookups_total")
        .expect("counter exists")
        .with_label_values(&[layer, result])
        .get()
}

pub struct Rig {
    pub exact: Arc<MemExact>,
    pub metrics: Arc<Metrics>,
    pub cache: Arc<ResponseCache>,
}

/// Exact-only rig with default config.
pub fn rig() -> Rig {
    let exact = Arc::new(MemExact::default());
    let metrics = metrics();
    let cache = Arc::new(ResponseCache::new(
        exact.clone(),
        Arc::new(FixedConfig(ResolvedCacheConfig::default())),
        flags(),
        metrics.clone(),
    ));
    Rig {
        exact,
        metrics,
        cache,
    }
}

/// Semantic-enabled rig (exact also on, threshold 0.95).
pub fn semantic_rig(embedder: Arc<FakeEmbedder>) -> (Rig, Arc<MemSemantic>) {
    let exact = Arc::new(MemExact::default());
    let store = Arc::new(MemSemantic::default());
    let metrics = metrics();
    let semantic = Arc::new(SemanticCache::new(store.clone(), embedder, metrics.clone()));
    let cfg = ResolvedCacheConfig {
        semantic_enabled: true,
        ..ResolvedCacheConfig::default()
    };
    let cache = Arc::new(
        ResponseCache::new(
            exact.clone(),
            Arc::new(FixedConfig(cfg)),
            flags(),
            metrics.clone(),
        )
        .with_semantic(semantic),
    );
    (
        Rig {
            exact,
            metrics,
            cache,
        },
        store,
    )
}
