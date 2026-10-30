//! Ollama/llama.cpp KV-cache session affinity (port of `shared/cache/affinity.py`).
//!
//! `waddleai:affinity:{org}:{session_or_prefix_sha}` maps to a backend id with a sliding TTL
//! refreshed on every successful lookup. The map is advisory: whether a hint is honored at
//! dispatch is routing's decision.

use std::time::Duration;

use redis::aio::MultiplexedConnection;
use redis::AsyncCommands;

/// Org-scoped session/prefix to backend affinity map.
#[async_trait::async_trait]
pub trait AffinityStore: Send + Sync {
    /// The affine backend for this org's session, sliding its TTL forward. Faults degrade to `None`.
    async fn lookup(&self, org_id: i64, session_hash: &str) -> Option<String>;
    /// Records (or overwrites) the affinity.
    async fn record(&self, org_id: i64, session_hash: &str, backend_id: &str);
}

/// Valkey-backed [`AffinityStore`] (default 30 minute sliding window).
pub struct ValkeyAffinity {
    conn: MultiplexedConnection,
    ttl: Duration,
}

impl ValkeyAffinity {
    /// Binds a connection and the sliding-TTL window.
    pub fn new(conn: MultiplexedConnection, ttl: Duration) -> Self {
        Self { conn, ttl }
    }
}

#[async_trait::async_trait]
impl AffinityStore for ValkeyAffinity {
    async fn lookup(&self, org_id: i64, session_hash: &str) -> Option<String> {
        let key = cache::keys::affinity(org_id, session_hash);
        let mut c = self.conn.clone();
        let got: Result<Option<String>, _> = c.get(&key).await;
        match got {
            Ok(Some(backend)) => {
                let slid: Result<(), _> = c.set_ex(&key, &backend, self.ttl.as_secs().max(1)).await;
                if let Err(e) = slid {
                    tracing::warn!(error = %e, "affinity TTL slide failed");
                }
                Some(backend)
            }
            Ok(None) => None,
            Err(e) => {
                tracing::warn!(error = %e, "affinity lookup failed; no backend hint");
                None
            }
        }
    }

    async fn record(&self, org_id: i64, session_hash: &str, backend_id: &str) {
        let mut c = self.conn.clone();
        let set: Result<(), _> = c
            .set_ex(
                cache::keys::affinity(org_id, session_hash),
                backend_id,
                self.ttl.as_secs().max(1),
            )
            .await;
        if let Err(e) = set {
            tracing::warn!(error = %e, "affinity record failed");
        }
    }
}
