//! Embedding client for the semantic layer: Ollama `/api/embeddings` over rustls.
//!
//! Model and endpoint resolution mirror Python (`shared/utils/embedding_manager.py`,
//! `shared/utils/ollama_endpoint.py`): `nomic-embed-text` by default, a dedicated embedding
//! endpoint (`WADDLEAI_OLLAMA_EMBEDDING_URL`, then the legacy names) when configured, else the
//! chat Ollama (`WADDLEAI_OLLAMA_URL`, then the legacy names, else localhost).

use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::json;

use crate::metrics::CacheMetrics;

/// Default embedding model (768-dim), matching `EmbeddingConfig.default_ollama`.
pub const DEFAULT_EMBEDDING_MODEL: &str = "nomic-embed-text";

const DEFAULT_OLLAMA_URL: &str = "http://localhost:11434";
const CHAT_ENVS: [&str; 5] = [
    "WADDLEAI_OLLAMA_URL",
    "OLLAMA_API_URL",
    "OLLAMA_URL",
    "OLLAMA_HOST",
    "OLLAMA_BASE_URL",
];
const EMBEDDING_ENVS: [&str; 3] = [
    "WADDLEAI_OLLAMA_EMBEDDING_URL",
    "OLLAMA_EMBEDDING_URL",
    "PENGUINCODE_EMBEDDING_OLLAMA_URL",
];

/// An embedding call failed. The message never contains the prompt.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct EmbedError(pub String);

/// Produces an embedding vector for a text.
#[async_trait::async_trait]
pub trait Embedder: Send + Sync {
    /// Embeds `text`; any failure is an `Err` the caller degrades on.
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError>;
    /// False when a circuit/health signal says calls should be bypassed outright.
    fn is_healthy(&self) -> bool {
        true
    }
}

fn first_set(get: &dyn Fn(&str) -> Option<String>, names: &[&str]) -> Option<String> {
    names.iter().find_map(|n| get(n).filter(|v| !v.is_empty()))
}

/// Resolves the Ollama base URL embedding calls target, over an injectable env lookup.
pub fn resolve_embedding_url(get: &dyn Fn(&str) -> Option<String>) -> String {
    let chat = first_set(get, &CHAT_ENVS).unwrap_or_else(|| DEFAULT_OLLAMA_URL.to_owned());
    first_set(get, &EMBEDDING_ENVS).unwrap_or(chat)
}

/// Closed metric label: `embedding_ollama` when a dedicated endpoint is configured, else
/// `chat_ollama`. Never the raw URL.
pub fn embedding_endpoint_label(get: &dyn Fn(&str) -> Option<String>) -> &'static str {
    if first_set(get, &EMBEDDING_ENVS).is_some() {
        "embedding_ollama"
    } else {
        "chat_ollama"
    }
}

#[derive(Deserialize)]
struct EmbeddingReply {
    embedding: Vec<f32>,
}

/// HTTP [`Embedder`] against Ollama.
pub struct EmbeddingClient {
    http: reqwest::Client,
    url: String,
    model: String,
    label: &'static str,
    metrics: Option<CacheMetrics>,
}

impl EmbeddingClient {
    /// Client for `base_url` (no trailing path) and `model`. `label` is the closed endpoint label.
    pub fn new(
        base_url: &str,
        model: &str,
        label: &'static str,
        metrics: Option<Arc<telemetry::Metrics>>,
    ) -> Result<Self, EmbedError> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| EmbedError(format!("embedding http client: {e}")))?;
        Ok(Self {
            http,
            url: format!("{}/api/embeddings", base_url.trim_end_matches('/')),
            model: model.to_owned(),
            label,
            metrics: metrics.map(CacheMetrics::new),
        })
    }

    /// Client configured from the process environment (see module docs).
    pub fn from_env(metrics: Option<Arc<telemetry::Metrics>>) -> Result<Self, EmbedError> {
        let get = |k: &str| std::env::var(k).ok();
        Self::new(
            &resolve_embedding_url(&get),
            DEFAULT_EMBEDDING_MODEL,
            embedding_endpoint_label(&get),
            metrics,
        )
    }

    async fn call(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        let resp = self
            .http
            .post(&self.url)
            .json(&json!({"model": self.model, "prompt": text}))
            .send()
            .await
            .map_err(|e| EmbedError(format!("embedding request failed: {}", e.without_url())))?;
        let status = resp.status();
        if !status.is_success() {
            return Err(EmbedError(format!("embedding endpoint returned {status}")));
        }
        let reply: EmbeddingReply = resp
            .json()
            .await
            .map_err(|e| EmbedError(format!("embedding reply malformed: {}", e.without_url())))?;
        if reply.embedding.is_empty() || reply.embedding.iter().any(|f| !f.is_finite()) {
            return Err(EmbedError("embedding reply empty or non-finite".into()));
        }
        Ok(reply.embedding)
    }
}

#[async_trait::async_trait]
impl Embedder for EmbeddingClient {
    async fn embed(&self, text: &str) -> Result<Vec<f32>, EmbedError> {
        let start = Instant::now();
        let out = self.call(text).await;
        if let Some(m) = &self.metrics {
            m.embedding_call(
                self.label,
                if out.is_ok() { "ok" } else { "error" },
                start.elapsed().as_secs_f64(),
            );
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| {
            pairs
                .iter()
                .find(|(n, _)| *n == k)
                .map(|(_, v)| (*v).to_owned())
        }
    }

    #[test]
    fn url_resolution_order_matches_python() {
        assert_eq!(resolve_embedding_url(&env(&[])), "http://localhost:11434");
        assert_eq!(
            resolve_embedding_url(&env(&[
                ("OLLAMA_HOST", "http://h:1"),
                ("OLLAMA_URL", "http://u:1")
            ])),
            "http://u:1",
            "OLLAMA_URL outranks OLLAMA_HOST"
        );
        assert_eq!(
            resolve_embedding_url(&env(&[
                ("WADDLEAI_OLLAMA_URL", "http://c:1"),
                ("OLLAMA_URL", "http://u:1")
            ])),
            "http://c:1"
        );
        assert_eq!(
            resolve_embedding_url(&env(&[
                ("WADDLEAI_OLLAMA_URL", "http://c:1"),
                ("OLLAMA_EMBEDDING_URL", "http://e:1"),
                ("WADDLEAI_OLLAMA_EMBEDDING_URL", "http://canon:1"),
            ])),
            "http://canon:1"
        );
        assert_eq!(
            resolve_embedding_url(&env(&[("PENGUINCODE_EMBEDDING_OLLAMA_URL", "http://p:1")])),
            "http://p:1"
        );
        assert_eq!(
            resolve_embedding_url(&env(&[("OLLAMA_URL", "")])),
            "http://localhost:11434"
        );
    }

    #[test]
    fn endpoint_label_is_a_closed_pair() {
        assert_eq!(embedding_endpoint_label(&env(&[])), "chat_ollama");
        assert_eq!(
            embedding_endpoint_label(&env(&[("OLLAMA_URL", "x")])),
            "chat_ollama"
        );
        assert_eq!(
            embedding_endpoint_label(&env(&[("OLLAMA_EMBEDDING_URL", "x")])),
            "embedding_ollama"
        );
    }
}
