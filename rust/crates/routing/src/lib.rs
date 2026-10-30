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
pub use cascade::{
    determine_tool_type, HeuristicRule, RequestSignals, ToolTypeDecision, ToolTypeSource,
    DEFAULT_TOOL_TYPE,
};
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

/// A routing fault: the residency clamp found no local model (the stage blocks), or a model
/// row is malformed (the registry load fails loudly).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RoutingError {
    /// The data-residency clamp (PII or `local_only`) left no local model to route to.
    #[error("no local model satisfies the data-residency policy")]
    NoLocalCandidate,
    /// A `cost_per_token` map holds a non-numeric value (Python raises while loading offers).
    #[error("model {0}: cost_per_token has a non-numeric value")]
    BadCost(String),
}
