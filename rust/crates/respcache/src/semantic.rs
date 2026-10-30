//! Restricted semantic response cache (port of `shared/cache/semantic.py`). Default OFF.
//!
//! Eligibility is strictly narrower than the exact layer: single-turn, no `tools`, no memory
//! injection, `temperature == 0`, and an informational last user message. A hit requires an exact
//! `(org, model_class, context_hash)` match plus cosine similarity >= the resolved threshold.
//! Candidates are narrowed by that exact filter first, then ranked here, so the same path works
//! on any store. Embedding is bounded by a latency budget; when it fails, times out or the
//! embedder reports unhealthy, the layer degrades to exact-only VISIBLY: a WARN log and
//! `waddleai_cache_lookups_total{layer="semantic",result="bypass"}`, never silently.

use std::sync::Arc;
use std::time::Duration;

use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::embed::Embedder;
use crate::error::StoreError;
use crate::exact::{py_float, py_truthy, CachedResponse};
use crate::metrics::CacheMetrics;
use crate::singleflight::{dedup_compute, jittered_ttl, InProcessSingleFlight, SingleFlightConfig};

/// Conversation-shape flags computed by the caller (not derivable from the body alone).
#[derive(Debug, Clone, PartialEq)]
pub struct CtxFlags {
    /// At most one message in the conversation.
    pub is_single_turn: bool,
    /// The request carries a non-empty `tools` schema.
    pub has_tools_schema: bool,
    /// Messages differ from the client's (memory/context was injected).
    pub has_memory_injection: bool,
    /// Raw `temperature` from the request.
    pub temperature: Option<Value>,
}

const INFORMATIONAL_STARTS: [&str; 14] = [
    "what", "who", "when", "where", "why", "how", "is ", "are ", "does ", "do ", "can ", "could ",
    "would ", "which",
];
const IMPERATIVE_MARKERS: [&str; 8] = [
    "write",
    "generate",
    "create",
    "implement",
    "refactor",
    "fix ",
    "debug",
    "code",
];

/// Conservative heuristic: question-shaped text is `informational`, everything ambiguous is
/// `other` (ineligible). A false negative is only a miss, never a wrong hit.
pub fn default_classify_intent(text: &str) -> &'static str {
    let stripped = text.trim();
    if stripped.is_empty() {
        return "other";
    }
    let lowered = stripped.to_lowercase();
    if IMPERATIVE_MARKERS.iter().any(|m| lowered.contains(m)) {
        return "other";
    }
    if stripped.ends_with('?') || INFORMATIONAL_STARTS.iter().any(|s| lowered.starts_with(s)) {
        return "informational";
    }
    "other"
}

/// Restriction matrix for the semantic layer (spec 6.2/6.5). `messages` are the post-filter
/// conversation (the body's `messages` replaced by the context's).
pub fn is_semantic_eligible(body: &Value, messages: &[Value], flags: &CtxFlags) -> bool {
    if !flags.is_single_turn || flags.has_tools_schema || flags.has_memory_injection {
        return false;
    }
    if body.get("tools").is_some_and(py_truthy) {
        return false;
    }
    match flags.temperature.as_ref().and_then(py_float) {
        Some(0.0) => {}
        _ => return false,
    }
    let last_user = messages
        .iter()
        .rev()
        .find(|m| m.get("role").and_then(Value::as_str) == Some("user"));
    let Some(text) = last_user
        .and_then(|m| m.get("content"))
        .and_then(Value::as_str)
    else {
        return false;
    };
    default_classify_intent(text) == "informational"
}

/// Cosine similarity; 0.0 for mismatched or empty or zero-norm vectors (Python parity).
fn cosine(a: &[f32], b: &[f32]) -> f64 {
    if a.len() != b.len() || a.is_empty() {
        return 0.0;
    }
    let (mut dot, mut na, mut nb) = (0.0f64, 0.0f64, 0.0f64);
    for (x, y) in a.iter().zip(b) {
        let (x, y) = (f64::from(*x), f64::from(*y));
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    if na == 0.0 || nb == 0.0 {
        return 0.0;
    }
    dot / (na.sqrt() * nb.sqrt())
}

/// Normalized-prompt key for in-process embedding de-dup (whitespace/case only, hashed so the
/// prompt text never becomes a map key or log field).
fn embed_dedup_key(text: &str) -> String {
    let normalized = text
        .trim()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let digest = Sha256::digest(normalized.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("semantic:embed:{hex}")
}

/// One stored semantic candidate.
#[derive(Debug, Clone, PartialEq)]
pub struct SemanticEntry {
    /// Row id (for the hit counter).
    pub id: i64,
    /// Stored prompt embedding.
    pub embedding: Vec<f32>,
    /// Stored full response body.
    pub response: Value,
}

/// Storage for semantic entries (`response_cache_entries`).
#[async_trait::async_trait]
pub trait SemanticStore: Send + Sync {
    /// Unexpired entries for the exact `(org, model_class, context_hash)` scope.
    async fn candidates(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
    ) -> Result<Vec<SemanticEntry>, StoreError>;
    /// Inserts an entry expiring `ttl_secs` from now.
    async fn insert(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
        embedding: &[f32],
        response: &Value,
        ttl_secs: u64,
    ) -> Result<(), StoreError>;
    /// Bumps an entry's hit counter.
    async fn increment_hit(&self, id: i64) -> Result<(), StoreError>;
}

/// Postgres [`SemanticStore`] over `response_cache_entries` (embedding as JSON text in
/// `prompt_embedding_json`, exactly as the Python layer writes it, so both read each other's rows).
pub struct PgSemanticStore {
    db: DatabaseConnection,
}

impl PgSemanticStore {
    /// Binds a pooled connection.
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

fn int4(v: i64, what: &str) -> Result<i32, StoreError> {
    i32::try_from(v).map_err(|_| StoreError(format!("{what} out of range for INTEGER column")))
}

#[async_trait::async_trait]
impl SemanticStore for PgSemanticStore {
    async fn candidates(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
    ) -> Result<Vec<SemanticEntry>, StoreError> {
        let stmt = Statement::from_sql_and_values(
            DbBackend::Postgres,
            "SELECT id, prompt_embedding_json, response::text AS response_json \
             FROM response_cache_entries \
             WHERE org_id = $1 AND model_class = $2 AND context_hash = $3 \
               AND expires_at > (now() AT TIME ZONE 'utc') \
               AND prompt_embedding_json IS NOT NULL",
            [
                int4(org_id, "org_id")?.into(),
                model_class.into(),
                context_hash.into(),
            ],
        );
        let rows = self.db.query_all_raw(stmt).await?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let id: i32 = row.try_get("", "id")?;
            let emb: String = row.try_get("", "prompt_embedding_json")?;
            let resp: String = row.try_get("", "response_json")?;
            let (Ok(embedding), Ok(response)) = (
                serde_json::from_str::<Vec<f32>>(&emb),
                serde_json::from_str::<Value>(&resp),
            ) else {
                tracing::warn!(entry_id = id, "semantic cache: unreadable row skipped");
                continue;
            };
            out.push(SemanticEntry {
                id: i64::from(id),
                embedding,
                response,
            });
        }
        Ok(out)
    }

    async fn insert(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
        embedding: &[f32],
        response: &Value,
        ttl_secs: u64,
    ) -> Result<(), StoreError> {
        let emb = serde_json::to_string(embedding).map_err(|e| StoreError(e.to_string()))?;
        let stmt = Statement::from_sql_and_values(
            DbBackend::Postgres,
            "INSERT INTO response_cache_entries \
             (org_id, model_class, prompt_embedding_json, context_hash, response, hit_count, \
              created_at, expires_at) \
             VALUES ($1, $2, $3, $4, CAST($5 AS json), 0, (now() AT TIME ZONE 'utc'), \
                     (now() AT TIME ZONE 'utc') + (CAST($6 AS double precision) * interval '1 second'))",
            [
                int4(org_id, "org_id")?.into(),
                model_class.into(),
                emb.into(),
                context_hash.into(),
                response.to_string().into(),
                (ttl_secs as f64).into(),
            ],
        );
        self.db.execute_raw(stmt).await?;
        Ok(())
    }

    async fn increment_hit(&self, id: i64) -> Result<(), StoreError> {
        let stmt = Statement::from_sql_and_values(
            DbBackend::Postgres,
            "UPDATE response_cache_entries SET hit_count = COALESCE(hit_count, 0) + 1 WHERE id = $1",
            [int4(id, "id")?.into()],
        );
        self.db.execute_raw(stmt).await?;
        Ok(())
    }
}

/// The semantic layer: embed, rank candidates, write entries.
pub struct SemanticCache {
    store: Arc<dyn SemanticStore>,
    embedder: Arc<dyn Embedder>,
    metrics: CacheMetrics,
    embed_timeout: Duration,
    jitter_fraction: f64,
    embed_flight: InProcessSingleFlight<Vec<f32>>,
    /// In-process single-flight for semantic misses (used by the facade's stampede guard).
    pub(crate) flight: InProcessSingleFlight<CachedResponse>,
}

impl SemanticCache {
    /// Layer over `store` and `embedder`; budget and jitter default from the stampede tunables.
    pub fn new(
        store: Arc<dyn SemanticStore>,
        embedder: Arc<dyn Embedder>,
        metrics: Arc<telemetry::Metrics>,
    ) -> Self {
        let cfg = SingleFlightConfig::from_env();
        Self {
            store,
            embedder,
            metrics: CacheMetrics::new(metrics),
            embed_timeout: cfg.embed_timeout,
            jitter_fraction: cfg.ttl_jitter_fraction,
            embed_flight: InProcessSingleFlight::new(),
            flight: InProcessSingleFlight::new(),
        }
    }

    /// Overrides the embedding latency budget.
    pub fn with_embed_timeout(mut self, budget: Duration) -> Self {
        self.embed_timeout = budget;
        self
    }

    /// Embeds `text` under the latency budget, de-duplicating concurrent identical prompts.
    /// `None` means bypass: the embedder is unhealthy, failed, or exceeded the budget. Every
    /// bypass is logged at WARN and counted, so exact-only degradation is never silent.
    pub async fn embed(&self, text: &str) -> Option<Vec<f32>> {
        if !self.embedder.is_healthy() {
            tracing::warn!(
                "embedding unavailable (embedder unhealthy); semantic cache degraded to exact-only"
            );
            self.metrics.lookup("semantic", "bypass");
            return None;
        }
        let key = embed_dedup_key(text);
        let work = dedup_compute(&self.embed_flight, &key, Duration::from_secs(10), || {
            self.embedder.embed(text)
        });
        match tokio::time::timeout(self.embed_timeout, work).await {
            Ok(Ok(v)) => Some(v),
            Ok(Err(e)) => {
                tracing::warn!(error = %e, "embedding unavailable; semantic cache degraded to exact-only");
                self.metrics.lookup("semantic", "bypass");
                None
            }
            Err(_) => {
                tracing::warn!(
                    budget_ms = self.embed_timeout.as_millis() as u64,
                    "embedding unavailable (exceeded latency budget); semantic cache degraded to exact-only"
                );
                self.metrics.lookup("semantic", "bypass");
                None
            }
        }
    }

    /// Ranks this scope's candidates against an already-computed embedding; the best one at or
    /// above `threshold` is a hit. A store fault is a visible bypass, not a request failure.
    pub async fn score_candidates(
        &self,
        org_id: i64,
        model_class: &str,
        context_hash: &str,
        query: &[f32],
        threshold: f64,
    ) -> Option<CachedResponse> {
        let candidates = match self
            .store
            .candidates(org_id, model_class, context_hash)
            .await
        {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!(error = %e, "semantic cache store unavailable; treating as miss");
                self.metrics.lookup("semantic", "bypass");
                return None;
            }
        };
        let mut best: Option<(&SemanticEntry, f64)> = None;
        for c in &candidates {
            let score = cosine(query, &c.embedding);
            if best.is_none_or(|(_, s)| score > s) {
                best = Some((c, score));
            }
        }
        let (entry, score) = best?;
        if score < threshold {
            return None;
        }
        if let Err(e) = self.store.increment_hit(entry.id).await {
            tracing::warn!(error = %e, "semantic cache hit counter update failed");
        }
        let usage = entry.response.get("usage").cloned().unwrap_or(json!({}));
        Some(CachedResponse {
            response: entry.response.clone(),
            usage,
            stored_at: 0.0,
        })
    }

    /// Embeds and stores a response. A no-op (logged by [`Self::embed`]) when the prompt cannot be
    /// embedded; never raises into the caller's response path.
    pub async fn put(
        &self,
        org_id: i64,
        model_class: &str,
        last_user_msg: &str,
        context_hash: &str,
        response: &CachedResponse,
        ttl_secs: u64,
    ) {
        let Some(embedding) = self.embed(last_user_msg).await else {
            tracing::debug!(
                org_id,
                "semantic cache: embedding bypassed; entry not written"
            );
            return;
        };
        let ttl = jittered_ttl(ttl_secs, self.jitter_fraction);
        if let Err(e) = self
            .store
            .insert(
                org_id,
                model_class,
                context_hash,
                &embedding,
                &response.response,
                ttl,
            )
            .await
        {
            tracing::warn!(error = %e, "semantic cache write failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flags(single: bool, tools: bool, mem: bool, t: Option<Value>) -> CtxFlags {
        CtxFlags {
            is_single_turn: single,
            has_tools_schema: tools,
            has_memory_injection: mem,
            temperature: t,
        }
    }

    fn q(text: &str) -> Vec<Value> {
        vec![json!({"role": "user", "content": text})]
    }

    #[test]
    fn intent_heuristic_is_conservative() {
        for t in [
            "What is Rust?",
            "how do I x",
            "Is it ok",
            "Could you tell me",
            "plain question?",
        ] {
            assert_eq!(default_classify_intent(t), "informational", "{t}");
        }
        for t in [
            "",
            "  ",
            "Write a poem?",
            "fix this bug",
            "Tell me about Rust",
            "what is the code for x?",
        ] {
            assert_eq!(default_classify_intent(t), "other", "{t}");
        }
    }

    #[test]
    fn eligibility_requires_every_condition() {
        let body = json!({});
        let t0 = Some(json!(0));
        let m = q("What is Rust?");
        assert!(is_semantic_eligible(
            &body,
            &m,
            &flags(true, false, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &m,
            &flags(false, false, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &m,
            &flags(true, true, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &m,
            &flags(true, false, true, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &json!({"tools": [{"x": 1}]}),
            &m,
            &flags(true, false, false, t0.clone())
        ));
        assert!(is_semantic_eligible(
            &json!({"tools": []}),
            &m,
            &flags(true, false, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &m,
            &flags(true, false, false, None)
        ));
        assert!(!is_semantic_eligible(
            &body,
            &m,
            &flags(true, false, false, Some(json!(0.5)))
        ));
        assert!(!is_semantic_eligible(
            &body,
            &[],
            &flags(true, false, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &[json!({"role": "user", "content": [{"type": "text"}]})],
            &flags(true, false, false, t0.clone())
        ));
        assert!(!is_semantic_eligible(
            &body,
            &q("Write code"),
            &flags(true, false, false, t0)
        ));
    }

    #[test]
    fn cosine_edge_cases() {
        assert_eq!(cosine(&[1.0, 0.0], &[1.0, 0.0]), 1.0);
        assert!(cosine(&[1.0, 0.0], &[0.0, 1.0]).abs() < 1e-12);
        assert_eq!(cosine(&[1.0], &[1.0, 2.0]), 0.0, "length mismatch");
        assert_eq!(cosine(&[], &[]), 0.0);
        assert_eq!(cosine(&[0.0, 0.0], &[1.0, 1.0]), 0.0, "zero norm");
        assert!((cosine(&[1.0, 1.0], &[2.0, 2.0]) - 1.0).abs() < 1e-9);
    }

    #[test]
    fn embed_dedup_key_normalizes_case_and_whitespace_and_hides_text() {
        let a = embed_dedup_key("  What  IS   Rust? ");
        assert_eq!(a, embed_dedup_key("what is rust?"));
        assert_ne!(a, embed_dedup_key("what is go?"));
        assert!(!a.contains("rust") && a.starts_with("semantic:embed:") && a.len() == 15 + 64);
    }

    #[test]
    fn int4_rejects_out_of_range() {
        assert_eq!(int4(5, "x").unwrap(), 5);
        assert!(int4(i64::from(i32::MAX) + 1, "x").is_err());
    }
}
