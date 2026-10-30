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
        // Distinct slot (never aliases a pattern type); `action_for` short-circuits it.
        ThreatType::UnsupportedContent => 5,
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
    /// Strings are scanned directly; content-part arrays contribute their text
    /// (`text`, nested `content`, tool `input`/`arguments`) and tool-call objects on
    /// the message contribute theirs; everything is joined and scanned as ONE unit. Missing/null/unknown content shapes fail closed
    /// with a Block threat, mirroring Python (which raises on non-string content).
    pub fn scan_messages(&self, messages: &[Value]) -> Vec<Threat> {
        let mut out = Vec::new();
        for m in messages {
            let (ok, ptrs) = message_slots(m);
            let texts: Vec<&str> = ptrs
                .iter()
                .filter_map(|p| m.pointer(p).and_then(Value::as_str))
                .collect();
            if !ok {
                out.push(self.unsupported());
            }
            // One unit per message: lengths and match counts sum across parts, so a
            // phrase fragmented over parts cannot slip under any threshold.
            if !texts.is_empty() {
                out.extend(self.scan_prompt(&texts.join("\n")));
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
        let mut per: [Vec<String>; 6] = Default::default();
        let mut counts = [0usize; 6];
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

/// Escapes one object key as a JSON-pointer segment.
fn seg(key: &str) -> String {
    key.replace('~', "~0").replace('/', "~1")
}

/// Every text-bearing string slot of one message, as JSON pointers relative to the message,
/// plus whether every part had a supported shape (false = fail closed).
///
/// This is the SINGLE field walker: the prompt scanner reads these slots and the content
/// filter rewrites the same slots, so detection and redaction coverage cannot drift apart.
pub(crate) fn message_slots(m: &Value) -> (bool, Vec<String>) {
    let mut out = Vec::new();
    let mut ok = collect_content(m.get("content"), "/content", &mut out);
    ok &= collect_tool_calls(m, &mut out);
    (ok, out)
}

/// Collect scannable string slots from a `content` value; false means unsupported shape.
fn collect_content(v: Option<&Value>, ptr: &str, out: &mut Vec<String>) -> bool {
    match v {
        Some(Value::String(_)) => {
            out.push(ptr.to_string());
            true
        }
        Some(Value::Array(parts)) => parts.iter().enumerate().fold(true, |ok, (i, p)| {
            collect_part(p, &format!("{ptr}/{i}"), out) & ok
        }),
        // Null/missing content is judged by `collect_tool_calls` (legal only with a call).
        None | Some(Value::Null) => true,
        _ => false,
    }
}

/// Allowlist: a `file` part is a pure reference only if its keys are within these.
const FILE_PART_KEYS: &[&str] = &["type", "file_id", "file"];
/// Allowlist for the nested `file` object of a `file` part.
const FILE_NESTED_KEYS: &[&str] = &["file_id", "filename"];

/// True if a `file` part carries nothing beyond string references (no inline payload).
fn is_pure_file_reference(o: &serde_json::Map<String, Value>) -> bool {
    let top_ok = o.iter().all(|(k, v)| match k.as_str() {
        "type" | "file_id" => v.is_string(),
        "file" => v.as_object().is_some_and(|n| {
            n.iter()
                .all(|(nk, nv)| FILE_NESTED_KEYS.contains(&nk.as_str()) && nv.is_string())
        }),
        _ => false,
    });
    top_ok && o.keys().all(|k| FILE_PART_KEYS.contains(&k.as_str()))
}

/// Collect slots from one content part (string or typed object).
///
/// Every part has its TEXT_KEYS scanned, even image/audio/file ones (regression:
/// text smuggled into a "non-text" part). A `file` part with an inline payload is
/// unscannable and fails closed; pure references (URL/file id) carry no text.
fn collect_part(p: &Value, ptr: &str, out: &mut Vec<String>) -> bool {
    match p {
        Value::String(_) => {
            out.push(ptr.to_string());
            true
        }
        Value::Object(o) => {
            let ty = o.get("type").and_then(Value::as_str).unwrap_or("");
            let non_text = NON_TEXT_PART_TYPES.contains(&ty);
            let mut found = false;
            let mut ok = true;
            for k in TEXT_KEYS {
                if let Some(v) = o.get(*k) {
                    found = true;
                    ok &= collect_value(v, &format!("{ptr}/{}", seg(k)), out);
                }
            }
            if ty == "file" && !is_pure_file_reference(o) {
                ok = false;
            }
            (found || non_text) && ok
        }
        _ => false,
    }
}

/// Collect every string slot reachable inside a text-bearing value (objects/arrays recursed).
fn collect_value(v: &Value, ptr: &str, out: &mut Vec<String>) -> bool {
    match v {
        Value::String(_) => {
            out.push(ptr.to_string());
            true
        }
        Value::Array(a) => a.iter().enumerate().fold(true, |ok, (i, x)| {
            collect_value(x, &format!("{ptr}/{i}"), out) & ok
        }),
        Value::Object(o) => o.iter().fold(true, |ok, (k, x)| {
            collect_value(x, &format!("{ptr}/{}", seg(k)), out) & ok
        }),
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
    }
}

/// Whether a tool-call element has a recognized, scanned shape.
fn is_call_shape(c: &Value) -> bool {
    c.as_object().is_some_and(|o| {
        ["function", "input", "arguments"]
            .iter()
            .any(|k| o.contains_key(*k))
    })
}

/// Collect tool/function-call slots (whole objects); false on unrecognized call
/// shapes, or when content is absent and no recognized call carries it.
fn collect_tool_calls(m: &Value, out: &mut Vec<String>) -> bool {
    let mut has_call = false;
    let mut ok = true;
    match m.get("tool_calls") {
        None => {}
        Some(Value::Array(calls)) => {
            for (i, c) in calls.iter().enumerate() {
                collect_value(c, &format!("/tool_calls/{i}"), out);
                if is_call_shape(c) {
                    has_call = true;
                } else {
                    ok = false;
                }
            }
        }
        Some(other) => {
            collect_value(other, "/tool_calls", out);
            ok = false;
        }
    }
    if let Some(f) = m.get("function_call") {
        collect_value(f, "/function_call", out);
        if f.as_object()
            .is_some_and(|o| o.contains_key("arguments") || o.contains_key("name"))
        {
            has_call = true;
        } else {
            ok = false;
        }
    }
    let content_absent = matches!(m.get("content"), None | Some(Value::Null));
    ok && (!content_absent || has_call)
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
