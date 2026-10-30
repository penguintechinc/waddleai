//! WaddleAI smart routing: the pure decision logic ported from `shared/routing/*` and the
//! `RoutingStage` that applies it, over a Valkey-invalidated per-org registry cache.

mod alias;
mod cascade;
mod decide;
mod escalate;
mod offers;
mod policy;
mod registry;
mod source;
mod stage;

pub use alias::{
    explicit_tool_type, resolve_alias, split_provider_prefix, AliasResolution, AliasRow,
    KNOWN_PROVIDERS,
};
pub use cascade::{determine_tool_type, HeuristicRule, RequestSignals, DEFAULT_TOOL_TYPE};
pub use decide::{decide, Decision, RoutingRequest};
pub use escalate::{escalation_target, should_escalate, EscalationDecision};
pub use offers::{
    build_offers, derive_requirements, qualifies, veto_and_reroute, Location, ModelOffer,
    RawModelConfig, Requirements,
};
pub use policy::{apply_sensitivity, filter_and_sort, RoutingPolicy};
pub use registry::{
    spawn_invalidation_listener, AssignmentRow, OrgRouting, RegistryCache, RegistryError,
    RegistrySource, SeedReport, ASSIGN_KEY_PREFIX, L1_MAX_AGE, POLICY_KEY_PREFIX,
};
pub use source::DbRegistrySource;
pub use stage::{RoutingMeta, RoutingStage, SMART_ROUTING_FLAG};

/// A routing-decision fault; the stage logs it and leaves `ctx.model` unchanged.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RoutingError {
    /// A message contains a tokenizer special-token literal (Python raises the same).
    #[error("message text contains a tokenizer special token")]
    Tokenize,
    /// `max_tokens` is not coercible to an integer.
    #[error("max_tokens is not an integer")]
    BadMaxTokens,
}
