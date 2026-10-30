//! The routing decision: tool type -> assignment -> alias -> capability veto -> policy ->
//! escalation -> sensitivity clamp -> final pick (port of `RoutingEngine.decide`).
//!
//! Pure and synchronous over an [`OrgRouting`] snapshot, so it never holds a lock across an await.

use serde_json::{json, Value};

use crate::alias::{alias_routed_from, explicit_tool_type, resolve_alias};
use crate::cascade::{determine_tool_type, RequestSignals, ToolTypeSource};
use crate::escalate::{escalation_target, should_escalate};
use crate::offers::{derive_requirements, veto_and_reroute, Location, ModelOffer};
use crate::policy::{apply_sensitivity, filter_and_sort};
use crate::registry::OrgRouting;
use crate::RoutingError;

/// Per-request inputs the stage reads from the pipeline context.
#[derive(Debug, Clone)]
pub struct RoutingRequest<'a> {
    /// Authenticated org (0 when unknown).
    pub org_id: i64,
    /// Request body (messages, tools, max_tokens).
    pub body: &'a Value,
    /// Model currently in `ctx.model`.
    pub model: Option<&'a str>,
    /// `X-WaddleAI-Tool-Type` header value (kebab-case).
    pub tool_type_hint: Option<&'a str>,
    /// `X-WaddleAI-Escalate` hint.
    pub escalate_hint: Option<&'a str>,
    /// SecurityIn flagged PII.
    pub pii_detected: bool,
    /// Whether to consult `routing_rules_v2` heuristics (the Python proxy loads none).
    pub use_heuristics: bool,
}

/// The routing outcome. Commercial picks stay in the decision for parity but are marked
/// non-executable: P2 dispatches local models only.
#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    /// Chosen model.
    pub model: String,
    /// Policy-ordered failover chain, excluding `model`.
    pub fallback_chain: Vec<String>,
    /// Transparency metadata (alias / capability_veto / escalation).
    pub routed_from: Option<Value>,
    /// Advisory provider pin from an explicit prefix or alias target (not a dispatch result).
    pub provider: Option<String>,
    /// Whether `model` can execute in P2 (local).
    pub executable: bool,
    /// Models in the decision (chosen + chain) that are commercial.
    pub commercial_models: Vec<String>,
    /// Chain entries P2 can actually fail over to (local only).
    pub executable_fallbacks: Vec<String>,
    /// No classifier ran and no hint/rule resolved a tool type: the unclassified default was used.
    pub unclassified_default: bool,
}

/// True when `name` is a known registry model served locally.
fn is_local(offers: &[ModelOffer], name: &str) -> bool {
    offers
        .iter()
        .any(|o| o.model_name == name && o.location == Location::Local)
}

/// Runs the full decision for one request against an org snapshot.
///
/// Data residency (R20): when the PII clamp or a `local_only` policy applies, the FINAL model
/// must be a known local model. Escalation targets, veto candidates and every fallback are held
/// to that, so neither a client escalate hint nor a capability veto can leave the clamp. If no
/// local model is available the decision fails closed with [`RoutingError::NoLocalCandidate`].
pub fn decide(req: &RoutingRequest<'_>, org: &OrgRouting) -> Result<Decision, RoutingError> {
    let model_signal = req.model.unwrap_or("");
    let explicit = explicit_tool_type(req.tool_type_hint, req.model);
    let rules: &[_] = if req.use_heuristics { &org.rules } else { &[] };
    let tool = determine_tool_type(
        explicit.as_deref(),
        &RequestSignals {
            model: model_signal.to_owned(),
        },
        rules,
    );
    let assignment = org.assignment(&tool.tool_type, req.org_id);
    let reqs = derive_requirements(req.body);

    let pii_clamp = req.pii_detected
        && !matches!(
            org.policy.sensitivity_routing.as_str(),
            "ignore" | "redact_then_any"
        );
    let clamp = pii_clamp || org.policy.mode == "local_only";

    let mut assigned_model = assignment.map(|a| a.default_model.clone());
    let mut alias_rf: Option<Value> = None;
    let mut provider: Option<String> = None;
    if let Some(m) = req
        .model
        .filter(|m| !m.is_empty() && !m.starts_with("waddleai/"))
    {
        let res = resolve_alias(m, req.org_id, &org.aliases);
        provider = res.provider.clone();
        if let Some(from) = res.routed_from.as_deref() {
            alias_rf = Some(alias_routed_from(from, &res.model));
            assigned_model = Some(res.model);
        }
    }
    let assigned_offer = assigned_model
        .as_deref()
        .and_then(|n| org.offers.iter().find(|o| o.model_name == n));
    // A veto re-routes only within the clamped (local) universe when the clamp applies.
    let veto_universe: Vec<ModelOffer> = if clamp {
        org.offers
            .iter()
            .filter(|o| o.location == Location::Local)
            .cloned()
            .collect()
    } else {
        org.offers.clone()
    };
    let (chosen, veto) = veto_and_reroute(assigned_offer, &veto_universe, &reqs);

    let qualified = filter_and_sort(&org.offers, &org.policy);
    let esc = should_escalate(None, org.policy.escalation_threshold, req.escalate_hint);
    let chain = if req.pii_detected {
        apply_sensitivity(qualified, true, &org.policy.sensitivity_routing)
    } else {
        qualified
    };

    let mut routed_from = alias_rf;
    let final_model = pick_final(
        &Pick {
            chosen: chosen.as_ref(),
            chain: &chain,
            offers: &org.offers,
            assignment: assignment
                .map(|a| (a.default_model.as_str(), a.escalation_model.as_deref())),
            escalate: esc.escalate,
            trigger: esc.trigger,
            policy_target: org.policy.escalation_target.as_deref(),
            veto,
            clamp,
        },
        &mut routed_from,
    )?;
    let fallback_chain: Vec<String> = chain
        .iter()
        .filter(|o| o.model_name != final_model && (!clamp || o.location == Location::Local))
        .map(|o| o.model_name.clone())
        .collect();

    let location_of = |name: &str| {
        org.offers
            .iter()
            .find(|o| o.model_name == name)
            .map(|o| o.location)
    };
    // An unknown final model (an unlisted escalation target, the last-resort default) is never
    // executable: dispatch must fail loudly rather than call a local runtime with its name.
    let executable = location_of(&final_model) == Some(Location::Local);
    let mut commercial_models: Vec<String> = Vec::new();
    for n in std::iter::once(&final_model).chain(fallback_chain.iter()) {
        if location_of(n) != Some(Location::Local) {
            commercial_models.push(n.clone());
        }
    }
    let executable_fallbacks = fallback_chain
        .iter()
        .filter(|n| location_of(n) == Some(Location::Local))
        .cloned()
        .collect();
    tracing::debug!(model = %final_model, chain = fallback_chain.len(), executable, "routing decision");
    Ok(Decision {
        model: final_model,
        fallback_chain,
        routed_from,
        provider,
        executable,
        commercial_models,
        executable_fallbacks,
        unclassified_default: tool.source == ToolTypeSource::DefaultNoClassifier,
    })
}

/// Inputs of the final selection.
struct Pick<'a> {
    chosen: Option<&'a ModelOffer>,
    chain: &'a [ModelOffer],
    offers: &'a [ModelOffer],
    assignment: Option<(&'a str, Option<&'a str>)>,
    escalate: bool,
    trigger: Option<&'static str>,
    policy_target: Option<&'a str>,
    veto: Option<&'static str>,
    clamp: bool,
}

/// Selects the final model; escalation outranks a capability veto, which outranks an alias.
/// Under the residency clamp every candidate must be a known local model.
fn pick_final(p: &Pick<'_>, routed_from: &mut Option<Value>) -> Result<String, RoutingError> {
    let ok = |name: &str| !p.clamp || is_local(p.offers, name);
    if p.escalate {
        if let Some(t) =
            escalation_target(p.assignment.and_then(|a| a.1), p.policy_target).filter(|t| ok(t))
        {
            *routed_from = Some(json!({"cause": "escalation", "trigger": p.trigger}));
            return Ok(t);
        }
    }
    if let (Some(reason), Some(c)) = (p.veto.filter(|r| *r != "no_assignment"), p.chosen) {
        if ok(&c.model_name) {
            *routed_from = Some(json!({"cause": "capability_veto", "reason": reason}));
            return Ok(c.model_name.clone());
        }
    }
    if let Some(c) = p
        .chosen
        .filter(|c| ok(&c.model_name) && p.chain.iter().any(|o| o.model_name == c.model_name))
    {
        return Ok(c.model_name.clone());
    }
    if let Some(first) = p.chain.iter().find(|o| ok(&o.model_name)) {
        return Ok(first.model_name.clone());
    }
    if let Some(c) = p.chosen.filter(|c| ok(&c.model_name)) {
        return Ok(c.model_name.clone());
    }
    if let Some((default, _)) = p.assignment.filter(|(d, _)| ok(d)) {
        return Ok(default.to_owned());
    }
    if p.clamp {
        return Err(RoutingError::NoLocalCandidate);
    }
    Ok("gpt-4".to_owned())
}
