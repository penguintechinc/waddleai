//! `QuotaEngine`: reserve-before / reconcile-after policy for the pooled quota (spec 6, 7).
//!
//! Port of `shared/quota/engine.py`. Every failure that could otherwise affect availability
//! fails OPEN (D10): the request is allowed, an ERROR is logged and a `fault` signal is
//! emitted so the bypass is visible. Reconcile and release never fail. Reserve estimates with
//! the REQUESTED model (provider unknown pre-dispatch); reconcile uses the actual
//! provider:model and is authoritative for override caps. A request is a strict no-op (zero
//! Valkey writes, never a 429) whenever the pool is off or unconfigured.

use std::sync::Arc;
use std::time::Instant;

use auth::UserContext;
use chrono::{NaiveDate, Utc};
use pipeline::FlagResolver;
use serde_json::Value;
use tokens::estimate_io_tokens;
use tracing::Instrument;

use crate::config::ConfigStore;
use crate::credits::CreditModel;
use crate::lease::{QuotaLimiter, ReserveRequest};
use crate::telemetry::QuotaTelemetry;
use crate::types::{
    Outcome, ReconcileResult, Reservation, REASON_DAILY, REASON_EXHAUSTED, REASON_OVERRIDE,
};

/// PostHog flag gating the pooled quota (default OFF, per-org rollout; distinct id = org id).
pub const POOLED_QUOTA_FLAG: &str = "waddleai.pooled_quota";

/// Days in one anchored quota cycle.
const CYCLE_DAYS: i64 = 30;

/// A quota backend fault the stage handles by failing open.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum QuotaError {
    /// The backend failed in a way that could not be absorbed internally.
    #[error("quota backend error: {0}")]
    Backend(String),
}

/// Scope of one durable ledger row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LedgerScope {
    /// A user's reservation consumption.
    User,
    /// A team's shared-buffer consumption.
    TeamBuffer,
    /// An override rule's consumption.
    Override,
}

/// Credits consumed by one scope in one cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LedgerEntry {
    /// Which counter family the entry belongs to.
    pub scope: LedgerScope,
    /// User, team or override id.
    pub scope_id: i64,
    /// Credits consumed.
    pub credits: i64,
}

/// A durable ledger write failed.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("quota ledger write failed: {0}")]
pub struct LedgerError(pub String);

/// Durable per-scope, per-cycle credit rollup (reporting, not enforcement).
#[async_trait::async_trait]
pub trait Ledger: Send + Sync {
    /// Accumulates `entries`; a failure is logged by the engine and never retried (not
    /// transactional, a retry would double-count).
    async fn add(
        &self,
        org_id: i64,
        cycle: i64,
        entries: &[LedgerEntry],
    ) -> Result<(), LedgerError>;
}

/// What the stage and guard need from the quota: reserve, settle, and the credit model.
#[async_trait::async_trait]
pub trait QuotaBackend: Send + Sync {
    /// Reserves credits for a request, or reports why it is blocked (or that nothing applies).
    async fn reserve(
        &self,
        user: &UserContext,
        model: Option<&str>,
        messages: &[Value],
    ) -> Result<Outcome, QuotaError>;
    /// Settles `reservation` against actual usage; never fails.
    async fn reconcile(
        &self,
        reservation: &Reservation,
        actual_credits: i64,
        provider: &str,
        model: &str,
        cache_hit: bool,
    ) -> ReconcileResult;
    /// Refunds a reservation in full; never fails.
    async fn release(&self, reservation: &Reservation);
    /// The credit model used for abort-time actual-credit calculation.
    fn credits(&self) -> &dyn CreditModel;
    /// Emits a fault signal for a visible, non-fatal bypass.
    fn fault(&self, kind: &str);
}

/// Orchestrates config lookup, credit estimate, limiter calls, ledger writes and telemetry.
pub struct QuotaEngine {
    limiter: Arc<QuotaLimiter>,
    config: Arc<dyn ConfigStore>,
    ledger: Arc<dyn Ledger>,
    credits: Arc<dyn CreditModel>,
    flags: Arc<dyn FlagResolver>,
    telemetry: QuotaTelemetry,
    today: Arc<dyn Fn() -> NaiveDate + Send + Sync>,
}

impl QuotaEngine {
    /// Binds collaborators; the date source defaults to the UTC calendar day.
    pub fn new(
        limiter: Arc<QuotaLimiter>,
        config: Arc<dyn ConfigStore>,
        ledger: Arc<dyn Ledger>,
        credits: Arc<dyn CreditModel>,
        flags: Arc<dyn FlagResolver>,
    ) -> Self {
        Self {
            limiter,
            config,
            ledger,
            credits,
            flags,
            telemetry: QuotaTelemetry::new(),
            today: Arc::new(|| Utc::now().date_naive()),
        }
    }

    /// Replaces the date source (tests pin the cycle and day stamp).
    pub fn with_today(mut self, today: Arc<dyn Fn() -> NaiveDate + Send + Sync>) -> Self {
        self.today = today;
        self
    }

    /// Records a no-op decision (nothing configured / not applicable) and proceeds.
    fn noop(&self, why: &str) -> Outcome {
        tracing::debug!(why, "quota noop");
        self.telemetry.decision("noop", Some(why));
        Outcome::proceed()
    }

    /// Reserves credits for a request, or reports why it is blocked (or that nothing applies).
    ///
    /// Total: nothing after the limiter grant can fail, so no lease is ever left dangling.
    pub async fn reserve(
        &self,
        user: &UserContext,
        model: Option<&str>,
        messages: &[Value],
    ) -> Outcome {
        let started = Instant::now();
        let span = tracing::info_span!(
            "quota.reserve",
            quota.blocked = tracing::field::Empty,
            quota.reserved = tracing::field::Empty
        );
        let outcome = self
            .reserve_inner(user, model, messages)
            .instrument(span.clone())
            .await;
        span.record("quota.blocked", outcome.blocked);
        span.record("quota.reserved", outcome.reservation.is_some());
        self.telemetry
            .reserve_seconds(started.elapsed().as_secs_f64());
        outcome
    }

    /// Policy body of [`QuotaEngine::reserve`].
    async fn reserve_inner(
        &self,
        user: &UserContext,
        model: Option<&str>,
        messages: &[Value],
    ) -> Outcome {
        let (user_id, org_id) = (i64::from(user.user_id), i64::from(user.org_id));
        let Some(team_id) = user.team_id.map(i64::from) else {
            return self.noop("teamless");
        };
        if !self
            .flags
            .enabled(POOLED_QUOTA_FLAG, Some(&org_id.to_string()))
            .await
        {
            return self.noop("flag_off");
        }
        let org_cfg = match self.config.get_org(org_id).await {
            Ok(Some(c)) if c.enabled && c.cycle_anchor_date.is_some() => c,
            Ok(_) => return self.noop("org_unconfigured"),
            Err(err) => return self.config_failed_open(&err),
        };
        let team_cfg = match self.config.get_team(team_id).await {
            Ok(Some(c)) if c.org_id == org_id => c,
            Ok(_) => return self.noop("team_unconfigured"),
            Err(err) => return self.config_failed_open(&err),
        };
        let Some(anchor) = org_cfg.cycle_anchor_date else {
            return self.noop("org_unconfigured");
        };
        let today = (self.today)();
        if today < anchor {
            return self.noop("cycle_not_started");
        }
        let cycle = (today - anchor).num_days().div_euclid(CYCLE_DAYS);

        self.credits.refresh_if_stale().await;
        let (in_tokens, out_tokens) = estimate_io_tokens(messages);
        let model_name = model.unwrap_or("");
        let credits = to_i64(
            self.credits
                .estimate_credits(model_name, in_tokens, out_tokens),
        )
        .max(1);
        let providers = self.credits.providers_for_model(model_name);
        let reservation = team_cfg.reservations.get(&user_id);
        let decision = self
            .limiter
            .reserve(&ReserveRequest {
                user_id,
                team_id,
                cycle,
                day: today.format("%Y%m%d").to_string(),
                credits,
                guaranteed: reservation.map_or(0, |r| r.guaranteed_credits.max(0)),
                buffer_cap: team_cfg.shared_buffer_credits.max(0),
                daily_subcap: reservation.and_then(|r| r.daily_subcap),
                overrides: org_cfg
                    .overrides
                    .iter()
                    .filter(|o| o.matches_reserve(user_id, model, &providers))
                    .cloned()
                    .collect(),
            })
            .await;
        if !decision.allowed && !decision.degraded {
            let reason = decision
                .reason
                .as_deref()
                .filter(|r| [REASON_EXHAUSTED, REASON_DAILY, REASON_OVERRIDE].contains(r))
                .unwrap_or("unknown");
            self.telemetry.rejection(reason);
            self.telemetry.decision("denied", Some(reason));
            tracing::info!(reason, cycle, credits, "quota denied");
            return Outcome {
                blocked: true,
                reason: decision.reason,
                reservation: None,
            };
        }
        let Some(rid) = decision.reservation_id.filter(|_| !decision.degraded) else {
            self.telemetry.fault("limiter_degraded");
            self.telemetry.decision("degraded", Some("limiter_error"));
            return Outcome::proceed();
        };
        self.telemetry.decision("allowed", None);
        self.telemetry.reserved(credits);
        if decision.from_buffer > 0 {
            self.telemetry.lease(decision.from_buffer);
        }
        Outcome {
            blocked: false,
            reason: None,
            reservation: Some(Reservation {
                reservation_id: rid,
                org_id,
                team_id,
                user_id,
                cycle,
                credits_reserved: credits,
                from_reservation: decision.from_reservation,
                from_buffer: decision.from_buffer,
                model_requested: model.map(str::to_owned),
            }),
        }
    }

    /// Config could not be loaded: fail open, visibly (ERROR log + fault + degraded decision).
    fn config_failed_open(&self, err: &crate::config::ConfigError) -> Outcome {
        tracing::error!(error = %err, "quota config unavailable; failing open");
        self.telemetry.fault("config_error");
        self.telemetry.decision("degraded", Some("config_error"));
        Outcome::proceed()
    }

    /// Settles `res` against actual usage; never fails.
    pub async fn reconcile(
        &self,
        res: &Reservation,
        actual_credits: i64,
        provider: &str,
        model: &str,
        cache_hit: bool,
    ) -> ReconcileResult {
        let actual = if cache_hit || provider == "cache" {
            0
        } else {
            actual_credits.max(0)
        };
        let caps = if actual > 0 {
            self.override_caps(res, provider, model).await
        } else {
            Vec::new()
        };
        let cap_pairs: Vec<(i64, i64)> = caps.iter().map(|o| (o.id, o.cap_credits)).collect();
        let result = self
            .limiter
            .reconcile(&res.reservation_id, actual, &cap_pairs)
            .await;
        self.telemetry.reconciled(result.applied, actual);
        if !result.applied {
            tracing::warn!(
                "quota reconcile found no reservation (expired or swept); estimate stands"
            );
            return result;
        }
        let mut entries = vec![
            LedgerEntry {
                scope: LedgerScope::User,
                scope_id: res.user_id,
                credits: result.from_reservation,
            },
            LedgerEntry {
                scope: LedgerScope::TeamBuffer,
                scope_id: res.team_id,
                credits: result.from_buffer,
            },
        ];
        entries.extend(cap_pairs.iter().map(|(id, _)| LedgerEntry {
            scope: LedgerScope::Override,
            scope_id: *id,
            credits: actual,
        }));
        entries.retain(|e| e.credits > 0);
        if !entries.is_empty() {
            if let Err(err) = self.ledger.add(res.org_id, res.cycle, &entries).await {
                // Never retry the whole add: it is not transactional, a retry double-counts.
                tracing::error!(error = %err, "quota ledger write failed");
                self.telemetry.fault("ledger_write");
            }
        }
        if result.overage > 0 {
            self.telemetry.overage("buffer");
            tracing::warn!(overage = result.overage, "quota buffer overage recorded");
        }
        if !result.over_cap_override_ids.is_empty() {
            self.telemetry.overage("override");
            tracing::warn!(
                rules = result.over_cap_override_ids.len(),
                "quota override cap exceeded"
            );
        }
        result
    }

    /// Overrides matching the ACTUAL provider:model (empty when config is down; best-effort).
    async fn override_caps(
        &self,
        res: &Reservation,
        provider: &str,
        model: &str,
    ) -> Vec<crate::types::OverrideRule> {
        match self.config.get_org(res.org_id).await {
            Ok(Some(cfg)) => cfg
                .overrides
                .into_iter()
                .filter(|o| o.matches_actual(res.user_id, provider, model))
                .collect(),
            Ok(None) => Vec::new(),
            Err(err) => {
                tracing::error!(error = %err, "quota override caps skipped (config unavailable)");
                self.telemetry.fault("config_error");
                Vec::new()
            }
        }
    }

    /// Refunds a reservation in full (request blocked after reserve); never fails.
    pub async fn release(&self, res: &Reservation) {
        let result = self.limiter.release(&res.reservation_id).await;
        self.telemetry.reconciled(result.applied, 0);
    }
}

/// Saturating `u64 -> i64` (credit estimates never approach the bound in practice).
fn to_i64(v: u64) -> i64 {
    i64::try_from(v).unwrap_or(i64::MAX)
}

#[async_trait::async_trait]
impl QuotaBackend for QuotaEngine {
    async fn reserve(
        &self,
        user: &UserContext,
        model: Option<&str>,
        messages: &[Value],
    ) -> Result<Outcome, QuotaError> {
        Ok(QuotaEngine::reserve(self, user, model, messages).await)
    }

    async fn reconcile(
        &self,
        reservation: &Reservation,
        actual_credits: i64,
        provider: &str,
        model: &str,
        cache_hit: bool,
    ) -> ReconcileResult {
        QuotaEngine::reconcile(
            self,
            reservation,
            actual_credits,
            provider,
            model,
            cache_hit,
        )
        .await
    }

    async fn release(&self, reservation: &Reservation) {
        QuotaEngine::release(self, reservation).await;
    }

    fn credits(&self) -> &dyn CreditModel {
        self.credits.as_ref()
    }

    fn fault(&self, kind: &str) {
        self.telemetry.fault(kind);
    }
}
