//! Stage-0 model aliasing and provider-pin parsing (port of `shared/routing/aliases.py`).

use serde_json::Value;

/// Registered provider names; a leading segment is a provider only if it matches exactly.
pub const KNOWN_PROVIDERS: [&str; 9] = [
    "openai",
    "anthropic",
    "ollama",
    "llamacpp",
    "gemini",
    "bedrock",
    "azure_openai",
    "cohere",
    "xai",
];

/// An enabled `model_aliases` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AliasRow {
    /// Owning org, `None` = global.
    pub org: Option<i64>,
    /// Client-supplied model name.
    pub source_model: String,
    /// Redirect target.
    pub target_model: String,
    /// Optional provider pin.
    pub target_provider: Option<String>,
}

/// Result of provider-pin parsing plus aliasing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AliasResolution {
    /// Target (or bare) model.
    pub model: String,
    /// Pinned provider, if any.
    pub provider: Option<String>,
    /// Original string when an alias redirected.
    pub routed_from: Option<String>,
}

/// Splits a provider prefix on the FIRST colon, only when it is a registered provider, so
/// Ollama tags like `gemma4:e4b` stay whole.
pub fn split_provider_prefix(model: &str) -> (Option<&str>, &str) {
    if let Some((prefix, rest)) = model.split_once(':') {
        if KNOWN_PROVIDERS.contains(&prefix) {
            return (Some(prefix), rest);
        }
    }
    (None, model)
}

/// Cheapest explicit tool-type signal: header, else a `waddleai/<tool-type>` model alias.
pub fn explicit_tool_type(header: Option<&str>, model: Option<&str>) -> Option<String> {
    if let Some(h) = header.filter(|h| !h.is_empty()) {
        return Some(h.to_owned());
    }
    model
        .and_then(|m| m.strip_prefix("waddleai/"))
        .map(str::to_owned)
}

/// Resolves a client model: strip the provider prefix, then org alias over global alias.
pub fn resolve_alias(model: &str, org: i64, aliases: &[AliasRow]) -> AliasResolution {
    let (provider, bare) = split_provider_prefix(model);
    let row = aliases
        .iter()
        .find(|a| a.org == Some(org) && a.source_model == bare)
        .or_else(|| {
            aliases
                .iter()
                .find(|a| a.org.is_none() && a.source_model == bare)
        });
    match row {
        None => AliasResolution {
            model: bare.to_owned(),
            provider: provider.map(str::to_owned),
            routed_from: None,
        },
        Some(r) => AliasResolution {
            model: r.target_model.clone(),
            provider: r
                .target_provider
                .clone()
                .filter(|p| !p.is_empty())
                .or_else(|| provider.map(str::to_owned)),
            routed_from: Some(model.to_owned()),
        },
    }
}

/// `routed_from` metadata for an alias redirect.
pub fn alias_routed_from(from: &str, to: &str) -> Value {
    serde_json::json!({"cause": "alias", "from": from, "to": to})
}
