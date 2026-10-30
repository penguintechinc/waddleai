//! Python-parity cache metrics over the shared `telemetry::Metrics` registry
//! (`waddleai_cache_*`, `waddleai_embedding_*`). Labels are closed vocabularies only.

use std::sync::Arc;

use telemetry::Metrics;

/// Thin recorder over the process Prometheus registry. A missing collector is logged, never
/// raised: telemetry failure must not become a request failure.
#[derive(Clone)]
pub struct CacheMetrics {
    inner: Arc<Metrics>,
}

impl CacheMetrics {
    /// Wraps the shared registry.
    pub fn new(inner: Arc<Metrics>) -> Self {
        Self { inner }
    }

    /// `waddleai_cache_lookups_total{layer,result}`; result is
    /// `hit|miss|bypass|stampede_wait|stampede_fallthrough`.
    pub fn lookup(&self, layer: &str, result: &str) {
        match self.inner.counter("waddleai_cache_lookups_total") {
            Ok(c) => c.with_label_values(&[layer, result]).inc(),
            Err(e) => tracing::warn!(error = %e, "cache lookup metric unavailable"),
        }
    }

    /// `waddleai_cache_lookup_duration_seconds{layer}`.
    pub fn lookup_duration(&self, layer: &str, seconds: f64) {
        match self
            .inner
            .histogram("waddleai_cache_lookup_duration_seconds")
        {
            Ok(h) => h.with_label_values(&[layer]).observe(seconds.max(0.0)),
            Err(e) => tracing::warn!(error = %e, "cache duration metric unavailable"),
        }
    }

    /// `waddleai_cache_tokens_saved_total{layer}`; a zero count records nothing (Python parity).
    pub fn tokens_saved(&self, layer: &str, tokens: u64) {
        if tokens == 0 {
            return;
        }
        match self.inner.counter("waddleai_cache_tokens_saved_total") {
            Ok(c) => c.with_label_values(&[layer]).inc_by(tokens as f64),
            Err(e) => tracing::warn!(error = %e, "cache tokens-saved metric unavailable"),
        }
    }

    /// `waddleai_cache_entries_evicted_total{layer}`.
    pub fn eviction(&self, layer: &str) {
        match self.inner.counter("waddleai_cache_entries_evicted_total") {
            Ok(c) => c.with_label_values(&[layer]).inc(),
            Err(e) => tracing::warn!(error = %e, "cache eviction metric unavailable"),
        }
    }

    /// `waddleai_embedding_calls_total` and `..._duration_seconds`; `endpoint` is the closed
    /// `chat_ollama|embedding_ollama` label, `outcome` is `ok|error`.
    pub fn embedding_call(&self, endpoint: &str, outcome: &str, seconds: f64) {
        match self.inner.counter("waddleai_embedding_calls_total") {
            Ok(c) => c.with_label_values(&[endpoint, outcome]).inc(),
            Err(e) => tracing::warn!(error = %e, "embedding call metric unavailable"),
        }
        match self
            .inner
            .histogram("waddleai_embedding_call_duration_seconds")
        {
            Ok(h) => h
                .with_label_values(&[endpoint, outcome])
                .observe(seconds.max(0.0)),
            Err(e) => tracing::warn!(error = %e, "embedding duration metric unavailable"),
        }
    }
}
