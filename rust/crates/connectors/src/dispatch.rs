//! `DispatchStage`: gates and runs upstream dispatch, mapping failures onto the context.

use std::sync::Arc;

use async_trait::async_trait;
use futures_core::stream::BoxStream;
use futures_util::StreamExt;
use pipeline::{
    AuthMode, DispatchError, Dispatcher, PipelineContext, Stage, StageError, StreamChunk,
};
use routing::{split_provider_prefix, RoutingMeta};

use crate::local_dispatcher::LOCAL_PROVIDERS;

/// What the gate decided for this request.
enum Gate {
    /// Run the dispatcher.
    Proceed,
    /// Do nothing (cache hit or already blocked).
    Skip,
}

/// Maps a dispatch failure onto the context exactly as Python `_record_dispatch_error` does.
/// Sets only status and a stable reason code; the upstream body is never available here.
pub fn record_dispatch_error(ctx: &mut PipelineContext, e: &DispatchError) {
    ctx.blocked = true;
    match e {
        DispatchError::Upstream { status, reason, .. } => {
            ctx.status_code = *status;
            ctx.block_reason = Some(reason.clone());
        }
        DispatchError::NoConnector(_) => {
            ctx.status_code = 500;
            ctx.block_reason = Some("no_connector".to_owned());
        }
        // Connector and any future variant: generic dispatch fault.
        _ => {
            ctx.status_code = 500;
            ctx.block_reason = Some("dispatch_error".to_owned());
        }
    }
}

/// Stage 7: dispatches to the upstream model. P2 reaches local runtimes only; commercial
/// picks, non-executable routing results and passthrough credentials fail loudly (P3).
pub struct DispatchStage {
    dispatcher: Arc<dyn Dispatcher>,
}

impl DispatchStage {
    /// Stage over a dispatcher (`LocalDispatcher` in P2; P3 swaps it, this impl is unchanged).
    pub fn new(dispatcher: Arc<dyn Dispatcher>) -> Self {
        Self { dispatcher }
    }

    /// Fail-loud and skip rules, in order: cache hit and blocked requests are never
    /// dispatched; passthrough, non-executable routing and commercial picks are `Unsupported`.
    /// `provider_hint` is advisory and never makes a request executable.
    fn gate(ctx: &PipelineContext) -> Result<Gate, StageError> {
        if ctx.cache_hit || ctx.blocked {
            return Ok(Gate::Skip);
        }
        if ctx.auth_mode == AuthMode::Passthrough {
            return Err(StageError::Unsupported("passthrough dispatch: P3"));
        }
        if let Some(meta) = ctx.extensions.get::<RoutingMeta>() {
            let picked_commercial = ctx
                .model
                .as_ref()
                .is_some_and(|m| meta.commercial_models.contains(m));
            if !meta.executable || picked_commercial {
                return Err(StageError::Unsupported("provider: P3"));
            }
        }
        if let Some((Some(p), _)) = ctx.model.as_deref().map(split_provider_prefix) {
            if !LOCAL_PROVIDERS.contains(&p) {
                return Err(StageError::Unsupported("provider: P3"));
            }
        }
        Ok(Gate::Proceed)
    }

    /// Streaming entry for the HTTP layer: runs the gate, then yields the dispatcher's stream
    /// (empty when skipped). The caller runs `security_out`/`meter` after it ends.
    pub fn stream<'a>(
        &'a self,
        ctx: &'a mut PipelineContext,
    ) -> Result<BoxStream<'a, Result<StreamChunk, DispatchError>>, StageError> {
        match Self::gate(ctx)? {
            Gate::Skip => Ok(futures_util::stream::empty().boxed()),
            Gate::Proceed => Ok(self.dispatcher.stream_dispatch(ctx)),
        }
    }
}

#[async_trait]
impl Stage for DispatchStage {
    fn name(&self) -> &'static str {
        "dispatch"
    }

    /// Dispatches (buffered, or accumulating a stream when `ctx.stream`). Upstream failures
    /// set `blocked`/`status_code`/`block_reason` and return `Ok`, like the Python stage.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        if matches!(Self::gate(ctx)?, Gate::Skip) {
            return Ok(());
        }
        if ctx.stream {
            let mut s = self.dispatcher.stream_dispatch(ctx);
            let mut failure = None;
            while let Some(item) = s.next().await {
                if let Err(e) = item {
                    failure = Some(e);
                }
            }
            drop(s);
            if let Some(e) = failure {
                record_dispatch_error(ctx, &e);
            }
        } else if let Err(e) = self.dispatcher.dispatch(ctx).await {
            record_dispatch_error(ctx, &e);
        }
        Ok(())
    }
}
