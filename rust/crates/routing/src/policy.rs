//! Org routing policy: filter + mode-sort = the fallback chain, plus the sensitivity clamp
//! (port of `shared/routing/{policy,sensitivity}.py`).

use crate::offers::{Location, ModelOffer};

/// Resolved org routing policy (defaults when no `routing_policies` row exists).
#[derive(Debug, Clone, PartialEq)]
pub struct RoutingPolicy {
    /// `local_only`, `local_first`, `commercial_only`, `cost`, `latency`.
    pub mode: String,
    /// Complexity threshold for escalation.
    pub escalation_threshold: i64,
    /// Org-level escalation target model.
    pub escalation_target: Option<String>,
    /// `local_only`, `redact_then_any` or `ignore`.
    pub sensitivity_routing: String,
}

impl Default for RoutingPolicy {
    fn default() -> Self {
        Self {
            mode: "local_first".into(),
            escalation_threshold: 3,
            escalation_target: None,
            sensitivity_routing: "local_only".into(),
        }
    }
}

const VALID_MODES: [&str; 5] = [
    "local_only",
    "local_first",
    "commercial_only",
    "cost",
    "latency",
];

/// Sort rank tuple for a mode; unknown modes order like `local_first`.
fn sort_key(mode: &str, o: &ModelOffer) -> (u8, f64) {
    let local_rank = u8::from(o.location != Location::Local);
    match mode {
        "cost" | "commercial_only" | "local_only" => (0, o.cost_per_token),
        // Latency data is never supplied by the proxy stage: every key is +inf, order is kept.
        "latency" => (0, 0.0),
        _ => (local_rank, o.cost_per_token),
    }
}

/// Applies the policy mode filter, then sorts (stable) by the mode's ranking.
pub fn filter_and_sort(candidates: &[ModelOffer], policy: &RoutingPolicy) -> Vec<ModelOffer> {
    let mode = if VALID_MODES.contains(&policy.mode.as_str()) {
        policy.mode.as_str()
    } else {
        "local_first"
    };
    let mut out: Vec<ModelOffer> = candidates
        .iter()
        .filter(|o| match mode {
            "local_only" => o.location == Location::Local,
            "commercial_only" => o.location == Location::Commercial,
            _ => true,
        })
        .cloned()
        .collect();
    out.sort_by(|a, b| {
        let (ka, kb) = (sort_key(mode, a), sort_key(mode, b));
        ka.0.cmp(&kb.0)
            .then(ka.1.partial_cmp(&kb.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    out
}

/// PII clamp: with PII present, `local_only` (also any unknown mode) drops every external
/// candidate; `redact_then_any` and `ignore` leave the chain unchanged.
pub fn apply_sensitivity(chain: Vec<ModelOffer>, pii: bool, org_mode: &str) -> Vec<ModelOffer> {
    if !pii || org_mode == "ignore" {
        return chain;
    }
    if org_mode == "redact_then_any" {
        return chain;
    }
    chain
        .into_iter()
        .filter(|o| o.location == Location::Local)
        .collect()
}
