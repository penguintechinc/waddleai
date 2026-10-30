//! tiktoken-backed token counting, ported from `TokenManager.count_tokens`.
//!
//! The Python manager only ever builds `cl100k_base` encoders (gpt-4 / gpt-3.5-turbo are
//! both `cl100k_base`, and the default fallback encoder is gpt-3.5-turbo), so every
//! provider/model resolves to `cl100k_base`. That estimate-vs-real behaviour is preserved.

use tiktoken_rs::cl100k_base_singleton;

/// A tiktoken encoding the manager can select.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    /// `cl100k_base` (gpt-4, gpt-3.5-turbo, and the Python default fallback).
    Cl100kBase,
}

impl Encoding {
    /// The tiktoken name of this encoding, as reported by Python `encoder.name`.
    pub fn name(self) -> &'static str {
        match self {
            Encoding::Cl100kBase => "cl100k_base",
        }
    }
}

/// How a provider's tokens are counted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CountPath {
    /// Local tiktoken estimate.
    Tiktoken,
    /// Only the provider's own count API is authoritative (Anthropic / Gemini).
    ProviderNative,
}

/// Why a native count could not be produced locally.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum CountError {
    /// The provider counts tokens via its own API; tiktoken must not stand in for it.
    #[error("provider {0} counts tokens via its own API; no local tiktoken count")]
    ProviderNative(String),
}

/// Picks the encoding for a provider/model exactly as `TokenManager.__init__` wires it:
/// the explicit `openai` gpt-4 / gpt-3.5-turbo entries and the default are all cl100k.
pub fn encoding_for(provider: &str, model: &str) -> Encoding {
    // Deliberate oracle wiring: the explicit openai arm mirrors the Python encoder table and
    // the wildcard mirrors `default_encoder`; both are cl100k_base today.
    #[allow(clippy::match_same_arms)]
    match (provider, model) {
        ("openai", "gpt-4") | ("openai", "gpt-3.5-turbo") => Encoding::Cl100kBase,
        _ => Encoding::Cl100kBase,
    }
}

/// Classifies how a provider's tokens must be counted; Anthropic and Gemini use their
/// own count endpoints rather than tiktoken.
pub fn count_path(provider: &str) -> CountPath {
    match provider {
        "anthropic" | "gemini" => CountPath::ProviderNative,
        _ => CountPath::Tiktoken,
    }
}

/// Counts tokens like `TokenManager.count_tokens`: tiktoken `encode` with Python's default
/// `disallowed_special="all"`, so text containing a special-token literal (e.g.
/// `<|endoftext|>`) raises in Python and falls back to `len(text) // 4` (chars, not bytes).
pub fn count_tokens(text: &str, provider: &str, model: &str) -> u64 {
    let enc = match encoding_for(provider, model) {
        Encoding::Cl100kBase => cl100k_base_singleton(),
    };
    if enc.special_tokens().iter().any(|s| text.contains(s)) {
        return (text.chars().count() / 4) as u64;
    }
    enc.encode_ordinary(text).len() as u64
}

/// Local count for providers whose tiktoken estimate is authoritative; fails loudly for
/// Anthropic/Gemini instead of silently estimating with the wrong tokenizer.
pub fn count_tokens_native(text: &str, provider: &str, model: &str) -> Result<u64, CountError> {
    match count_path(provider) {
        CountPath::Tiktoken => Ok(count_tokens(text, provider, model)),
        CountPath::ProviderNative => Err(CountError::ProviderNative(provider.to_string())),
    }
}
