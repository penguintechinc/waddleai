//! `RoutingStage`: applies the routing decision to the pipeline context.

use std::sync::Arc;

use opentelemetry::global;
use opentelemetry::metrics::Counter;
use pipeline::{PipelineContext, Stage, StageError};

use crate::decide::{decide, RoutingRequest};
use crate::registry::RegistryCache;

/// PostHog flag gating smart routing (flag off = `ctx.model` left exactly as set upstream).
pub const SMART_ROUTING_FLAG: &str = "waddleai.smart_routing";

/// What routing decided beyond the context fields, for `DispatchStage` (stored in
/// `ctx.extensions`). P2 executes local models only, so commercial and unknown picks are listed
/// here as non-executable instead of being dropped from the decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutingMeta {
    /// Whether the chosen model is a known local model (the only kind P2 can execute).
    pub executable: bool,
    /// Models in the chosen model + chain that are commercial or unknown to the registry.
    pub commercial_models: Vec<String>,
    /// Chain entries P2 can fail over to.
    pub executable_fallbacks: Vec<String>,
    /// Advisory provider pin (`provider:model` prefix / alias target); never a dispatch result.
    pub provider_hint: Option<String>,
    /// The tool type fell to the unclassified default (no classifier in P2).
    pub unclassified_default: bool,
}

/// Blocks the request fail-closed (data-residency): never routes around the policy.
fn block_residency(ctx: &mut PipelineContext) {
    ctx.blocked = true;
    ctx.status_code = 503;
    ctx.block_reason = Some("No local model is available for the data-residency policy".to_owned());
}

/// Stage 5: smart routing over the per-org registry cache.
pub struct RoutingStage {
    registry: Arc<RegistryCache>,
    use_heuristics: bool,
    unclassified: Counter<u64>,
}

impl RoutingStage {
    /// Stage over a registry cache. Heuristic rules are off by default, as the Python proxy
    /// constructs its stage with no rules.
    pub fn new(registry: Arc<RegistryCache>) -> Self {
        let unclassified = global::meter("waddleai-routing")
            .u64_counter("routing_classifier_unavailable_total")
            .with_unit("1")
            .with_description("Requests routed on the unclassified default (no classifier in P2)")
            .build();
        Self {
            registry,
            use_heuristics: false,
            unclassified,
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

    /// Routes the request. A registry outage leaves `ctx.model` unchanged (Python parity) unless
    /// PII is present, whose default policy is `local_only`: then the request is blocked. A
    /// residency-clamp decision failure always blocks (fail closed).
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let org_id = ctx.user.as_ref().map_or(0, |u| i64::from(u.org_id));
        let snapshot = match self.registry.get(org_id).await {
            Ok(s) => s,
            Err(e) => {
                if ctx.pii_detected {
                    tracing::error!(error = %e, "routing: registry unavailable with PII; blocking");
                    block_residency(ctx);
                } else {
                    tracing::error!(error = %e, "routing: registry unavailable, model unchanged");
                }
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
                    tracing::error!(error = %e, "routing: residency clamp has no local model; blocking");
                    block_residency(ctx);
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
        if decision.unclassified_default {
            self.unclassified.add(1, &[]);
            tracing::debug!("routing: tool type fell to the unclassified default (no classifier)");
        }
        ctx.extensions.insert(RoutingMeta {
            executable: decision.executable,
            commercial_models: decision.commercial_models,
            executable_fallbacks: decision.executable_fallbacks,
            provider_hint: decision.provider,
            unclassified_default: decision.unclassified_default,
        });
        Ok(())
    }
}
