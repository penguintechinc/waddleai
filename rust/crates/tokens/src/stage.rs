//! `TokenBudgetStage`: reserves TPM / monthly budget before dispatch.
//!
//! Port of the Python `TokenBudgetStage`. The reservation id is stashed on the context for the
//! meter stage to reconcile; a denied reservation short-circuits the pipeline with HTTP 429.

use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{PipelineContext, Stage, StageError};

use crate::estimate::estimate_io_tokens;
use crate::limiter::{BudgetGate, KeyLimits};

/// The virtual-key budget subject the auth stage places in `ctx.extensions`. Absent means the
/// request has no virtual key (stage skips); `limits: None` means the key has no limits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VkeyBudget {
    /// Virtual key id the budget is charged against.
    pub vkey_id: i64,
    /// The key's configured limits, if any.
    pub limits: Option<KeyLimits>,
}

/// Pipeline stage that estimates request size and reserves it against the key's budgets.
pub struct TokenBudgetStage {
    gate: Arc<dyn BudgetGate>,
}

impl TokenBudgetStage {
    /// Builds the stage over a budget gate (the Valkey `TokenLimiter` in production).
    pub fn new(gate: Arc<dyn BudgetGate>) -> Self {
        Self { gate }
    }
}

#[async_trait]
impl Stage for TokenBudgetStage {
    /// Stable stage name used by the driver and the stage log.
    fn name(&self) -> &'static str {
        "token_budget"
    }

    /// Reserves estimated tokens; sets 429 + the decision reason on denial, otherwise stashes
    /// the reservation id. Skips silently without a virtual key or without limits.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let Some(budget) = ctx.extensions.get::<VkeyBudget>().cloned() else {
            tracing::debug!("token_budget: skipping (no vkey)");
            return Ok(());
        };
        let Some(limits) = budget.limits else {
            tracing::debug!(
                vkey_id = budget.vkey_id,
                "token_budget: no limits configured"
            );
            return Ok(());
        };
        let (input, output) = estimate_io_tokens(&ctx.messages);
        let total = input + output;
        let decision = self.gate.reserve(budget.vkey_id, total, 0.0, &limits).await;
        if !decision.allowed {
            ctx.blocked = true;
            ctx.status_code = 429;
            tracing::warn!(
                vkey_id = budget.vkey_id,
                reason = decision.reason.as_deref().unwrap_or(""),
                "token_budget: quota exceeded"
            );
            ctx.block_reason = decision.reason;
            return Ok(());
        }
        tracing::debug!(
            vkey_id = budget.vkey_id,
            reserved = total,
            "token_budget: reserved"
        );
        ctx.reservation_id = decision.reservation_id;
        Ok(())
    }
}
