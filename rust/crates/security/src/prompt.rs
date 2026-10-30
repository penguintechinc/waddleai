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
    /// Message content in a shape the scanner cannot inspect (fail-closed).
    UnsupportedContent,
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
            Self::UnsupportedContent => "unsupported_content_shape",
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
            "unsupported_content_shape" => Self::UnsupportedContent,
            _ => return None,
        })
    }

    fn base_severity(self) -> Severity {
        match self {
            Self::PromptInjection | Self::DataExtraction => Severity::High,
            Self::Jailbreak => Severity::Medium,
            Self::SystemPromptLeak | Self::CredentialHarvesting | Self::UnsupportedContent => {
                Severity::Critical
            }
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
        if t == ThreatType::UnsupportedContent {
            return Action::Block;
        }
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
        // Never indexed: `action_for` short-circuits and no pattern carries this type.
        ThreatType::UnsupportedContent => 0,
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

/// Whether pattern `index` (table order) matches `text`, with Python-case folding applied.
pub fn pattern_matches(index: usize, text: &str) -> bool {
    let folded = fold_python_case(text);
    compiled()
        .get(index)
        .is_some_and(|r| r.is_match(folded.as_deref().unwrap_or(text)))
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

    /// Scan every text-bearing field of each message; returns all threats in order.
    ///
    /// Strings are scanned directly; content-part arrays are scanned part by part
    /// (`text`, nested `content`, tool `input`/`arguments`); tool-call arguments on
    /// the message are scanned too. Missing/null/unknown content shapes fail closed
    /// with a Block threat, mirroring Python (which raises on non-string content).
    pub fn scan_messages(&self, messages: &[Value]) -> Vec<Threat> {
        let mut out = Vec::new();
        for m in messages {
            let mut texts: Vec<&str> = Vec::new();
            let mut ok = collect_content(m.get("content"), &mut texts);
            ok &= collect_tool_calls(m, &mut texts);
            if !ok {
                out.push(self.unsupported());
            }
            for t in texts {
                out.extend(self.scan_prompt(t));
            }
        }
        out
    }

    fn unsupported(&self) -> Threat {
        Threat {
            threat_type: ThreatType::UnsupportedContent,
            severity: Severity::Critical,
            confidence: 1.0,
            matched_patterns: vec!["unsupported_content_shape".to_string()],
            suggested_action: Action::Block,
        }
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
        let folded = fold_python_case(prompt);
        let hay = folded.as_deref().unwrap_or(prompt);
        for ((t, _), re) in THREAT_PATTERNS.iter().zip(regexes) {
            for m in re.find_iter(hay) {
                let i = idx(*t);
                counts[i] += 1;
                if per[i].len() < 5 {
                    per[i].push(original_slice(prompt, hay, m.start(), m.end()));
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

/// Map the code points Python's IGNORECASE equates with ASCII `i` but the regex
/// crate's simple case folding does not (U+0130, U+0131) onto `i`, 1:1 by char.
///
/// Python also equates U+212A with `k` and U+017F with `s`; the regex crate already
/// folds those, so they need no mapping. Returns `None` when nothing changes.
fn fold_python_case(s: &str) -> Option<String> {
    if !s.contains(['\u{130}', '\u{131}']) {
        return None;
    }
    Some(
        s.chars()
            .map(|c| {
                if c == '\u{130}' || c == '\u{131}' {
                    'i'
                } else {
                    c
                }
            })
            .collect(),
    )
}

/// Recover the original text for a match found in the folded haystack (1:1 chars).
fn original_slice(orig: &str, hay: &str, start: usize, end: usize) -> String {
    if std::ptr::eq(orig, hay) || orig == hay {
        return hay[start..end].to_string();
    }
    let cs = hay[..start].chars().count();
    let n = hay[start..end].chars().count();
    orig.chars().skip(cs).take(n).collect()
}

/// Content-part types that legitimately carry no scannable text.
const NON_TEXT_PART_TYPES: &[&str] = &["image_url", "image", "input_image", "input_audio", "file"];

/// Keys whose string/array/object values can carry text reaching the model.
const TEXT_KEYS: &[&str] = &["text", "content", "input", "arguments", "partial_json"];

/// Collect scannable strings from a `content` value; false means unsupported shape.
fn collect_content<'a>(v: Option<&'a Value>, out: &mut Vec<&'a str>) -> bool {
    match v {
        Some(Value::String(s)) => {
            out.push(s);
            true
        }
        Some(Value::Array(parts)) => parts.iter().fold(true, |ok, p| collect_part(p, out) & ok),
        // Null/missing content is judged by `collect_tool_calls` (legal only with a call).
        None | Some(Value::Null) => true,
        _ => false,
    }
}

/// Collect text from one content part (string or typed object).
fn collect_part<'a>(p: &'a Value, out: &mut Vec<&'a str>) -> bool {
    match p {
        Value::String(s) => {
            out.push(s);
            true
        }
        Value::Object(o) => {
            if let Some(Value::String(t)) = o.get("type") {
                if NON_TEXT_PART_TYPES.contains(&t.as_str()) {
                    return true;
                }
            }
            let mut found = false;
            let mut ok = true;
            for k in TEXT_KEYS {
                if let Some(v) = o.get(*k) {
                    found = true;
                    ok &= collect_value(v, out);
                }
            }
            found && ok
        }
        _ => false,
    }
}

/// Collect every string reachable inside a text-bearing value (objects/arrays recursed).
fn collect_value<'a>(v: &'a Value, out: &mut Vec<&'a str>) -> bool {
    match v {
        Value::String(s) => {
            out.push(s);
            true
        }
        Value::Array(a) => a.iter().fold(true, |ok, x| collect_value(x, out) & ok),
        Value::Object(o) => o.values().fold(true, |ok, x| collect_value(x, out) & ok),
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
    }
}

/// Collect tool/function-call arguments; false if content was absent and no call carries it.
fn collect_tool_calls<'a>(m: &'a Value, out: &mut Vec<&'a str>) -> bool {
    let mut has_call = false;
    if let Some(Value::Array(calls)) = m.get("tool_calls") {
        for c in calls {
            has_call = true;
            if let Some(f) = c.get("function") {
                collect_value(f, out);
            }
        }
    }
    if let Some(f) = m.get("function_call") {
        has_call = true;
        collect_value(f, out);
    }
    let content_absent = matches!(m.get("content"), None | Some(Value::Null));
    !content_absent || has_call
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
