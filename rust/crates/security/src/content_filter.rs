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
/// `Closed` is the safe production mode; `Open` is the Python-parity default.
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

/// A violation plus the byte span recorded at match time (the text itself is never kept).
struct Hit {
    violation: FilterViolation,
    span: (usize, usize),
    /// Further spans covered by the same violation (a substring rule's later occurrences).
    extra: Vec<(usize, usize)>,
}

/// A compiled case-insensitive, dot-all pattern. Patterns the linear-time `regex` engine can
/// express (no lookaround/backreference) never backtrack; only the rest use fancy-regex, with an
/// explicit backtrack limit.
pub(crate) enum Pattern {
    /// Linear-time engine.
    Linear(regex::Regex),
    /// Backtracking engine (lookaround/backreference patterns).
    Fancy(Regex),
}

impl Pattern {
    /// Compiles `pattern`, preferring the linear engine; `limit` bounds the fancy fallback.
    pub(crate) fn build(pattern: &str, limit: usize) -> Result<Self, fancy_regex::Error> {
        let full = format!("(?is){pattern}");
        if let Ok(re) = regex::Regex::new(&full) {
            return Ok(Self::Linear(re));
        }
        fancy_regex::RegexBuilder::new(&full)
            .backtrack_limit(limit)
            .build()
            .map(Self::Fancy)
    }

    /// Non-empty match spans; the flag is true when the scan aborted (matches found before the
    /// abort are still returned).
    pub(crate) fn spans(&self, text: &str) -> (Vec<(usize, usize)>, bool) {
        match self {
            Self::Linear(re) => (
                re.find_iter(text)
                    .filter(|m| !m.as_str().is_empty())
                    .map(|m| (m.start(), m.end()))
                    .collect(),
                false,
            ),
            Self::Fancy(re) => {
                let mut out = Vec::new();
                for m in re.find_iter(text) {
                    match m {
                        Ok(m) if !m.as_str().is_empty() => out.push((m.start(), m.end())),
                        Ok(_) => {}
                        Err(_) => return (out, true),
                    }
                }
                (out, false)
            }
        }
    }
}

/// Backtrack ceiling for org-supplied (untrusted) fancy regexes; exceeding it aborts that rule.
pub(crate) const CUSTOM_BACKTRACK_LIMIT: usize = 1_000_000;
/// Backtrack ceiling for the two vetted lookahead builtins (the only fancy ones).
const BUILTIN_BACKTRACK_LIMIT: usize = 100_000_000;

/// Builds an org custom rule pattern (tight backtrack limit).
pub(crate) fn build_regex(pattern: &str) -> Result<Pattern, fancy_regex::Error> {
    Pattern::build(pattern, CUSTOM_BACKTRACK_LIMIT)
}

/// Outcome of the tier-2 scan beyond its hits.
#[derive(Default)]
struct CustomScan {
    /// Some rule aborted (regex limit / broken literal): its verdict is unknown.
    errored: bool,
    /// An aborted rule was a BLOCK rule: must fail closed whatever the org fail mode.
    blocked_abort: bool,
}

/// A compiled builtin pattern.
struct Builtin {
    name: &'static str,
    regex: Pattern,
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
            let regex =
                Pattern::build(pattern, BUILTIN_BACKTRACK_LIMIT).map_err(|_| BuildError(name))?;
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
        let scan = run_custom(text, phase, &cfg, &mut hits);
        degraded |= scan.errored;

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
                        span: h.span,
                        extra: Vec::new(),
                    })),
                    Err(_) => degraded = true,
                }
            }
        }

        let (mut action, mut filtered) = match decide(text, &hits) {
            Some(d) => d,
            None => {
                // A span could not be applied: never emit unredacted PII.
                degraded = true;
                (FilterAction::Redact, String::new())
            }
        };
        let redaction_failed =
            action == FilterAction::Redact && filtered.is_empty() && !text.is_empty();
        if degraded {
            let mode = self.fail_modes.for_org(org);
            self.warn_degraded(org, phase, mode);
            if mode == FailMode::Closed || scan.blocked_abort || redaction_failed {
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
            let (spans, aborted) = b.regex.spans(text);
            for span in spans {
                hits.push(Hit {
                    violation: FilterViolation {
                        rule_name: b.name.to_owned(),
                        kind: RuleKind::BuiltinPii,
                        action: RuleAction::Redact,
                        confidence: b.confidence,
                    },
                    span,
                    extra: Vec::new(),
                });
            }
            if aborted {
                tracing::error!(pattern = b.name, "builtin scan aborted (regex limit)");
                errored = true;
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

/// Tier 2: applies the org's compiled rules for this phase, recording match-time byte spans.
fn run_custom(text: &str, phase: Phase, cfg: &OrgConfig, hits: &mut Vec<Hit>) -> CustomScan {
    let mut scan = CustomScan::default();
    for c in &cfg.rules {
        let r = &c.rule;
        if r.target != phase.target() && r.target != "both" {
            continue;
        }
        let action = RuleAction::parse(&r.action);
        let abort = |scan: &mut CustomScan| {
            scan.errored = true;
            scan.blocked_abort |= action == RuleAction::Block;
        };
        let (kind, confidence) = match &c.matcher {
            Matcher::Substring(_) => (RuleKind::CustomString, 0.95),
            _ => (RuleKind::CustomRegex, 0.90),
        };
        let mut push = |s: usize, e: usize, extra: Vec<(usize, usize)>| {
            hits.push(Hit {
                violation: FilterViolation {
                    rule_name: r.name.clone(),
                    kind,
                    action,
                    confidence,
                },
                span: (s, e),
                extra,
            });
        };
        match &c.matcher {
            Matcher::Broken => abort(&mut scan),
            Matcher::Substring(re) => {
                // Python records only the first occurrence; redaction still covers all of them.
                let mut it = re.find_iter(text);
                if let Some(first) = it.next() {
                    push(
                        first.start(),
                        first.end(),
                        it.map(|m| (m.start(), m.end())).collect(),
                    );
                }
            }
            Matcher::Pattern(re) => {
                let (spans, aborted) = re.spans(text);
                for (s, e) in spans {
                    push(s, e, Vec::new());
                }
                if aborted {
                    tracing::warn!(rule_id = r.id, "custom regex aborted (regex limit)");
                    abort(&mut scan);
                }
            }
        }
    }
    scan
}

/// `_determine_action`: block beats redact beats log. `None` = a redaction span was unusable.
fn decide(text: &str, hits: &[Hit]) -> Option<(FilterAction, String)> {
    if hits.is_empty() {
        return Some((FilterAction::Allow, text.to_owned()));
    }
    if hits.iter().any(|h| h.violation.action == RuleAction::Block) {
        return Some((FilterAction::Block, text.to_owned()));
    }
    let redacts: Vec<&Hit> = hits
        .iter()
        .filter(|h| h.violation.action == RuleAction::Redact)
        .collect();
    if !redacts.is_empty() {
        return apply_redactions(text, &redacts).map(|t| (FilterAction::Redact, t));
    }
    Some((FilterAction::Log, text.to_owned()))
}

/// Case-insensitive (per-char simple fold) non-overlapping occurrences of `needle` in `text`,
/// as byte spans of `text`. No regex, no offset math on a re-cased copy, cannot fail.
fn occurrences(text: &str, needle: &str) -> Vec<(usize, usize)> {
    let n: Vec<char> = needle.chars().collect();
    let mut out = Vec::new();
    if n.is_empty() {
        return out;
    }
    let fold_eq = |a: char, b: char| a == b || a.to_lowercase().eq(b.to_lowercase());
    let mut skip_to = 0usize;
    for (i, _) in text.char_indices() {
        if i < skip_to {
            continue;
        }
        let mut it = text[i..].char_indices();
        let mut end = i;
        let mut ok = true;
        for &nc in &n {
            match it.next() {
                Some((o, tc)) if fold_eq(tc, nc) => end = i + o + tc.len_utf8(),
                _ => {
                    ok = false;
                    break;
                }
            }
        }
        if ok {
            out.push((i, end));
            skip_to = end;
        }
    }
    out
}

/// Distinct matched texts for which other occurrences are searched (parity extra; bounded so
/// a flood of matches cannot go quadratic -- every match's own span is always redacted).
const PROPAGATE_CAP: usize = 64;

/// `_apply_redactions` by span: every redact hit's recorded spans (plus, for a bounded number of
/// distinct texts, other case-insensitive occurrences as Python's re-sub does) are claimed in hit
/// order; a span overlapping an earlier claim is dropped (Python's sequential substitution
/// likewise no longer finds a broken match). Claims are replaced right-to-left on the original
/// text. `None` if a span is unusable.
fn apply_redactions(text: &str, redacts: &[&Hit]) -> Option<String> {
    use std::collections::{BTreeMap, HashSet};
    let mut claimed: BTreeMap<usize, (usize, usize)> = BTreeMap::new(); // start -> (end, label idx)
    let mut propagated: HashSet<String> = HashSet::new();
    for (idx, h) in redacts.iter().enumerate() {
        let (s, e) = h.span;
        let own = text.get(s..e).filter(|t| !t.is_empty())?;
        let mut spans = vec![(s, e)];
        spans.extend(h.extra.iter().copied());
        if propagated.len() < PROPAGATE_CAP && propagated.insert(own.to_lowercase()) {
            spans.extend(occurrences(text, own));
        }
        for (s, e) in spans {
            // Claims are disjoint, so only the nearest claim starting before `e` can overlap.
            let overlaps = claimed.range(..e).next_back().is_some_and(|(_, c)| c.0 > s);
            if !overlaps {
                claimed.insert(s, (e, idx));
            }
        }
    }
    let merged: Vec<(usize, usize, usize)> =
        claimed.into_iter().map(|(s, (e, i))| (s, e, i)).collect();
    let mut out = text.to_owned();
    for (s, e, idx) in merged.into_iter().rev() {
        if !text.is_char_boundary(s) || !text.is_char_boundary(e) {
            return None;
        }
        let label = format!(
            "[REDACTED:{}]",
            redacts[idx].violation.rule_name.to_uppercase()
        );
        out.replace_range(s..e, &label);
    }
    Some(out)
}
