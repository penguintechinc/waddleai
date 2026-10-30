//! PII/PCI content filter: tier 1 builtins, tier 2 org rules, tier 3 NER seam.
//!
//! Port of `shared/security/content_filter.py` (tiers 1-3; the tier-4 auditor is P3). Raw
//! matched text never leaves this module: results carry rule names, actions and the
//! redacted output only.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use fancy_regex::Regex;

use crate::builtins::BUILTIN_PATTERNS;
use crate::ner::NerTier;
use crate::rules::{scope_of, Matcher, OrgConfig, RulesStore};

/// Minimum spacing of degraded-policy WARN lines per (org, phase).
const WARN_INTERVAL: Duration = Duration::from_secs(60);

/// Which side of the model call is being filtered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Phase {
    /// Prompt going to the model.
    Input,
    /// Completion coming back.
    Output,
}

impl Phase {
    /// Rule `target` value for this phase.
    fn target(self) -> &'static str {
        match self {
            Phase::Input => "input",
            Phase::Output => "output",
        }
    }
}

/// What to do when a tier could not run (rules unreachable, NER unavailable, scan error).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailMode {
    /// Keep the decision the remaining tiers produced.
    Open,
    /// Block the request.
    Closed,
}

/// Degraded-policy table: a default plus per-org overrides.
#[derive(Debug, Clone)]
pub struct FailModes {
    default: FailMode,
    per_org: HashMap<i64, FailMode>,
}

impl FailModes {
    /// Policy with `default` for every org.
    pub fn new(default: FailMode) -> Self {
        Self {
            default,
            per_org: HashMap::new(),
        }
    }

    /// Overrides the policy for one org.
    pub fn with_org(mut self, org_id: i64, mode: FailMode) -> Self {
        self.per_org.insert(org_id, mode);
        self
    }

    /// Effective mode for an org.
    pub fn for_org(&self, org: Option<i64>) -> FailMode {
        org.and_then(|o| self.per_org.get(&o).copied())
            .unwrap_or(self.default)
    }
}

impl Default for FailModes {
    /// Fail-open, matching the Python default (`auditor_fail_mode="open"`).
    fn default() -> Self {
        Self::new(FailMode::Open)
    }
}

/// Per-rule action.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuleAction {
    /// Reject the request.
    Block,
    /// Replace the match with a typed placeholder.
    Redact,
    /// Record only.
    Log,
}

impl RuleAction {
    /// Parses a stored action; unknown values are log-only, as in Python.
    fn parse(s: &str) -> Self {
        match s {
            "block" => Self::Block,
            "redact" => Self::Redact,
            _ => Self::Log,
        }
    }
}

/// Overall decision.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FilterAction {
    /// No violations.
    Allow,
    /// Blocked.
    Block,
    /// Text was redacted.
    Redact,
    /// Violations recorded, text untouched.
    Log,
}

impl FilterAction {
    /// Python action string.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Allow => "allow",
            Self::Block => "block",
            Self::Redact => "redact",
            Self::Log => "log",
        }
    }
}

/// Origin of a violation (Python `rule_type`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuleKind {
    /// Tier-1 builtin pattern.
    BuiltinPii,
    /// Tier-2 substring rule.
    CustomString,
    /// Tier-2 regex rule.
    CustomRegex,
    /// Tier-3 NER entity.
    NerEntity,
}

impl RuleKind {
    /// Python rule-type string.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BuiltinPii => "builtin_pii",
            Self::CustomString => "custom_string",
            Self::CustomRegex => "custom_regex",
            Self::NerEntity => "ner_entity",
        }
    }
}

/// One detected violation. Deliberately carries no matched text.
#[derive(Debug, Clone, PartialEq)]
pub struct FilterViolation {
    /// Builtin name, custom rule name, or `ner:<TYPE>`.
    pub rule_name: String,
    /// Which tier produced it.
    pub kind: RuleKind,
    /// Rule action.
    pub action: RuleAction,
    /// Detection confidence.
    pub confidence: f64,
}

/// Outcome of filtering one text.
#[derive(Debug, Clone, PartialEq)]
pub struct FilterResult {
    /// False only when blocked.
    pub allowed: bool,
    /// Overall decision.
    pub action: FilterAction,
    /// Redacted text (original unless action is `Redact`).
    pub filtered_text: String,
    /// Violations in tier order.
    pub violations: Vec<FilterViolation>,
    /// A tier could not run; the decision was made under the degraded policy.
    pub degraded: bool,
}

/// A violation plus the raw matched text needed for redaction (never exposed).
struct Hit {
    violation: FilterViolation,
    full: String,
}

/// A compiled builtin pattern.
struct Builtin {
    name: &'static str,
    regex: Regex,
    confidence: f64,
}

/// Tiers 1-3 content filter over a shared rules store.
pub struct ContentFilter {
    builtins: Vec<Builtin>,
    rules: Arc<RulesStore>,
    ner: Option<NerTier>,
    fail_modes: FailModes,
    last_warn: Mutex<HashMap<(Option<i64>, Phase), Instant>>,
}

/// Failure to construct the filter (a builtin did not compile: a build-time defect).
#[derive(Debug, thiserror::Error)]
#[error("builtin pattern {0} failed to compile")]
pub struct BuildError(pub &'static str);

impl ContentFilter {
    /// Filter with the 23 builtins; `ner` of `None` leaves tier 3 off entirely.
    pub fn new(
        rules: Arc<RulesStore>,
        ner: Option<NerTier>,
        fail_modes: FailModes,
    ) -> Result<Self, BuildError> {
        let mut builtins = Vec::with_capacity(BUILTIN_PATTERNS.len());
        for (name, pattern, confidence) in BUILTIN_PATTERNS {
            let regex = Regex::new(&format!("(?is){pattern}")).map_err(|_| BuildError(name))?;
            builtins.push(Builtin {
                name,
                regex,
                confidence: *confidence,
            });
        }
        Ok(Self {
            builtins,
            rules,
            ner,
            fail_modes,
            last_warn: Mutex::new(HashMap::new()),
        })
    }

    /// Filters a prompt before it reaches the model. `user_id`/`ip` are for the audit layer
    /// and are intentionally not logged here.
    pub async fn filter_input(
        &self,
        text: &str,
        user_id: Option<i64>,
        org_id: Option<i64>,
        ip: Option<&str>,
    ) -> FilterResult {
        self.run(text, Phase::Input, user_id, org_id, ip).await
    }

    /// Filters a completion before it returns to the caller.
    pub async fn filter_output(
        &self,
        text: &str,
        user_id: Option<i64>,
        org_id: Option<i64>,
        ip: Option<&str>,
    ) -> FilterResult {
        self.run(text, Phase::Output, user_id, org_id, ip).await
    }

    /// Runs tiers 1-3 and applies the decision and degraded policy.
    async fn run(
        &self,
        text: &str,
        phase: Phase,
        _user_id: Option<i64>,
        org_id: Option<i64>,
        _ip: Option<&str>,
    ) -> FilterResult {
        let org = scope_of(org_id);
        let mut degraded = false;
        let mut hits: Vec<Hit> = Vec::new();

        let cfg = match self.rules.get(org).await {
            Ok(cfg) => cfg,
            Err(_) => {
                degraded = true;
                Arc::new(OrgConfig::compile(Default::default()))
            }
        };

        degraded |= self.run_builtins(text, &cfg, &mut hits);
        run_custom(text, phase, &cfg, &mut hits);

        if let Some(ner) = &self.ner {
            if ner.enabled(org).await {
                match ner.detect(text, org).await {
                    Ok(found) => hits.extend(found.into_iter().map(|h| Hit {
                        violation: FilterViolation {
                            rule_name: h.rule_name,
                            kind: RuleKind::NerEntity,
                            action: if h.redact {
                                RuleAction::Redact
                            } else {
                                RuleAction::Log
                            },
                            confidence: h.confidence,
                        },
                        full: h.full,
                    })),
                    Err(_) => degraded = true,
                }
            }
        }

        let (mut action, mut filtered) = decide(text, &hits);
        if degraded {
            let mode = self.fail_modes.for_org(org);
            self.warn_degraded(org, phase, mode);
            if mode == FailMode::Closed {
                action = FilterAction::Block;
                filtered = text.to_owned();
            }
        }
        tracing::debug!(
            phase = phase.target(),
            action = action.as_str(),
            violations = hits.len(),
            degraded,
            "content filter decision"
        );
        FilterResult {
            allowed: action != FilterAction::Block,
            action,
            filtered_text: filtered,
            violations: hits.into_iter().map(|h| h.violation).collect(),
            degraded,
        }
    }

    /// Tier 1: runs every builtin the org has not disabled. Returns true on a scan error.
    fn run_builtins(&self, text: &str, cfg: &OrgConfig, hits: &mut Vec<Hit>) -> bool {
        let mut errored = false;
        for b in &self.builtins {
            if cfg.disabled_builtins.contains(b.name) {
                continue;
            }
            for m in b.regex.find_iter(text) {
                match m {
                    Ok(m) if !m.as_str().is_empty() => hits.push(Hit {
                        violation: FilterViolation {
                            rule_name: b.name.to_owned(),
                            kind: RuleKind::BuiltinPii,
                            action: RuleAction::Redact,
                            confidence: b.confidence,
                        },
                        full: m.as_str().to_owned(),
                    }),
                    Ok(_) => {}
                    Err(_) => {
                        tracing::error!(pattern = b.name, "builtin scan aborted (regex limit)");
                        errored = true;
                        break;
                    }
                }
            }
        }
        errored
    }

    /// Emits the degraded-policy WARN at most once per interval per (org, phase).
    fn warn_degraded(&self, org: Option<i64>, phase: Phase, mode: FailMode) {
        let mut map = self.last_warn.lock().unwrap_or_else(|p| p.into_inner());
        let now = Instant::now();
        let due = map
            .get(&(org, phase))
            .is_none_or(|t| now.duration_since(*t) >= WARN_INTERVAL);
        if due {
            map.insert((org, phase), now);
            tracing::warn!(
                phase = phase.target(),
                fail_mode = ?mode,
                "content filter degraded: a tier could not run"
            );
        }
    }
}

/// Tier 2: applies the org's compiled rules for this phase.
fn run_custom(text: &str, phase: Phase, cfg: &OrgConfig, hits: &mut Vec<Hit>) {
    for c in &cfg.rules {
        let r = &c.rule;
        if r.target != phase.target() && r.target != "both" {
            continue;
        }
        let action = RuleAction::parse(&r.action);
        match &c.matcher {
            Matcher::Substring(needle) => {
                let lowered = text.to_lowercase();
                let Some(byte_idx) = lowered.find(needle.as_str()) else {
                    continue;
                };
                // Python slices the original by indices found in the lowercased text.
                let start = lowered[..byte_idx].chars().count();
                let len = r.pattern.chars().count();
                let full: String = text.chars().skip(start).take(len).collect();
                if full.is_empty() {
                    continue;
                }
                hits.push(Hit {
                    violation: FilterViolation {
                        rule_name: r.name.clone(),
                        kind: RuleKind::CustomString,
                        action,
                        confidence: 0.95,
                    },
                    full,
                });
            }
            Matcher::Pattern(re) => {
                for m in re.find_iter(text) {
                    match m {
                        Ok(m) if !m.as_str().is_empty() => hits.push(Hit {
                            violation: FilterViolation {
                                rule_name: r.name.clone(),
                                kind: RuleKind::CustomRegex,
                                action,
                                confidence: 0.90,
                            },
                            full: m.as_str().to_owned(),
                        }),
                        Ok(_) => {}
                        Err(_) => {
                            tracing::warn!(rule_id = r.id, "custom regex aborted (regex limit)");
                            break;
                        }
                    }
                }
            }
        }
    }
}

/// `_determine_action`: block beats redact beats log; redaction applies to redact hits only.
fn decide(text: &str, hits: &[Hit]) -> (FilterAction, String) {
    if hits.is_empty() {
        return (FilterAction::Allow, text.to_owned());
    }
    if hits.iter().any(|h| h.violation.action == RuleAction::Block) {
        return (FilterAction::Block, text.to_owned());
    }
    let redacts: Vec<&Hit> = hits
        .iter()
        .filter(|h| h.violation.action == RuleAction::Redact)
        .collect();
    if !redacts.is_empty() {
        return (FilterAction::Redact, apply_redactions(text, &redacts));
    }
    (FilterAction::Log, text.to_owned())
}

/// `_apply_redactions`: sequentially replaces every case-insensitive occurrence of each
/// matched text with `[REDACTED:<RULE_NAME>]`, in violation order.
fn apply_redactions(text: &str, redacts: &[&Hit]) -> String {
    let mut out = text.to_owned();
    for h in redacts {
        let placeholder = format!("[REDACTED:{}]", h.violation.rule_name.to_uppercase());
        match regex::Regex::new(&format!("(?i){}", regex::escape(&h.full))) {
            Ok(re) => {
                out = re
                    .replace_all(&out, regex::NoExpand(&placeholder))
                    .into_owned()
            }
            Err(_) => tracing::error!("redaction pattern failed to compile; match left in place"),
        }
    }
    out
}
