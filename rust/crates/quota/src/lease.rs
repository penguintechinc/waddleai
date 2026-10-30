//! Atomic Valkey reserve/reconcile/release/sweep over the Python Lua scripts (spec 6, D4, D8).
//!
//! A line-for-line port of `QuotaLimiter` in `shared/quota/lease.py`: the scripts are the
//! Python constants exported verbatim, the keys come from `cache::keys::quota`, and every
//! argument is formatted exactly as Python's `str(...)`, so Rust and Python operate on the
//! same counters with zero drift. Any Valkey failure fails OPEN (D10), logged at error level.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use cache::keys::quota as keys;
use redis::aio::MultiplexedConnection;
use redis::{Script, Value};

use crate::types::{OverrideRule, QuotaDecision, ReconcileResult};

/// Reserve script, exported from `LUA_RESERVE` by `scripts/export-quota-lua.py`.
pub const LUA_RESERVE: &str = include_str!("../lua/reserve.lua");
/// Reconcile/release script, exported from `LUA_RECONCILE`.
pub const LUA_RECONCILE: &str = include_str!("../lua/reconcile.lua");
/// `override_counter` key prefix passed to the reconcile script (`keys.OVERRIDE_PREFIX`).
pub const OVERRIDE_PREFIX: &str = "wa:quota:override:";

/// Reservation index score TTL: how long before the sweeper may refund an orphan.
pub const RESERVATION_TTL_S: i64 = 3600;
/// Stored reservation record TTL.
pub const RECORD_TTL_S: i64 = 4200;
/// Counter TTL (35 days).
pub const COUNTER_TTL_S: i64 = 35 * 86400;

/// Clock returning Unix seconds; injectable so tests can pin reserve timestamps.
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// Wall-clock seconds since the Unix epoch (`time.time()`).
fn system_clock() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |d| d.as_secs_f64())
}

/// Inputs of one reserve call (Python keyword arguments).
#[derive(Debug, Clone)]
pub struct ReserveRequest {
    /// Requesting user.
    pub user_id: i64,
    /// Acting team.
    pub team_id: i64,
    /// Cycle index.
    pub cycle: i64,
    /// UTC day stamp `YYYYMMDD`.
    pub day: String,
    /// Credits to reserve (floored at 1).
    pub credits: i64,
    /// The user's guaranteed credits.
    pub guaranteed: i64,
    /// The team's shared buffer cap.
    pub buffer_cap: i64,
    /// The user's daily sub-cap (`None`/0 = none).
    pub daily_subcap: Option<i64>,
    /// Override rules enforced pre-dispatch.
    pub overrides: Vec<OverrideRule>,
}

/// Atomic reserve, reconcile, release and expiry sweep over Valkey Lua scripts.
pub struct QuotaLimiter {
    conn: MultiplexedConnection,
    reserve_script: Script,
    reconcile_script: Script,
    clock: Clock,
}

impl QuotaLimiter {
    /// Builds a limiter over a Valkey connection using the wall clock.
    pub fn new(conn: MultiplexedConnection) -> Self {
        Self::with_clock(conn, Arc::new(system_clock))
    }

    /// Builds a limiter with an injected clock (tests pass a fixed one).
    pub fn with_clock(conn: MultiplexedConnection, clock: Clock) -> Self {
        Self {
            conn,
            reserve_script: Script::new(LUA_RESERVE),
            reconcile_script: Script::new(LUA_RECONCILE),
            clock,
        }
    }

    /// Atomically reserves `credits` (reservation, then buffer, else deny); fails open on error.
    pub async fn reserve(&self, req: &ReserveRequest) -> QuotaDecision {
        let credits = req.credits.max(1);
        let rid = uuid::Uuid::new_v4().simple().to_string();
        let mut inv = self.reserve_script.prepare_invoke();
        inv.key(keys::user_counter(req.user_id, req.cycle))
            .key(keys::buffer_counter(req.team_id, req.cycle))
            .key(keys::daily_counter(req.user_id, &req.day))
            .key(keys::reservation(&rid))
            .key(keys::RESERVATION_INDEX);
        for rule in &req.overrides {
            inv.key(keys::override_counter(rule.id, req.cycle));
        }
        inv.arg(credits)
            .arg(req.guaranteed)
            .arg(req.buffer_cap)
            .arg(req.daily_subcap.unwrap_or(0))
            .arg(&rid)
            .arg((self.clock)())
            .arg(RESERVATION_TTL_S)
            .arg(RECORD_TTL_S)
            .arg(COUNTER_TTL_S)
            .arg(req.overrides.len())
            .arg(req.cycle);
        for rule in &req.overrides {
            // 0 = unlimited; never let an absent cap reach Lua.
            inv.arg(rule.cap_credits);
        }
        let mut conn = self.conn.clone();
        let parsed = match inv.invoke_async::<Value>(&mut conn).await {
            Ok(v) => parse_reserve(&v),
            Err(err) => {
                tracing::error!(error = %err, "quota reserve failed open");
                return QuotaDecision {
                    allowed: true,
                    degraded: true,
                    ..QuotaDecision::default()
                };
            }
        };
        match parsed {
            Some((true, _, from_resv, from_buf)) => QuotaDecision {
                allowed: true,
                reservation_id: Some(rid),
                from_reservation: from_resv,
                from_buffer: from_buf,
                ..QuotaDecision::default()
            },
            Some((false, reason, _, _)) => QuotaDecision {
                allowed: false,
                reason,
                ..QuotaDecision::default()
            },
            None => {
                tracing::error!("quota reserve returned an unparsable reply; failing open");
                QuotaDecision {
                    allowed: true,
                    degraded: true,
                    ..QuotaDecision::default()
                }
            }
        }
    }

    /// Settles a reservation against actual credits using ITS stored cycle/keys; never fails.
    ///
    /// Cap value 0 means unlimited. A missing record (settled/swept) is `applied: false`.
    pub async fn reconcile(
        &self,
        reservation_id: &str,
        actual_credits: i64,
        override_caps: &[(i64, i64)],
    ) -> ReconcileResult {
        let mut inv = self.reconcile_script.prepare_invoke();
        inv.key(keys::reservation(reservation_id))
            .key(keys::RESERVATION_INDEX)
            .arg(reservation_id)
            .arg(actual_credits.max(0))
            .arg(COUNTER_TTL_S)
            .arg(OVERRIDE_PREFIX)
            .arg(override_caps.len());
        for (id, cap) in override_caps {
            inv.arg(*id).arg(*cap);
        }
        let mut conn = self.conn.clone();
        match inv.invoke_async::<Value>(&mut conn).await {
            Ok(v) => parse_reconcile(&v).unwrap_or_else(|| {
                tracing::warn!("quota reconcile returned an unparsable reply");
                ReconcileResult::default()
            }),
            Err(err) => {
                tracing::warn!(error = %err, "quota reconcile failed");
                ReconcileResult::default()
            }
        }
    }

    /// Refunds a reservation in full (blocked request, cache hit, expired record).
    pub async fn release(&self, reservation_id: &str) -> ReconcileResult {
        self.reconcile(reservation_id, 0, &[]).await
    }

    /// Refunds up to `batch` reservations whose index score is in the past; returns the count.
    ///
    /// Backstop for reservations never reconciled/released, including a lost reserve reply.
    pub async fn sweep_expired(&self, now: Option<f64>, batch: usize) -> usize {
        let moment = now.unwrap_or_else(|| (self.clock)());
        let mut conn = self.conn.clone();
        let due: Vec<String> = match redis::cmd("ZRANGEBYSCORE")
            .arg(keys::RESERVATION_INDEX)
            .arg("-inf")
            .arg(moment)
            .arg("LIMIT")
            .arg(0)
            .arg(batch)
            .query_async(&mut conn)
            .await
        {
            Ok(v) => v,
            Err(err) => {
                tracing::warn!(error = %err, "quota sweep failed to read the index");
                return 0;
            }
        };
        let mut released = 0;
        for rid in due {
            if self.release(&rid).await.applied {
                released += 1;
            }
        }
        released
    }
}

/// Decodes a reply element to text (bulk or simple string).
fn text(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::BulkString(b)) => Some(String::from_utf8_lossy(b).into_owned()),
        Some(Value::SimpleString(s)) => Some(s.clone()),
        _ => None,
    }
}

/// Decodes an integer reply element.
fn int(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Int(n)) => Some(*n),
        _ => None,
    }
}

/// Parses `{allowed, reason, from_resv, from_buf}`; an empty reason is `None`.
fn parse_reserve(v: &Value) -> Option<(bool, Option<String>, i64, i64)> {
    let Value::Array(items) = v else { return None };
    let allowed = int(items.first())? != 0;
    let reason = text(items.get(1)).filter(|r| !r.is_empty());
    Some((allowed, reason, int(items.get(2))?, int(items.get(3))?))
}

/// Parses `{applied, from_resv, from_buf, overage, over_ids_csv}`.
fn parse_reconcile(v: &Value) -> Option<ReconcileResult> {
    let Value::Array(items) = v else { return None };
    if int(items.first())? == 0 {
        return Some(ReconcileResult::default());
    }
    let over = text(items.get(4)).unwrap_or_default();
    let ids: Option<Vec<i64>> = over
        .split(',')
        .filter(|s| !s.is_empty())
        .map(|s| s.parse().ok())
        .collect();
    Some(ReconcileResult {
        applied: true,
        from_reservation: int(items.get(1))?,
        from_buffer: int(items.get(2))?,
        overage: int(items.get(3))?,
        over_cap_override_ids: ids?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bulk(s: &str) -> Value {
        Value::BulkString(s.as_bytes().to_vec())
    }

    #[test]
    fn parse_reserve_shapes() {
        let ok = Value::Array(vec![Value::Int(1), bulk(""), Value::Int(5), Value::Int(2)]);
        assert_eq!(parse_reserve(&ok), Some((true, None, 5, 2)));
        let deny = Value::Array(vec![
            Value::Int(0),
            bulk("quota_exhausted"),
            Value::Int(0),
            Value::Int(0),
        ]);
        assert_eq!(
            parse_reserve(&deny),
            Some((false, Some("quota_exhausted".into()), 0, 0))
        );
        assert_eq!(parse_reserve(&Value::Nil), None);
        assert_eq!(parse_reserve(&Value::Array(vec![Value::Int(1)])), None);
    }

    #[test]
    fn parse_reconcile_shapes() {
        let applied = Value::Array(vec![
            Value::Int(1),
            Value::Int(3),
            Value::Int(4),
            Value::Int(1),
            bulk("7,9"),
        ]);
        let r = parse_reconcile(&applied).unwrap();
        assert!(r.applied);
        assert_eq!((r.from_reservation, r.from_buffer, r.overage), (3, 4, 1));
        assert_eq!(r.over_cap_override_ids, vec![7, 9]);
        let missing = Value::Array(vec![
            Value::Int(0),
            Value::Int(0),
            Value::Int(0),
            Value::Int(0),
            bulk(""),
        ]);
        assert_eq!(parse_reconcile(&missing), Some(ReconcileResult::default()));
        assert_eq!(parse_reconcile(&Value::Nil), None);
        let bad_ids = Value::Array(vec![
            Value::Int(1),
            Value::Int(0),
            Value::Int(0),
            Value::Int(0),
            bulk("x"),
        ]);
        assert_eq!(parse_reconcile(&bad_ids), None);
    }
}
