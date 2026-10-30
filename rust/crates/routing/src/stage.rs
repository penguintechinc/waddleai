//! `RoutingStage`: applies the routing decision to the pipeline context.

use std::sync::Arc;

use pipeline::{PipelineContext, Stage, StageError};

use crate::decide::{decide, RoutingRequest};
use crate::registry::RegistryCache;

/// PostHog flag gating smart routing (flag off = `ctx.model` left exactly as set upstream).
pub const SMART_ROUTING_FLAG: &str = "waddleai.smart_routing";

/// What routing decided beyond the context fields, for `DispatchStage` (stored in
/// `ctx.extensions`). P2 executes local models only, so commercial picks are listed here as
/// non-executable instead of being dropped from the decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutingMeta {
    /// Whether the chosen model can execute in P2.
    pub executable: bool,
    /// Commercial models present in the chosen model + chain.
    pub commercial_models: Vec<String>,
    /// Chain entries P2 can fail over to.
    pub executable_fallbacks: Vec<String>,
}

/// Stage 5: smart routing over the per-org registry cache.
pub struct RoutingStage {
    registry: Arc<RegistryCache>,
    use_heuristics: bool,
}

impl RoutingStage {
    /// Stage over a registry cache. Heuristic rules are off by default, as the Python proxy
    /// constructs its stage with no rules.
    pub fn new(registry: Arc<RegistryCache>) -> Self {
        Self {
            registry,
            use_heuristics: false,
        }
    }

    /// Enables the stage-1 `routing_rules_v2` heuristic cascade.
    pub fn with_heuristics(mut self, on: bool) -> Self {
        self.use_heuristics = on;
        self
    }
}

#[async_trait::async_trait]
impl Stage for RoutingStage {
    fn name(&self) -> &'static str {
        "routing"
    }

    fn flag(&self) -> Option<&'static str> {
        Some(SMART_ROUTING_FLAG)
    }

    /// Routes the request. Routing never blocks a request: a registry or decision failure is
    /// logged and `ctx.model` is left unchanged (Python parity).
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let org_id = ctx.user.as_ref().map_or(0, |u| i64::from(u.org_id));
        let snapshot = match self.registry.get(org_id).await {
            Ok(s) => s,
            Err(e) => {
                tracing::error!(error = %e, "routing: registry unavailable, model left unchanged");
                return Ok(());
            }
        };
        let decision = {
            let req = RoutingRequest {
                org_id,
                body: &ctx.body,
                model: ctx.model.as_deref(),
                tool_type_hint: ctx.explicit_tool_type_hint.as_deref(),
                escalate_hint: ctx.escalate_hint.as_deref(),
                pii_detected: ctx.pii_detected,
                use_heuristics: self.use_heuristics,
            };
            match decide(&req, &snapshot) {
                Ok(d) => d,
                Err(e) => {
                    tracing::error!(error = %e, "routing: decide failed, model left unchanged");
                    return Ok(());
                }
            }
        };
        if ctx.requested_model.is_none() {
            ctx.requested_model = ctx.model.clone();
        }
        ctx.model = Some(decision.model);
        ctx.fallback_chain = decision.fallback_chain;
        ctx.routed_from = decision.routed_from;
        if decision.provider.is_some() {
            ctx.provider = decision.provider;
        }
        ctx.extensions.insert(RoutingMeta {
            executable: decision.executable,
            commercial_models: decision.commercial_models,
            executable_fallbacks: decision.executable_fallbacks,
        });
        Ok(())
    }
}
