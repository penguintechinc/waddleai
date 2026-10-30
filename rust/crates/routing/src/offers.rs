//! Candidate offers, request requirements and the capability veto (port of
//! `shared/routing/{offers,capability,requirements}.py`).
//!
//! Offers are the model universe the engine matches hard requirements against; the veto keeps the
//! assigned model when it qualifies and otherwise re-routes to the best qualified candidate.

use serde_json::Value;

use crate::RoutingError;

/// Where a model runs; commercial offers appear in decisions but are not executable in P2.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Location {
    /// Ollama / llama.cpp, executed by this proxy.
    Local,
    /// A commercial provider (dispatch lands in P3).
    Commercial,
}

impl Location {
    /// Wire name, as in the Python `ModelOffer.location`.
    pub fn as_str(self) -> &'static str {
        match self {
            Location::Local => "local",
            Location::Commercial => "commercial",
        }
    }
}

/// One candidate model with its capability data.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelOffer {
    /// Registry model name.
    pub model_name: String,
    /// Capability score (the interim source never varies it: 3.0).
    pub capability_score: f64,
    /// Whether tool calls are supported.
    pub supports_tools: bool,
    /// Whether image input is supported.
    pub supports_vision: bool,
    /// Context window in tokens.
    pub context_window: i64,
    /// Mean per-provider cost per token.
    pub cost_per_token: f64,
    /// Local or commercial.
    pub location: Location,
    /// Whether the model is currently servable.
    pub available: bool,
}

/// A raw `model_configs` row, before offer derivation.
#[derive(Debug, Clone, PartialEq)]
pub struct RawModelConfig {
    /// `model_name`.
    pub model_name: String,
    /// `preferred_providers` JSON array.
    pub preferred_providers: Value,
    /// `cost_per_token` JSON map (provider -> cost).
    pub cost_per_token: Value,
    /// `context_length`.
    pub context_length: i64,
    /// `capabilities` JSON array.
    pub capabilities: Value,
    /// `enabled`.
    pub enabled: bool,
}

/// Python 3.12+ `sum()` of floats: Neumaier-compensated, not a naive left-to-right sum.
fn py_float_sum(values: &[f64]) -> f64 {
    let (mut total, mut comp) = (0.0_f64, 0.0_f64);
    for &x in values {
        let t = total + x;
        if x.is_finite() && t.is_finite() {
            if total.abs() >= x.abs() {
                comp += (total - t) + x;
            } else {
                comp += (x - t) + total;
            }
        }
        total = t;
    }
    if comp != 0.0 && comp.is_finite() {
        total += comp;
    }
    total
}

/// Mean of a cost map, summed like Python (compensated) in Postgres `jsonb` key order (length,
/// then bytes) so float ties match production. A non-numeric cost fails loudly (Python raises).
fn mean_cost(model: &str, map: &Value) -> Result<f64, RoutingError> {
    let Some(obj) = map.as_object() else {
        return Ok(0.0);
    };
    let mut entries: Vec<(&String, &Value)> = obj.iter().collect();
    entries.sort_by(|a, b| a.0.len().cmp(&b.0.len()).then_with(|| a.0.cmp(b.0)));
    let mut values = Vec::with_capacity(entries.len());
    for (_, v) in &entries {
        values.push(
            v.as_f64()
                .ok_or_else(|| RoutingError::BadCost(model.to_owned()))?,
        );
    }
    Ok(if values.is_empty() {
        0.0
    } else {
        py_float_sum(&values) / values.len() as f64
    })
}

/// Derives the offer universe from `model_configs` rows exactly as `RoutingStage._load_offers`:
/// location from `preferred_providers`, cost = mean of the cost map, vision from `capabilities`.
pub fn build_offers(rows: &[RawModelConfig]) -> Result<Vec<ModelOffer>, RoutingError> {
    rows.iter()
        .filter(|r| r.enabled)
        .map(|r| {
            let is_local = r.preferred_providers.as_array().is_some_and(|a| {
                a.iter()
                    .any(|p| matches!(p.as_str(), Some("ollama" | "llamacpp")))
            });
            let avg_cost = mean_cost(&r.model_name, &r.cost_per_token)?;
            let vision = r
                .capabilities
                .as_array()
                .is_some_and(|a| a.iter().any(|c| c.as_str() == Some("vision")));
            Ok(ModelOffer {
                model_name: r.model_name.clone(),
                capability_score: 3.0,
                supports_tools: true,
                supports_vision: vision,
                context_window: if r.context_length == 0 {
                    4096
                } else {
                    r.context_length
                },
                cost_per_token: avg_cost,
                location: if is_local {
                    Location::Local
                } else {
                    Location::Commercial
                },
                available: true,
            })
        })
        .collect()
}

/// Hard requirements derived from the request body alone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Requirements {
    /// Prompt tokens plus `max_tokens`.
    pub min_context: i64,
    /// Request supplies tools or an active `tool_choice`.
    pub needs_tools: bool,
    /// Request carries image content.
    pub needs_vision: bool,
}

/// Python truthiness of a JSON value.
pub(crate) fn py_truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python `str()` of a scalar JSON value (containers use their JSON text).
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Text of an OpenAI-style `content`: a string, or the newline-joined text parts of a list.
fn message_text(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(parts) => parts
            .iter()
            .filter(|p| p.get("type").and_then(Value::as_str) == Some("text"))
            .map(|p| p.get("text").map_or_else(String::new, py_str))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// tiktoken `cl100k_base` count of one text. Special-token literals are counted as plain text:
/// Python raises there and fails open, which a client could trigger; here it cannot error.
fn count_tokens(text: &str) -> i64 {
    tiktoken_rs::cl100k_base_singleton()
        .encode_ordinary(text)
        .len() as i64
}

/// `int(body.get("max_tokens") or 0)`, except that a value Python cannot coerce counts as 0
/// instead of raising (a client-triggerable fail-open in Python).
fn max_tokens(body: &Value) -> i64 {
    let Some(v) = body.get("max_tokens").filter(|v| py_truthy(v)) else {
        return 0;
    };
    match v {
        Value::Bool(_) => 1,
        Value::Number(n) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .unwrap_or(0),
        Value::String(s) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// Derives the requirements vector from a chat-completion-style body.
pub fn derive_requirements(body: &Value) -> Requirements {
    let empty = Vec::new();
    let messages = body
        .get("messages")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let mut tokens = 0;
    for m in messages.iter().filter(|m| m.is_object()) {
        let text = m.get("content").map_or_else(String::new, message_text);
        tokens += count_tokens(&text);
    }
    let needs_vision = messages.iter().any(|m| {
        m.get("content")
            .and_then(Value::as_array)
            .is_some_and(|parts| {
                parts.iter().any(|p| {
                    matches!(
                        p.get("type").and_then(Value::as_str),
                        Some("image_url" | "image" | "input_image")
                    )
                })
            })
    });
    let needs_tools = body.get("tools").is_some_and(py_truthy)
        || !matches!(body.get("tool_choice"), None | Some(Value::Null))
            && body.get("tool_choice").and_then(Value::as_str) != Some("none");
    Requirements {
        min_context: tokens + max_tokens(body),
        needs_tools,
        needs_vision,
    }
}

/// True when `offer` satisfies every hard requirement.
pub fn qualifies(offer: &ModelOffer, reqs: &Requirements) -> bool {
    offer.available
        && offer.context_window >= reqs.min_context
        && !(reqs.needs_tools && !offer.supports_tools)
        && !(reqs.needs_vision && !offer.supports_vision)
}

/// First failing hard requirement, as a machine-readable reason.
fn veto_reason(offer: &ModelOffer, reqs: &Requirements) -> &'static str {
    if !offer.available {
        "unavailable"
    } else if offer.context_window < reqs.min_context {
        "context_overflow"
    } else if reqs.needs_tools && !offer.supports_tools {
        "tools_unsupported"
    } else if reqs.needs_vision && !offer.supports_vision {
        "vision_unsupported"
    } else {
        "unqualified"
    }
}

/// Highest-capability qualified offer; ties keep the first (Python `max`).
fn best_candidate(offers: &[ModelOffer], reqs: &Requirements) -> Option<ModelOffer> {
    let mut best: Option<&ModelOffer> = None;
    for o in offers.iter().filter(|o| qualifies(o, reqs)) {
        if best.is_none_or(|b| o.capability_score > b.capability_score) {
            best = Some(o);
        }
    }
    best.cloned()
}

/// Keeps the assigned offer if it qualifies, else re-routes within `candidates`; returns the
/// chosen offer and the veto reason (`None` = assignment kept).
pub fn veto_and_reroute(
    assigned: Option<&ModelOffer>,
    candidates: &[ModelOffer],
    reqs: &Requirements,
) -> (Option<ModelOffer>, Option<&'static str>) {
    if let Some(a) = assigned.filter(|a| qualifies(a, reqs)) {
        return (Some(a.clone()), None);
    }
    let reason = assigned.map_or("no_assignment", |a| veto_reason(a, reqs));
    (best_candidate(candidates, reqs), Some(reason))
}
