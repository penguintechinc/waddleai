//! Security for the WaddleAI proxy: prompt scanning and the PII content filter.

mod builtins;
pub mod content_filter;
pub mod ner;
pub mod patterns;
pub mod prompt;
pub mod rules;

pub use builtins::BUILTIN_PATTERNS;
pub use content_filter::{
    BuildError, ContentFilter, FailMode, FailModes, FilterAction, FilterResult, FilterViolation,
    Phase, RuleAction, RuleKind,
};
pub use ner::{
    content_hash, DenyLicense, FlagsLicenseGate, LicenseGate, NerTier, MIN_NER_CONFIDENCE,
    NER_TIER_FLAG, NER_TIER_LICENSE_FEATURE,
};
pub use prompt::{Action, Policy, PromptScanner, SecurityError, Severity, Threat, ThreatType};
pub use rules::{
    spawn_invalidation_listener, DbRulesSource, FilterRule, KvCache, OrgConfig, RawOrgConfig,
    RulesError, RulesSource, RulesStore, SeedReport,
};
