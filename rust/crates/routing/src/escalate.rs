//! Escalation triggers and target resolution (port of `shared/routing/escalation.py`).
//!
//! The proxy stage never supplies a session, so sticky state is not consulted; only the
//! classifier-free triggers apply (explicit hint here).

/// Outcome of the escalation check.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EscalationDecision {
    /// Whether to escalate.
    pub escalate: bool,
    /// Which trigger fired.
    pub trigger: Option<&'static str>,
}

/// Evaluates the escalation triggers; the first match wins. `auto:high`/`true` escalate,
/// `auto:low` suppresses every later trigger, any other hint is ignored.
pub fn should_escalate(
    complexity: Option<i64>,
    threshold: i64,
    explicit_hint: Option<&str>,
) -> EscalationDecision {
    match explicit_hint {
        Some("true" | "auto:high") => {
            return EscalationDecision {
                escalate: true,
                trigger: Some("explicit_hint"),
            }
        }
        Some("auto:low") => {
            return EscalationDecision {
                escalate: false,
                trigger: None,
            }
        }
        _ => {}
    }
    if complexity.is_some_and(|c| c >= threshold) {
        return EscalationDecision {
            escalate: true,
            trigger: Some("complexity"),
        };
    }
    EscalationDecision {
        escalate: false,
        trigger: None,
    }
}

/// Assignment row's escalation model wins over the org policy target (empty counts as unset).
pub fn escalation_target(assignment: Option<&str>, policy: Option<&str>) -> Option<String> {
    assignment
        .filter(|s| !s.is_empty())
        .or(policy.filter(|s| !s.is_empty()))
        .map(str::to_owned)
}
