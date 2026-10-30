//! Valkey-backed atomic token/budget gate, ported from `shared/utils/token_limiter.py`.
//!
//! The Lua scripts are exported byte-for-byte from the Python constants
//! (`rust/scripts/gen-token-lua.py`) and embedded here; a drift test fails if they diverge.
//! Like the Python limiter it fails OPEN (request allowed) when Valkey or the script fails,
//! but every such event is logged at error level and counted so it is never silent.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Utc};
use pipeline::{FlagResolver, KeyLimits};
use redis::aio::MultiplexedConnection;
use redis::{Script, Value};

/// Lua for the TPM + budget check and reservation (exported from `TokenLimiter.LUA_RESERVE`).
pub const LUA_RESERVE: &str = include_str!("../lua/reserve.lua");
/// Lua for reconciling a reservation with actual usage (`TokenLimiter.LUA_RECONCILE`).
pub const LUA_RECONCILE: &str = include_str!("../lua/reconcile.lua");
/// Feature flag gating native rate limiting; OFF means every request is allowed ungated.
pub const NATIVE_RATE_LIMIT_FLAG: &str = "waddleai.native_rate_limit";

/// Outcome of a reserve call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateDecision {
    /// Whether the request may proceed.
    pub allowed: bool,
    /// Denial reason (`tpm_exceeded` | `monthly_tokens_exceeded` | `monthly_usd_exceeded`).
    pub reason: Option<String>,
    /// Reservation id to reconcile after the call (`None` when ungated).
    pub reservation_id: Option<String>,
}

impl GateDecision {
    /// An allow decision, optionally carrying a reservation id.
    pub fn allow(reservation_id: Option<String>) -> Self {
        Self {
            allowed: true,
            reason: None,
            reservation_id,
        }
    }

    /// A deny decision with the limiter's reason string.
    pub fn deny(reason: impl Into<String>) -> Self {
        Self {
            allowed: false,
            reason: Some(reason.into()),
            reservation_id: None,
        }
    }
}

/// Seam over the budget gate so the stage is testable without Valkey.
#[async_trait]
pub trait BudgetGate: Send + Sync {
    /// Reserves `estimated_tokens` / `estimated_usd` for a virtual key against its limits.
    async fn reserve(
        &self,
        vkey_id: i64,
        estimated_tokens: u64,
        estimated_usd: f64,
        limits: &KeyLimits,
    ) -> GateDecision;

    /// Adjusts a prior reservation to the actual usage; never errors (fail-open, logged).
    async fn reconcile(&self, reservation_id: &str, actual_tokens: u64, actual_usd: f64);
}

/// Atomic Valkey token limiter driving the exported Lua scripts via `EVALSHA`
/// (with automatic script reload on `NOSCRIPT`, like the Python `_evalsha_with_reload`).
pub struct TokenLimiter {
    conn: MultiplexedConnection,
    flags: Arc<dyn FlagResolver>,
    reserve_script: Script,
    reconcile_script: Script,
    fail_open_reserve: AtomicU64,
    fail_open_reserve_response: AtomicU64,
    fail_open_reconcile: AtomicU64,
}

impl TokenLimiter {
    /// Builds a limiter over a Valkey connection and a flag resolver.
    pub fn new(conn: MultiplexedConnection, flags: Arc<dyn FlagResolver>) -> Self {
        Self {
            conn,
            flags,
            reserve_script: Script::new(LUA_RESERVE),
            reconcile_script: Script::new(LUA_RECONCILE),
            fail_open_reserve: AtomicU64::new(0),
            fail_open_reserve_response: AtomicU64::new(0),
            fail_open_reconcile: AtomicU64::new(0),
        }
    }

    /// Count of fail-open events for a phase (`reserve`, `reserve_response`, `reconcile`).
    pub fn fail_open_count(&self, phase: &str) -> u64 {
        match phase {
            "reserve" => self.fail_open_reserve.load(Ordering::Relaxed),
            "reserve_response" => self.fail_open_reserve_response.load(Ordering::Relaxed),
            "reconcile" => self.fail_open_reconcile.load(Ordering::Relaxed),
            _ => 0,
        }
    }

    /// Reserve with an explicit clock (test seam); `reserve` uses `Utc::now()`.
    pub async fn reserve_at(
        &self,
        vkey_id: i64,
        estimated_tokens: u64,
        estimated_usd: f64,
        limits: &KeyLimits,
        now: DateTime<Utc>,
    ) -> GateDecision {
        let id = vkey_id.to_string();
        if !self.flags.enabled(NATIVE_RATE_LIMIT_FLAG, Some(&id)).await {
            return GateDecision::allow(None);
        }
        if limits.is_unlimited() {
            return GateDecision::allow(None);
        }
        let reservation_id = uuid::Uuid::new_v4().to_string();
        let mut conn = self.conn.clone();
        let result: Result<Value, redis::RedisError> = self
            .reserve_script
            .prepare_invoke()
            .arg(&id)
            .arg(estimated_tokens.to_string())
            .arg(format!("{estimated_usd:?}"))
            .arg(limits.tpm_limit.unwrap_or(0).to_string())
            .arg(limits.monthly_token_limit.unwrap_or(0).to_string())
            .arg(limits.monthly_usd_limit.unwrap_or(0).to_string())
            .arg(&reservation_id)
            .arg(now.format("%Y%m%d%H%M").to_string())
            .arg(now.format("%Y%m").to_string())
            .invoke_async(&mut conn)
            .await;
        match result {
            Ok(value) => match parse_reserve(&value) {
                Some(decision) => decision,
                None => {
                    tracing::error!("invalid Lua reserve response; failing open");
                    self.fail_open_reserve_response
                        .fetch_add(1, Ordering::Relaxed);
                    GateDecision::allow(None)
                }
            },
            Err(err) => {
                tracing::error!(error = %err, "token limiter reserve failed; failing open");
                self.fail_open_reserve.fetch_add(1, Ordering::Relaxed);
                GateDecision::allow(None)
            }
        }
    }
}

/// Decodes the script reply `{allowed, reason, reservation_id}`; `None` when malformed
/// (fewer than two elements, or a non-integer first element), matching Python's checks.
fn parse_reserve(value: &Value) -> Option<GateDecision> {
    let Value::Array(items) = value else {
        return None;
    };
    if items.len() < 2 {
        return None;
    }
    let allowed = match items.first()? {
        Value::Int(n) => *n != 0,
        _ => return None,
    };
    let text = |v: Option<&Value>| match v {
        Some(Value::BulkString(b)) => Some(String::from_utf8_lossy(b).into_owned()),
        Some(Value::SimpleString(s)) => Some(s.clone()),
        _ => None,
    };
    if allowed {
        Some(GateDecision::allow(text(items.get(2))))
    } else {
        Some(GateDecision::deny(text(items.get(1)).unwrap_or_default()))
    }
}

#[async_trait]
impl BudgetGate for TokenLimiter {
    async fn reserve(
        &self,
        vkey_id: i64,
        estimated_tokens: u64,
        estimated_usd: f64,
        limits: &KeyLimits,
    ) -> GateDecision {
        self.reserve_at(vkey_id, estimated_tokens, estimated_usd, limits, Utc::now())
            .await
    }

    async fn reconcile(&self, reservation_id: &str, actual_tokens: u64, actual_usd: f64) {
        let now = Utc::now();
        let mut conn = self.conn.clone();
        let result: Result<Value, redis::RedisError> = self
            .reconcile_script
            .prepare_invoke()
            .arg(reservation_id)
            .arg(actual_tokens.to_string())
            .arg(format!("{actual_usd:?}"))
            .arg(now.format("%Y%m%d%H%M").to_string())
            .arg(now.format("%Y%m").to_string())
            .invoke_async(&mut conn)
            .await;
        if let Err(err) = result {
            tracing::error!(error = %err, "token limiter reconcile failed; failing open");
            self.fail_open_reconcile.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_reserve_shapes() {
        let bulk = |s: &str| Value::BulkString(s.as_bytes().to_vec());
        let allow = Value::Array(vec![Value::Int(1), bulk(""), bulk("rid")]);
        assert_eq!(
            parse_reserve(&allow),
            Some(GateDecision::allow(Some("rid".into())))
        );
        let deny = Value::Array(vec![Value::Int(0), bulk("tpm_exceeded"), bulk("")]);
        assert_eq!(
            parse_reserve(&deny),
            Some(GateDecision::deny("tpm_exceeded"))
        );
        let simple = Value::Array(vec![
            Value::Int(1),
            Value::SimpleString("".into()),
            Value::SimpleString("r".into()),
        ]);
        assert_eq!(
            parse_reserve(&simple),
            Some(GateDecision::allow(Some("r".into())))
        );
        assert_eq!(parse_reserve(&Value::Nil), None);
        assert_eq!(parse_reserve(&Value::Array(vec![Value::Int(1)])), None);
        assert_eq!(
            parse_reserve(&Value::Array(vec![bulk("x"), bulk("y")])),
            None
        );
    }

    #[test]
    fn limits_unlimited_detection() {
        assert!(KeyLimits::default().is_unlimited());
        assert!(!KeyLimits {
            tpm_limit: Some(1),
            ..Default::default()
        }
        .is_unlimited());
    }
}
