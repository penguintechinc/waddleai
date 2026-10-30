//! `QuotaStage`: reserve pooled-quota credits just before dispatch (spec 6, D7, D9).
//!
//! Placed after the cache stage (a hit is free and must never 429) and routing (the
//! alias-resolved model is the one estimated). The flag is resolved with
//! `distinct_id = org_id`. A deny is a 429 with a stable, user-safe message; an admit stores
//! the reservation plus a [`QuotaGuard`] so a later block/error settles it. The mode follows
//! `ctx.auth_mode`: Virtual debits credits; Passthrough never debits (the user pays the
//! provider directly), leaving rate limiting to the mode-agnostic budget stage.

use std::sync::Arc;

use pipeline::{AuthMode, FlagResolver, PipelineContext, Stage, StageError};

use crate::engine::{QuotaBackend, POOLED_QUOTA_FLAG};
use crate::guard::QuotaGuard;

/// User-safe 429 message for a known deny reason.
fn block_message(reason: Option<&str>) -> &'static str {
    match reason {
        Some("quota_exhausted") => "Token quota exhausted for the current billing cycle",
        Some("daily_subcap_exceeded") => "Daily token limit reached",
        Some("override_exceeded") => "Token limit reached for this user, model or provider",
        _ => "Token quota exceeded",
    }
}

/// Reserves credits via the quota backend; blocks with 429 when the quota is exhausted.
pub struct QuotaStage {
    engine: Option<Arc<dyn QuotaBackend>>,
    flags: Arc<dyn FlagResolver>,
}

impl QuotaStage {
    /// Binds the (possibly absent) backend and the flag resolver for the org flag.
    pub fn new(engine: Option<Arc<dyn QuotaBackend>>, flags: Arc<dyn FlagResolver>) -> Self {
        Self { engine, flags }
    }
}

#[async_trait::async_trait]
impl Stage for QuotaStage {
    fn name(&self) -> &'static str {
        "quota"
    }

    /// Reserves for this request, or passes through when quota does not apply. Fail-open: a
    /// backend error is logged at error level and the request proceeds.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let org_id = ctx.user.as_ref().map(|u| u.org_id);
        let (Some(engine), Some(org_id)) = (self.engine.as_ref(), org_id) else {
            return Ok(());
        };
        if ctx.cache_hit {
            return Ok(());
        }
        if ctx.auth_mode == AuthMode::Passthrough {
            tracing::debug!("quota: passthrough is rate-limit-only; no credit debit");
            return Ok(());
        }
        if !self
            .flags
            .enabled(POOLED_QUOTA_FLAG, Some(&org_id.to_string()))
            .await
        {
            return Ok(());
        }
        let Some(user) = ctx.user.as_ref() else {
            return Ok(());
        };
        let outcome = match engine
            .reserve(user, ctx.model.as_deref(), &ctx.messages)
            .await
        {
            Ok(o) => o,
            Err(err) => {
                tracing::error!(error = %err, "quota stage failed open");
                return Ok(());
            }
        };
        if outcome.blocked {
            ctx.blocked = true;
            ctx.status_code = 429;
            ctx.block_reason = Some(block_message(outcome.reason.as_deref()).to_owned());
            return Ok(());
        }
        if let Some(res) = outcome.reservation {
            ctx.quota_reservation = Some(pipeline::QuotaReservation {
                id: res.reservation_id.clone(),
                tokens: u64::try_from(res.credits_reserved).unwrap_or(0),
            });
            ctx.quota_release = Some(Box::new(QuotaGuard::new(Arc::clone(engine), res.clone())));
            // The full reservation (cycle, org, team, split) stays available to the meter
            // stage's reconcile through the typed extension map.
            ctx.extensions.insert(res);
        }
        Ok(())
    }
}
