//! Exact Valkey response-cache layer (port of `shared/cache/exact.py` + the eligibility matrix of
//! `shared/cache/keys.py`).
//!
//! Layout is byte-compatible with the live Python proxy: `waddleai:cache:exact:{org}:{sha256}`
//! holds `{"response","usage","stored_at"}` JSON with a jittered TTL, a per-org sorted set
//! (`waddleai:cache:idx:{org}`, score = last access epoch) tracks LRU order, and a per-org byte
//! counter (`waddleai:cache:bytes:{org}`) enforces the org quota. Entries from either
//! implementation are readable by the other. Atomicity is best-effort, as in Python: a lost race
//! degrades hit rate or quota accounting, never leaks across orgs.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

use redis::aio::MultiplexedConnection;
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::StoreError;
use crate::metrics::CacheMetrics;
use crate::singleflight::jittered_ttl;

/// A cached provider response ready for direct replay.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CachedResponse {
    /// Client-facing response body, exactly as dispatched.
    pub response: Value,
    /// Provider usage block.
    pub usage: Value,
    /// Reserved write timestamp (Python always stores `0.0`).
    pub stored_at: f64,
}

/// Bounds applied to one exact-cache write.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PutLimits {
    /// Base TTL in seconds (jittered on write).
    pub ttl_secs: u64,
    /// Largest accepted entry, in KiB; larger entries are rejected, never written.
    pub max_entry_kb: u64,
    /// Per-org byte quota in KiB; exceeding it evicts that org's least-recently-used entries.
    pub org_quota_kb: u64,
}

/// Storage for exact-cache entries.
#[async_trait::async_trait]
pub trait ExactStore: Send + Sync {
    /// Fetches an entry, refreshing its LRU score on hit.
    async fn get(&self, org_id: i64, key: &str) -> Result<Option<CachedResponse>, StoreError>;
    /// Writes an entry; `Ok(false)` means rejected for exceeding `max_entry_kb` (nothing written).
    async fn put(
        &self,
        org_id: i64,
        key: &str,
        value: &CachedResponse,
        limits: PutLimits,
    ) -> Result<bool, StoreError>;
}

/// Valkey-backed [`ExactStore`].
pub struct ValkeyExact {
    conn: MultiplexedConnection,
    metrics: CacheMetrics,
    jitter_fraction: f64,
}

fn now_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

impl ValkeyExact {
    /// Binds a Valkey connection and the shared metrics registry.
    pub fn new(conn: MultiplexedConnection, metrics: std::sync::Arc<telemetry::Metrics>) -> Self {
        Self {
            conn,
            metrics: CacheMetrics::new(metrics),
            jitter_fraction: crate::singleflight::SingleFlightConfig::from_env()
                .ttl_jitter_fraction,
        }
    }

    async fn current_bytes(&self, org_id: i64) -> Result<i64, StoreError> {
        let mut c = self.conn.clone();
        let raw: Option<i64> = c.get(cache::keys::exact_bytes(org_id)).await?;
        Ok(raw.unwrap_or(0))
    }

    async fn adjust_bytes(&self, org_id: i64, delta: i64) -> Result<(), StoreError> {
        let current = self.current_bytes(org_id).await?;
        let mut c = self.conn.clone();
        let _: () = c
            .set(cache::keys::exact_bytes(org_id), (current + delta).max(0))
            .await?;
        Ok(())
    }

    /// Evicts this org's least-recently-accessed entries until `needed` more bytes fit. Touches
    /// only the org's own namespace.
    async fn evict_to_fit(
        &self,
        org_id: i64,
        needed: i64,
        org_quota_kb: u64,
    ) -> Result<(), StoreError> {
        let quota = i64::try_from(org_quota_kb.saturating_mul(1024)).unwrap_or(i64::MAX);
        let mut current = self.current_bytes(org_id).await?;
        let mut c = self.conn.clone();
        while current.saturating_add(needed) > quota {
            let lru: Vec<String> = c.zrange(cache::keys::exact_idx(org_id), 0, 0).await?;
            let Some(member) = lru.into_iter().next() else {
                break;
            };
            let entry_key = cache::keys::exact_entry(org_id, &member);
            let raw: Option<Vec<u8>> = c.get(&entry_key).await?;
            let size = raw.map_or(0, |r| r.len()) as i64;
            let _: i64 = c.del(&entry_key).await?;
            let _: i64 = c.zrem(cache::keys::exact_idx(org_id), &member).await?;
            current = (current - size).max(0);
            let _: () = c.set(cache::keys::exact_bytes(org_id), current).await?;
            self.metrics.eviction("exact");
            tracing::debug!(org_id, bytes = size, "exact cache evicted LRU entry");
        }
        Ok(())
    }

    async fn get_inner(
        &self,
        org_id: i64,
        key: &str,
    ) -> Result<Option<CachedResponse>, StoreError> {
        let mut c = self.conn.clone();
        let raw: Option<Vec<u8>> = c.get(cache::keys::exact_entry(org_id, key)).await?;
        let Some(raw) = raw else {
            return Ok(None);
        };
        let _: i64 = c
            .zadd(cache::keys::exact_idx(org_id), key, now_epoch())
            .await?;
        match serde_json::from_slice::<CachedResponse>(&raw) {
            Ok(v) => Ok(Some(v)),
            Err(e) => {
                tracing::error!(org_id, error = %e, "exact cache: corrupt payload; treating as miss");
                Ok(None)
            }
        }
    }
}

#[async_trait::async_trait]
impl ExactStore for ValkeyExact {
    async fn get(&self, org_id: i64, key: &str) -> Result<Option<CachedResponse>, StoreError> {
        let start = Instant::now();
        let out = self.get_inner(org_id, key).await;
        self.metrics
            .lookup_duration("exact", start.elapsed().as_secs_f64());
        out
    }

    async fn put(
        &self,
        org_id: i64,
        key: &str,
        value: &CachedResponse,
        limits: PutLimits,
    ) -> Result<bool, StoreError> {
        let payload = serde_json::to_vec(value).map_err(|e| StoreError(e.to_string()))?;
        let size = payload.len() as i64;
        if payload.len() as u64 > limits.max_entry_kb.saturating_mul(1024) {
            tracing::debug!(
                org_id,
                bytes = size,
                max_entry_kb = limits.max_entry_kb,
                "exact cache: entry exceeds max_entry_kb; not written"
            );
            return Ok(false);
        }
        let entry_key = cache::keys::exact_entry(org_id, key);
        let mut c = self.conn.clone();
        let old: Option<Vec<u8>> = c.get(&entry_key).await?;
        let old_size = old.map_or(0, |o| o.len()) as i64;
        self.evict_to_fit(org_id, size - old_size, limits.org_quota_kb)
            .await?;
        let ttl = jittered_ttl(limits.ttl_secs, self.jitter_fraction).max(1);
        let _: () = c.set_ex(&entry_key, payload, ttl).await?;
        let _: i64 = c
            .zadd(cache::keys::exact_idx(org_id), key, now_epoch())
            .await?;
        self.adjust_bytes(org_id, size - old_size).await?;
        Ok(true)
    }
}

/// Python truthiness of a JSON value (`if value:`).
pub(crate) fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `float(value)` for the JSON shapes a request can carry; `None` where Python raises.
pub(crate) fn py_float(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

fn message_has_tool_result(message: &Value) -> bool {
    let Some(m) = message.as_object() else {
        return false;
    };
    if m.get("role").and_then(Value::as_str) == Some("tool") {
        return true;
    }
    if m.get("tool_calls").is_some_and(py_truthy) {
        return true;
    }
    if let Some(blocks) = m.get("content").and_then(Value::as_array) {
        return blocks.iter().any(|b| {
            matches!(
                b.get("type").and_then(Value::as_str),
                Some("tool_use" | "tool_result")
            )
        });
    }
    false
}

/// Determinism-eligibility matrix for the exact cache (spec 6.1): `temperature` explicitly
/// `0`/`0.0` and no message carrying an already-executed tool-call result. `temperature` is the
/// raw request value; `messages` the post-filter conversation.
pub fn is_exact_eligible(temperature: Option<&Value>, messages: &[Value]) -> bool {
    let Some(t) = temperature else {
        return false;
    };
    if t.is_null() {
        return false;
    }
    match py_float(t) {
        Some(0.0) => {}
        _ => return false,
    }
    !messages.iter().any(message_has_tool_result)
}

/// False for a failed or empty completion that must never enter the cache (it would be replayed to
/// every identical prompt). Unrecognized body shapes are cacheable; only a positively empty body is
/// refused.
pub fn is_cacheable_response(response: &Value, usage: &Value) -> bool {
    let failed = |v: &Value| v.get("failure_reason").is_some_and(py_truthy);
    if failed(usage) || failed(response) {
        return false;
    }
    if let Some(choices) = response.get("choices").and_then(Value::as_array) {
        if let Some(first) = choices.first() {
            let msg = first.get("message");
            let has = |k: &str| msg.and_then(|m| m.get(k)).is_some_and(py_truthy);
            return has("content") || has("tool_calls");
        }
    }
    if let Some(blocks) = response.get("content").and_then(Value::as_array) {
        return blocks.iter().any(|b| {
            b.is_object()
                && (b.get("type").and_then(Value::as_str) != Some("text")
                    || b.get("text").is_some_and(py_truthy))
        });
    }
    true
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn eligibility_matrix() {
        let user = json!({"role": "user", "content": "hi"});
        let ok = |t: Value, m: &[Value]| is_exact_eligible(Some(&t), m);
        assert!(ok(json!(0), std::slice::from_ref(&user)));
        assert!(ok(json!(0.0), std::slice::from_ref(&user)));
        assert!(
            ok(json!("0"), std::slice::from_ref(&user)),
            "Python float('0')"
        );
        assert!(
            ok(json!(false), std::slice::from_ref(&user)),
            "float(False) == 0.0"
        );
        assert!(!ok(json!(0.7), std::slice::from_ref(&user)));
        assert!(!ok(json!(true), std::slice::from_ref(&user)));
        assert!(!ok(json!("hot"), std::slice::from_ref(&user)));
        assert!(!ok(json!([0]), std::slice::from_ref(&user)));
        assert!(!is_exact_eligible(None, std::slice::from_ref(&user)));
        assert!(!is_exact_eligible(
            Some(&Value::Null),
            std::slice::from_ref(&user)
        ));
        for tool_msg in [
            json!({"role": "tool", "content": "x"}),
            json!({"role": "assistant", "tool_calls": [{"id": "1"}]}),
            json!({"role": "user", "content": [{"type": "tool_result"}]}),
            json!({"role": "assistant", "content": [{"type": "tool_use"}]}),
        ] {
            assert!(!ok(json!(0), &[user.clone(), tool_msg]));
        }
        // empty tool_calls, text blocks, non-object messages do not disqualify
        assert!(ok(
            json!(0),
            &[
                json!({"role": "assistant", "tool_calls": []}),
                json!({"role": "user", "content": [{"type": "text", "text": "a"}]}),
                json!("stray"),
            ]
        ));
    }

    #[test]
    fn cacheability_refuses_failed_and_empty_completions() {
        let usage = json!({});
        let ok = json!({"choices": [{"message": {"content": "hi"}}]});
        assert!(is_cacheable_response(&ok, &usage));
        assert!(is_cacheable_response(
            &json!({"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]}),
            &usage
        ));
        assert!(!is_cacheable_response(
            &json!({"choices": [{"message": {"content": ""}}]}),
            &usage
        ));
        assert!(!is_cacheable_response(&json!({"choices": [{}]}), &usage));
        assert!(!is_cacheable_response(
            &ok,
            &json!({"failure_reason": "budget"})
        ));
        assert!(!is_cacheable_response(
            &json!({"failure_reason": "x", "choices": [{"message": {"content": "hi"}}]}),
            &usage
        ));
        assert!(is_cacheable_response(
            &json!({"content": [{"type": "text", "text": "hi"}]}),
            &usage
        ));
        assert!(is_cacheable_response(
            &json!({"content": [{"type": "tool_use"}]}),
            &usage
        ));
        assert!(!is_cacheable_response(
            &json!({"content": [{"type": "text", "text": ""}]}),
            &usage
        ));
        assert!(!is_cacheable_response(&json!({"content": []}), &usage));
        assert!(
            is_cacheable_response(&json!({"weird": true}), &usage),
            "unknown shape cacheable"
        );
        assert!(is_cacheable_response(&json!({"choices": []}), &usage));
    }

    #[test]
    fn python_payload_shape_round_trips() {
        let raw = br#"{"response":{"a":1},"usage":{"total_tokens":3},"stored_at":0.0}"#;
        let v: CachedResponse = serde_json::from_slice(raw).unwrap();
        assert_eq!(v.usage["total_tokens"], 3);
        assert_eq!(
            serde_json::to_string(&v).unwrap(),
            String::from_utf8_lossy(raw)
        );
        assert!(serde_json::from_slice::<CachedResponse>(b"{\"response\":1}").is_err());
    }
}
