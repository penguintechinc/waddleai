//! The [`Stage`] trait and the typed [`StageError`] stages fail with.

use crate::context::PipelineContext;

/// A stage fault; the driver releases quota then propagates it to the caller.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum StageError {
    /// A path that is intentionally not implemented; fails loudly, never a silent no-op.
    #[error("unsupported: {0}")]
    Unsupported(&'static str),
    /// An upstream dependency failed.
    #[error("upstream: {0}")]
    Upstream(String),
    /// An internal invariant failed.
    #[error("internal: {0}")]
    Internal(String),
}

/// One step of the proxy pipeline (auth, budget, dispatch, ...), run in order by the driver.
/// Stages mutate the context and set `blocked` to short-circuit the remainder.
#[async_trait::async_trait]
pub trait Stage: Send + Sync {
    /// Stable stage name ("auth","token_budget","security_in","cache","routing","quota","dispatch","security_out","meter").
    fn name(&self) -> &'static str;
    /// Optional PostHog flag gating this stage (None = always run).
    fn flag(&self) -> Option<&'static str> {
        None
    }
    /// Executes the stage, mutating ctx; set ctx.blocked/status_code/block_reason to
    /// short-circuit. Err = stage fault (driver releases quota, propagates).
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError>;
}
