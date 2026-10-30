//! Prompt-security scanning for the WaddleAI proxy.

pub mod patterns;
pub mod prompt;

pub use prompt::{Action, Policy, PromptScanner, SecurityError, Severity, Threat, ThreatType};
