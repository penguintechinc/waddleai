//! Prompt-injection scanner: a behavior port of Python `PromptSecurityScanner`.
//!
//! Detection only (threats + block decision); sanitization stays with the caller
//! until a later task needs it. Scanned message content is never logged.

use crate::patterns::THREAT_PATTERNS;
use regex::{Regex, RegexBuilder};
use serde_json::Value;
use std::sync::OnceLock;

/// Failure compiling an exported pattern into the `regex` crate.
#[derive(Debug, thiserror::Error)]
pub enum SecurityError {
    /// A pattern failed to compile; carries its index and the engine message.
    #[error("pattern {index} failed to compile: {message}")]
    Compile {
        /// Position in `THREAT_PATTERNS`.
        index: usize,
        /// Engine error text.
        message: String,
    },
}

/// Category of detected threat (mirrors Python `ThreatType` string values).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ThreatType {
    /// Instruction override / guard spoofing.
    PromptInjection,
    /// Persona and restriction-bypass attempts.
    Jailbreak,
    /// Attempts to read the system prompt.
    DataExtraction,
    /// Chat-template control tokens.
    SystemPromptLeak,
    /// Secrets and credential patterns.
    CredentialHarvesting,
}

impl ThreatType {
    /// Python-compatible wire string.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::PromptInjection => "prompt_injection",
            Self::Jailbreak => "jailbreak",
            Self::DataExtraction => "data_extraction",
            Self::SystemPromptLeak => "system_prompt_leak",
            Self::CredentialHarvesting => "credential_harvesting",
        }
    }

    /// Parse the Python wire string.
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "prompt_injection" => Self::PromptInjection,
            "jailbreak" => Self::Jailbreak,
            "data_extraction" => Self::DataExtraction,
            "system_prompt_leak" => Self::SystemPromptLeak,
            "credential_harvesting" => Self::CredentialHarvesting,
            _ => return None,
        })
    }

    fn base_severity(self) -> Severity {
        match self {
            Self::PromptInjection | Self::DataExtraction => Severity::High,
            Self::Jailbreak => Severity::Medium,
            Self::SystemPromptLeak | Self::CredentialHarvesting => Severity::Critical,
        }
    }
}

/// Threat severity (mirrors Python `Severity`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// Low.
    Low,
    /// Medium.
    Medium,
    /// High.
    High,
    /// Critical.
    Critical,
}

impl Severity {
    /// Parse the Python wire string.
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "low" => Self::Low,
            "medium" => Self::Medium,
            "high" => Self::High,
            "critical" => Self::Critical,
            _ => return None,
        })
    }
}

/// Response action for a threat (mirrors Python `Action`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// Log only.
    Log,
    /// Sanitize the content.
    Sanitize,
    /// Block the request.
    Block,
    /// Rate-limit the caller.
    RateLimit,
}

impl Action {
    /// Parse the Python wire string.
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "log" => Self::Log,
            "sanitize" => Self::Sanitize,
            "block" => Self::Block,
            "rate_limit" => Self::RateLimit,
            _ => return None,
        })
    }
}

/// One detected threat for a message.
#[derive(Debug, Clone, PartialEq)]
pub struct Threat {
    /// Threat category.
    pub threat_type: ThreatType,
    /// Severity after match-count escalation.
    pub severity: Severity,
    /// `min(1, matches / 5)`.
    pub confidence: f64,
    /// First five matched strings (or `prompt_too_long`).
    pub matched_patterns: Vec<String>,
    /// Policy action for this threat type.
    pub suggested_action: Action,
}

/// Security policy (strict / balanced / permissive), same constants as Python.
#[derive(Debug, Clone)]
pub struct Policy {
    /// Max prompt length in characters.
    pub max_prompt_length: usize,
    /// Minimum matches per threat type to report it.
    pub suspicious_pattern_threshold: usize,
    actions: [Action; 5],
}

impl Policy {
    /// Look up a policy by name; unknown names fall back to `balanced` like Python.
    pub fn named(name: &str) -> Self {
        use Action::{Block, Log, Sanitize};
        // Order: injection, jailbreak, extraction, prompt-leak, credentials.
        match name {
            "strict" => Self::new(10_000, 1, [Block; 5]),
            "permissive" => Self::new(100_000, 3, [Sanitize, Log, Sanitize, Log, Block]),
            _ => Self::new(50_000, 2, [Block, Sanitize, Block, Sanitize, Block]),
        }
    }

    fn new(max: usize, thr: usize, actions: [Action; 5]) -> Self {
        Self {
            max_prompt_length: max,
            suspicious_pattern_threshold: thr,
            actions,
        }
    }

    /// Action configured for a threat type.
    pub fn action_for(&self, t: ThreatType) -> Action {
        self.actions[idx(t)]
    }
}

fn idx(t: ThreatType) -> usize {
    match t {
        ThreatType::PromptInjection => 0,
        ThreatType::Jailbreak => 1,
        ThreatType::DataExtraction => 2,
        ThreatType::SystemPromptLeak => 3,
        ThreatType::CredentialHarvesting => 4,
    }
}

/// Translate a Python `re` source into `regex`-crate syntax.
///
/// Python `\s` also matches U+001C..U+001F, and a literal `{` not starting a
/// quantifier is legal in Python but a syntax error here; both are rewritten.
fn translate(src: &str) -> String {
    let mut out = String::with_capacity(src.len() + 8);
    let chars: Vec<char> = src.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() {
            if chars[i + 1] == 's' {
                out.push_str("[\\s\\x1c-\\x1f]");
            } else {
                out.push(c);
                out.push(chars[i + 1]);
            }
            i += 2;
            continue;
        }
        if c == '{' && !chars.get(i + 1).is_some_and(|n| n.is_ascii_digit()) {
            out.push_str("\\{");
        } else {
            out.push(c);
        }
        i += 1;
    }
    out
}

/// Compile every exported pattern (IGNORECASE | MULTILINE | DOTALL), in table order.
pub fn compile_all() -> Result<Vec<Regex>, SecurityError> {
    THREAT_PATTERNS
        .iter()
        .enumerate()
        .map(|(index, (_, src))| {
            RegexBuilder::new(&translate(src))
                .case_insensitive(true)
                .multi_line(true)
                .dot_matches_new_line(true)
                .build()
                .map_err(|e| SecurityError::Compile {
                    index,
                    message: e.to_string(),
                })
        })
        .collect()
}

fn compiled() -> &'static [Regex] {
    static CELL: OnceLock<Vec<Regex>> = OnceLock::new();
    CELL.get_or_init(|| match compile_all() {
        Ok(v) => v,
        // Static, test-verified patterns: reaching this means the build is broken.
        // An empty table is treated as fail-closed by `scan_prompt`.
        Err(e) => {
            tracing::error!(error = %e, "threat pattern compile failed");
            Vec::new()
        }
    })
}

/// Scanner bound to a [`Policy`].
#[derive(Debug, Clone)]
pub struct PromptScanner {
    policy: Policy,
}

impl PromptScanner {
    /// Build a scanner; fails loudly via [`PromptScanner::try_new`] if patterns are broken.
    pub fn new(policy: Policy) -> Self {
        Self { policy }
    }

    /// Like `new` but verifies the pattern table compiled completely.
    pub fn try_new(policy: Policy) -> Result<Self, SecurityError> {
        compile_all()?;
        Ok(Self { policy })
    }

    /// Scan each message's `content` string; returns all threats in message order.
    pub fn scan_messages(&self, messages: &[Value]) -> Vec<Threat> {
        messages
            .iter()
            .flat_map(|m| self.scan_prompt(m.get("content").and_then(Value::as_str).unwrap_or("")))
            .collect()
    }

    /// True if any threat's action is `Block` (Python `should_block`).
    pub fn should_block(threats: &[Threat]) -> bool {
        threats.iter().any(|t| t.suggested_action == Action::Block)
    }

    fn scan_prompt(&self, prompt: &str) -> Vec<Threat> {
        if prompt.chars().count() > self.policy.max_prompt_length {
            return vec![Threat {
                threat_type: ThreatType::PromptInjection,
                severity: Severity::Medium,
                confidence: 1.0,
                matched_patterns: vec!["prompt_too_long".to_string()],
                suggested_action: Action::Block,
            }];
        }
        let regexes = compiled();
        if regexes.len() != THREAT_PATTERNS.len() {
            // Fail closed: never wave traffic through on a broken pattern table.
            return vec![Threat {
                threat_type: ThreatType::PromptInjection,
                severity: Severity::Critical,
                confidence: 1.0,
                matched_patterns: vec!["pattern_table_unavailable".to_string()],
                suggested_action: Action::Block,
            }];
        }
        let mut per: [Vec<String>; 5] = Default::default();
        let mut counts = [0usize; 5];
        for ((t, _), re) in THREAT_PATTERNS.iter().zip(regexes) {
            for m in re.find_iter(prompt) {
                let i = idx(*t);
                counts[i] += 1;
                if per[i].len() < 5 {
                    per[i].push(m.as_str().to_string());
                }
            }
        }
        let order = [
            ThreatType::PromptInjection,
            ThreatType::Jailbreak,
            ThreatType::DataExtraction,
            ThreatType::SystemPromptLeak,
            ThreatType::CredentialHarvesting,
        ];
        let mut out = Vec::new();
        for t in order {
            let n = counts[idx(t)];
            if n >= self.policy.suspicious_pattern_threshold && n > 0 {
                out.push(Threat {
                    threat_type: t,
                    severity: severity_for(t, n),
                    confidence: (n as f64 / 5.0).min(1.0),
                    matched_patterns: std::mem::take(&mut per[idx(t)]),
                    suggested_action: self.policy.action_for(t),
                });
            }
        }
        out
    }
}

fn severity_for(t: ThreatType, n: usize) -> Severity {
    let base = t.base_severity();
    if n >= 5 {
        return match base {
            Severity::Low => Severity::Medium,
            Severity::Medium => Severity::High,
            Severity::High => Severity::Critical,
            Severity::Critical => Severity::Critical,
        };
    }
    base
}
