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

/// Derives the offer universe from `model_configs` rows exactly as `RoutingStage._load_offers`:
/// location from `preferred_providers`, cost = mean of the cost map, vision from `capabilities`.
pub fn build_offers(rows: &[RawModelConfig]) -> Vec<ModelOffer> {
    rows.iter()
        .filter(|r| r.enabled)
        .map(|r| {
            let is_local = r.preferred_providers.as_array().is_some_and(|a| {
                a.iter()
                    .any(|p| matches!(p.as_str(), Some("ollama" | "llamacpp")))
            });
            let costs: Vec<f64> = r
                .cost_per_token
                .as_object()
                .map(|m| m.values().filter_map(Value::as_f64).collect())
                .unwrap_or_default();
            let avg_cost = if costs.is_empty() {
                0.0
            } else {
                costs.iter().sum::<f64>() / costs.len() as f64
            };
            let vision = r
                .capabilities
                .as_array()
                .is_some_and(|a| a.iter().any(|c| c.as_str() == Some("vision")));
            ModelOffer {
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
            }
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

/// tiktoken `cl100k_base` count of one text; a special-token literal is an error exactly as
/// Python's default `disallowed_special="all"` (the engine then leaves the model unchanged).
fn count_tokens(text: &str) -> Result<i64, RoutingError> {
    let enc = tiktoken_rs::cl100k_base_singleton();
    if enc.special_tokens().iter().any(|s| text.contains(s)) {
        return Err(RoutingError::Tokenize);
    }
    Ok(enc.encode_ordinary(text).len() as i64)
}

/// `int(body.get("max_tokens") or 0)`.
fn max_tokens(body: &Value) -> Result<i64, RoutingError> {
    let Some(v) = body.get("max_tokens").filter(|v| py_truthy(v)) else {
        return Ok(0);
    };
    match v {
        Value::Bool(_) => Ok(1),
        Value::Number(n) => n
            .as_i64()
            .or_else(|| {
                n.as_f64()
                    .filter(|f| f.is_finite())
                    .map(|f| f.trunc() as i64)
            })
            .ok_or(RoutingError::BadMaxTokens),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .map_err(|_| RoutingError::BadMaxTokens),
        _ => Err(RoutingError::BadMaxTokens),
    }
}

/// Derives the requirements vector from a chat-completion-style body.
pub fn derive_requirements(body: &Value) -> Result<Requirements, RoutingError> {
    let empty = Vec::new();
    let messages = body
        .get("messages")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let mut tokens = 0;
    for m in messages.iter().filter(|m| m.is_object()) {
        let text = m.get("content").map_or_else(String::new, message_text);
        tokens += count_tokens(&text)?;
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
    Ok(Requirements {
        min_context: tokens + max_tokens(body)?,
        needs_tools,
        needs_vision,
    })
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
fn best_candidate<'a>(offers: &'a [ModelOffer], reqs: &Requirements) -> Option<&'a ModelOffer> {
    let mut best: Option<&ModelOffer> = None;
    for o in offers.iter().filter(|o| qualifies(o, reqs)) {
        if best.is_none_or(|b| o.capability_score > b.capability_score) {
            best = Some(o);
        }
    }
    best
}

/// Keeps the assigned offer if it qualifies, else re-routes; returns the chosen offer and the
/// veto reason (`None` = assignment kept).
pub fn veto_and_reroute<'a>(
    assigned: Option<&'a ModelOffer>,
    offers: &'a [ModelOffer],
    reqs: &Requirements,
) -> (Option<&'a ModelOffer>, Option<&'static str>) {
    if let Some(a) = assigned.filter(|a| qualifies(a, reqs)) {
        return (Some(a), None);
    }
    let reason = assigned.map_or("no_assignment", |a| veto_reason(a, reqs));
    (best_candidate(offers, reqs), Some(reason))
}
