//! Tool-type cascade: stage 0 explicit, stage 1 heuristic rules (port of
//! `shared/routing/{heuristics,tool_type}.py`). Stage 2 (classifier) is not part of P2: a punt
//! returns the safe default `general`, exactly Python's behaviour with no classifier client.

use serde_json::Value;

use crate::offers::py_truthy;

/// Fallback tool type when no stage resolves one (kebab-case vocabulary).
pub const DEFAULT_TOOL_TYPE: &str = "general";

/// A stage-1 rule (`routing_rules_v2`).
#[derive(Debug, Clone, PartialEq)]
pub struct HeuristicRule {
    /// Lower number fires first.
    pub priority: i64,
    /// Match predicate map.
    pub match_: Value,
    /// Action map; `tool_type` is the field the cascade consumes.
    pub action: Value,
}

/// Cheap request signals; the proxy stage supplies only the model.
#[derive(Debug, Clone, Default)]
pub struct RequestSignals {
    /// Requested model (empty when unknown).
    pub model: String,
}

/// True when every recognised predicate holds; unknown keys are ignored and an empty or
/// non-object match never fires.
fn rule_matches(m: &Value, s: &RequestSignals) -> bool {
    let Some(obj) = m.as_object().filter(|o| !o.is_empty()) else {
        return false;
    };
    obj.iter().all(|(k, v)| match k.as_str() {
        // The proxy stage supplies no tool names, so this predicate can never hold.
        "tool_name_present" => false,
        "endpoint" => v.as_str().is_some_and(|e| e.is_empty()),
        "has_image" => v.as_bool() == Some(false),
        "model_prefix" => v.as_str().is_some_and(|p| s.model.starts_with(p)),
        _ => true,
    })
}

/// Resolves the tool type: explicit, else the first matching rule's action, else the default.
pub fn determine_tool_type(
    explicit: Option<&str>,
    signals: &RequestSignals,
    rules: &[HeuristicRule],
) -> String {
    if let Some(e) = explicit.filter(|e| !e.is_empty()) {
        return e.to_owned();
    }
    let mut sorted: Vec<&HeuristicRule> = rules.iter().collect();
    sorted.sort_by_key(|r| r.priority);
    if let Some(rule) = sorted
        .into_iter()
        .find(|r| rule_matches(&r.match_, signals))
    {
        if let Some(tt) = rule.action.get("tool_type").filter(|v| py_truthy(v)) {
            if let Some(s) = tt.as_str() {
                return s.to_owned();
            }
        }
    }
    DEFAULT_TOOL_TYPE.to_owned()
}
