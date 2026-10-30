//! Quota value types, ported 1:1 from `shared/quota/types.py` (pooled-quota spec 5-7).

use std::collections::{HashMap, HashSet};

use chrono::NaiveDate;

/// Deny reason: the team buffer cannot cover the lease.
pub const REASON_EXHAUSTED: &str = "quota_exhausted";
/// Deny reason: the user's per-day sub-cap would be exceeded.
pub const REASON_DAILY: &str = "daily_subcap_exceeded";
/// Deny reason: an override cap would be exceeded.
pub const REASON_OVERRIDE: &str = "override_exceeded";

/// A hard cap on a user and/or model and/or provider (further-restrict only, spec D5).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OverrideRule {
    /// Override row id (names the Valkey counter).
    pub id: i64,
    /// Cap in credits; 0 means unlimited at the Lua layer.
    pub cap_credits: i64,
    /// Restrict to one user.
    pub user_id: Option<i64>,
    /// Restrict to one model.
    pub model: Option<String>,
    /// Restrict to one provider.
    pub provider: Option<String>,
}

impl OverrideRule {
    /// Best-effort pre-dispatch match (spec 7): a rule naming a provider matches only when
    /// `providers` (those serving the requested model) is exactly that provider; otherwise it
    /// is enforced at reconcile only.
    pub fn matches_reserve(
        &self,
        user_id: i64,
        model: Option<&str>,
        providers: &HashSet<String>,
    ) -> bool {
        if let Some(p) = &self.provider {
            if providers.len() != 1 || !providers.contains(p) {
                return false;
            }
        }
        if self.user_id.is_some_and(|u| u != user_id) {
            return false;
        }
        // Python compares `self.model != model` where `model` may be None; a rule that
        // names a model never matches a request with no model.
        !self.model.as_deref().is_some_and(|m| Some(m) != model)
    }

    /// Exact match against the actual provider:model once dispatch has happened.
    pub fn matches_actual(&self, user_id: i64, provider: &str, model: &str) -> bool {
        self.user_id.is_none_or(|u| u == user_id)
            && self.model.as_deref().is_none_or(|m| m == model)
            && self.provider.as_deref().is_none_or(|p| p == provider)
    }
}

/// A user's private guaranteed credits and optional per-day sub-cap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserReservation {
    /// The user.
    pub user_id: i64,
    /// Guaranteed credits per cycle.
    pub guaranteed_credits: i64,
    /// Optional per-day sub-cap.
    pub daily_subcap: Option<i64>,
}

/// An org's pool switch, cycle anchor, and override rules.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrgQuotaConfig {
    /// The org.
    pub org_id: i64,
    /// Pool enforcement switch.
    pub enabled: bool,
    /// UTC anchor date of the 30-day cycle; `None` means unconfigured.
    pub cycle_anchor_date: Option<NaiveDate>,
    /// Org pool size in credits.
    pub pool_credits: i64,
    /// Override rules, ordered by id.
    pub overrides: Vec<OverrideRule>,
}

/// A team's sub-pool, lendable buffer, and per-user reservations (keyed by user id).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TeamQuotaConfig {
    /// The team.
    pub team_id: i64,
    /// Owning org.
    pub org_id: i64,
    /// Team pool size in credits.
    pub pool_credits: i64,
    /// Shared (lendable) buffer in credits.
    pub shared_buffer_credits: i64,
    /// Per-user guaranteed reservations.
    pub reservations: HashMap<i64, UserReservation>,
}

/// Result of one atomic reserve; `degraded` means the limiter failed open.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct QuotaDecision {
    /// Whether the request may proceed.
    pub allowed: bool,
    /// Deny reason from the script.
    pub reason: Option<String>,
    /// Reservation id to settle later.
    pub reservation_id: Option<String>,
    /// Credits debited from the user's guaranteed reservation.
    pub from_reservation: i64,
    /// Credits leased from the team buffer.
    pub from_buffer: i64,
    /// True when Valkey/the script failed and the request was allowed anyway (D10).
    pub degraded: bool,
}

/// Result of reconciling/releasing one reservation.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReconcileResult {
    /// False when the record was already settled/swept or the call failed.
    pub applied: bool,
    /// Final credits against the user's reservation.
    pub from_reservation: i64,
    /// Final credits against the team buffer.
    pub from_buffer: i64,
    /// Buffer overage recorded by this reconcile.
    pub overage: i64,
    /// Override ids whose cap this reconcile exceeded.
    pub over_cap_override_ids: Vec<i64>,
}

/// What the proxy remembers about an admitted request until reconcile (carries the cycle).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reservation {
    /// Valkey reservation id (`uuid4().hex`).
    pub reservation_id: String,
    /// Owning org.
    pub org_id: i64,
    /// Acting team.
    pub team_id: i64,
    /// Requesting user.
    pub user_id: i64,
    /// Cycle index the reservation was taken in.
    pub cycle: i64,
    /// Credits reserved (estimate).
    pub credits_reserved: i64,
    /// Credits taken from the user's reservation.
    pub from_reservation: i64,
    /// Credits taken from the team buffer.
    pub from_buffer: i64,
    /// Model requested at reserve time.
    pub model_requested: Option<String>,
}

/// What the engine tells the stage: block with a reason, or proceed (reservation or no-op).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Outcome {
    /// True when the request must be refused with 429.
    pub blocked: bool,
    /// Deny reason when blocked.
    pub reason: Option<String>,
    /// The held reservation when admitted.
    pub reservation: Option<Reservation>,
}

impl Outcome {
    /// Proceed with no reservation (no-op, degraded or failed-open).
    pub fn proceed() -> Self {
        Self::default()
    }
}
