//! [`ProxyPipeline`]: ordered stage driver, a port of Python `ProxyPipeline._run_stage`.

use std::sync::Arc;

use tracing::Instrument;

use crate::context::PipelineContext;
use crate::flags::FlagResolver;
use crate::stage::{Stage, StageError};

/// Runs stages in order with flag-gating, blocked short-circuit, tracing and
/// exactly-once quota release. Cheap to share behind an `Arc`.
pub struct ProxyPipeline {
    stages: Vec<Arc<dyn Stage>>,
    flags: Arc<dyn FlagResolver>,
}

impl ProxyPipeline {
    /// Builds a pipeline over ordered stages and a flag resolver.
    pub fn new(stages: Vec<Arc<dyn Stage>>, flags: Arc<dyn FlagResolver>) -> Self {
        Self { stages, flags }
    }

    /// Runs every stage in order, short-circuiting once `ctx.blocked` is set.
    pub async fn run(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let span = tracing::info_span!("pipeline");
        async {
            for stage in &self.stages {
                self.run_stage(ctx, stage.as_ref()).await?;
            }
            Ok(())
        }
        .instrument(span)
        .await
    }

    /// Runs stages strictly before `stage` (exclusive). Unknown names fail loudly.
    pub async fn run_until(
        &self,
        ctx: &mut PipelineContext,
        stage: &str,
    ) -> Result<(), StageError> {
        let idx = self.position(stage)?;
        let span = tracing::info_span!("pipeline_head");
        async {
            for s in &self.stages[..idx] {
                self.run_stage(ctx, s.as_ref()).await?;
            }
            Ok(())
        }
        .instrument(span)
        .await
    }

    /// Runs stages strictly after `stage` (exclusive); a blocked ctx is refunded first.
    /// Unknown names fail loudly.
    pub async fn run_after(
        &self,
        ctx: &mut PipelineContext,
        stage: &str,
    ) -> Result<(), StageError> {
        let idx = self.position(stage)?;
        if ctx.blocked {
            // Streaming blocks/errors set blocked outside run_stage; refund before the
            // short-circuit loop so the reservation is not left to the sweeper.
            release_quota(ctx).await;
        }
        let span = tracing::info_span!("pipeline_tail");
        async {
            for s in &self.stages[idx + 1..] {
                self.run_stage(ctx, s.as_ref()).await?;
            }
            Ok(())
        }
        .instrument(span)
        .await
    }

    /// Index of a named stage, or an `Internal` error naming it.
    fn position(&self, name: &str) -> Result<usize, StageError> {
        self.stages
            .iter()
            .position(|s| s.name() == name)
            .ok_or_else(|| StageError::Internal(format!("unknown stage: {name}")))
    }

    /// One stage: flag skip (evaluated before the blocked check), blocked
    /// short-circuit, traced call, quota release on error or block.
    async fn run_stage(
        &self,
        ctx: &mut PipelineContext,
        stage: &dyn Stage,
    ) -> Result<(), StageError> {
        let name = stage.name();
        if let Some(flag) = stage.flag() {
            let distinct_id = ctx.user.as_ref().map(|u| u.user_id.to_string());
            if !self.flags.enabled(flag, distinct_id.as_deref()).await {
                ctx.stage_log.push(format!("skipped:{name}"));
                return Ok(());
            }
        }

        if ctx.blocked {
            release_quota(ctx).await;
            ctx.stage_log.push(format!("short-circuit:{name}"));
            return Ok(());
        }

        let span = tracing::info_span!(
            "stage",
            stage = name,
            error = tracing::field::Empty,
            "error.message" = tracing::field::Empty,
            "gen_ai.system" = tracing::field::Empty,
            "gen_ai.request.model" = tracing::field::Empty,
            "gen_ai.response.model" = tracing::field::Empty,
            "gen_ai.usage.input_tokens" = tracing::field::Empty,
            "gen_ai.usage.output_tokens" = tracing::field::Empty,
            "gen_ai.response.finish_reason" = tracing::field::Empty,
        );
        match stage.call(ctx).instrument(span.clone()).await {
            Ok(()) => {
                ctx.stage_log.push(format!("ran:{name}"));
                if name == "dispatch" {
                    set_genai_attributes(&span, ctx);
                }
            }
            Err(e) => {
                tracing::error!(stage = name, error = %e, "stage failed");
                span.record("error", true);
                span.record("error.message", e.to_string().as_str());
                release_quota(ctx).await;
                return Err(e);
            }
        }

        if ctx.blocked {
            release_quota(ctx).await;
        }
        Ok(())
    }
}

/// Takes and settles the quota guard exactly once, shielded from cancellation of the
/// caller. A failing settlement is logged, never raised.
async fn release_quota(ctx: &mut PipelineContext) {
    ctx.quota_reservation = None;
    let Some(guard) = ctx.quota_release.take() else {
        return;
    };
    // The settlement runs as its own task: aborting/dropping the awaiting task detaches
    // the JoinHandle but the settle still runs to completion (mirrors settle_shielded).
    let handle = tokio::spawn(guard.release());
    match handle.await {
        Ok(Ok(())) => {}
        Ok(Err(e)) => tracing::error!(error = %e, "quota release failed"),
        Err(e) => tracing::error!(error = %e, "quota release task failed"),
    }
}

/// Attaches GenAI semantic-convention attributes to the dispatch span: model
/// identifiers and token counts only, never prompt/completion content or credentials.
fn set_genai_attributes(span: &tracing::Span, ctx: &PipelineContext) {
    if let Some(p) = ctx.provider.as_deref().filter(|s| !s.is_empty()) {
        span.record("gen_ai.system", p);
    }
    if let Some(m) = ctx
        .requested_model
        .as_deref()
        .or(ctx.model.as_deref())
        .filter(|s| !s.is_empty())
    {
        span.record("gen_ai.request.model", m);
    }
    if let Some(m) = ctx.model.as_deref().filter(|s| !s.is_empty()) {
        span.record("gen_ai.response.model", m);
    }
    if let Some(u) = &ctx.usage {
        span.record("gen_ai.usage.input_tokens", u.input_tokens);
        span.record("gen_ai.usage.output_tokens", u.output_tokens);
    }
    if let Some(f) = ctx.finish_reason.as_deref().filter(|s| !s.is_empty()) {
        span.record("gen_ai.response.finish_reason", f);
    }
}
