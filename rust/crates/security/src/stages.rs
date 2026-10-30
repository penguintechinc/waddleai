//! Pipeline stages `security_in` / `security_out`: v1 port of Python `SecurityInStage` and
//! `SecurityOutStage`. They live here (not in `pipeline`) because `security` already depends
//! on `pipeline`; the reverse edge would be a cycle.

use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{FlagResolver, PipelineContext, Stage, StageError};
use serde_json::Value;

use crate::content_filter::ContentFilter;
use crate::prompt::{Policy, PromptScanner, SecurityError};
use crate::rules::RulesStore;

/// Flag whose ON state routes an org to the security v2 path (not ported; fails loudly).
const SECURITY_V2_FLAG: &str = "waddleai.security_v2";
/// Block reason for a prompt-injection hit (parity with Python).
const REASON_INJECTION: &str = "prompt_injection_detected";
/// Block reason for an input PII hard block (parity with Python).
const REASON_PII: &str = "pii_detected";
/// Block reason for an output block (parity with Python).
const REASON_OUTPUT: &str = "response_blocked_pii";

/// Failure to bring up the security stages (broken pattern table or rules seed).
#[derive(Debug, thiserror::Error)]
pub enum StartError {
    /// The threat pattern table did not compile.
    #[error("prompt scanner failed to start: {0}")]
    Scanner(#[from] SecurityError),
    /// No rules scope could be seeded; the cache would start empty.
    #[error("content-filter rules seed loaded 0 of {0} scopes")]
    Seed(usize),
}

/// Org id as the filter sees it (`None` for a missing user).
fn org_of(ctx: &PipelineContext) -> Option<i64> {
    ctx.user.as_ref().map(|u| i64::from(u.org_id))
}

/// True when security v2 is ON for the request's org.
async fn v2_on(flags: &dyn FlagResolver, org: Option<i64>) -> bool {
    let id = org.map(|o| o.to_string());
    flags.enabled(SECURITY_V2_FLAG, id.as_deref()).await
}

/// Scans prompts for injection, then filters PII/PCI from each message.
///
/// Prompt scan runs first and short-circuits so a blocked injection never reaches the filter.
pub struct SecurityInStage {
    scanner: PromptScanner,
    filter: Arc<ContentFilter>,
    flags: Arc<dyn FlagResolver>,
}

impl SecurityInStage {
    /// Starts the stage: verifies the pattern table (`try_new`, loud) and seeds the rules cache
    /// for the global scope plus `seed_orgs` so the filter never runs on a cold empty cache.
    pub async fn start(
        policy: Policy,
        filter: Arc<ContentFilter>,
        rules: Arc<RulesStore>,
        seed_orgs: &[i64],
        flags: Arc<dyn FlagResolver>,
    ) -> Result<Self, StartError> {
        let scanner = PromptScanner::try_new(policy)?;
        let report = rules.seed(seed_orgs).await;
        if report.loaded == 0 {
            return Err(StartError::Seed(report.failed));
        }
        if report.failed > 0 {
            tracing::warn!(failed = report.failed, "some rules scopes will lazy-load");
        }
        Ok(Self {
            scanner,
            filter,
            flags,
        })
    }
}

/// Outcome of filtering one piece of text.
enum Verdict {
    Blocked,
    Text { text: String, violated: bool },
}

async fn filter_text(
    filter: &ContentFilter,
    text: &str,
    user: Option<i64>,
    org: Option<i64>,
) -> Verdict {
    let r = filter.filter_input(text, user, org, None).await;
    if !r.allowed {
        return Verdict::Blocked;
    }
    Verdict::Text {
        text: r.filtered_text,
        violated: !r.violations.is_empty(),
    }
}

/// Filters a message's content in place (string, or the text of each content part). Returns
/// `Some(violated)` or `None` when a hard block fired. Non-text parts and null content are kept.
async fn filter_content(
    filter: &ContentFilter,
    msg: &mut Value,
    user: Option<i64>,
    org: Option<i64>,
) -> Option<bool> {
    let mut violated = false;
    let Some(content) = msg.get_mut("content") else {
        return Some(false);
    };
    match content {
        Value::String(s) => match filter_text(filter, s, user, org).await {
            Verdict::Blocked => return None,
            Verdict::Text { text, violated: v } => {
                *s = text;
                violated |= v;
            }
        },
        Value::Array(parts) => {
            for part in parts {
                let slot = match part {
                    Value::String(s) => Some(s),
                    Value::Object(o) => match o.get_mut("text") {
                        Some(Value::String(s)) => Some(s),
                        _ => None,
                    },
                    _ => None,
                };
                if let Some(s) = slot {
                    match filter_text(filter, s, user, org).await {
                        Verdict::Blocked => return None,
                        Verdict::Text { text, violated: v } => {
                            *s = text;
                            violated |= v;
                        }
                    }
                }
            }
        }
        _ => {}
    }
    Some(violated)
}

#[async_trait]
impl Stage for SecurityInStage {
    fn name(&self) -> &'static str {
        "security_in"
    }

    /// Scans then filters `ctx.messages`; blocks 400 on injection or a PII hard block.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        if ctx.messages.is_empty() {
            return Ok(());
        }
        if v2_on(self.flags.as_ref(), org_of(ctx)).await {
            return Err(StageError::Unsupported("security_v2"));
        }
        let threats = self.scanner.scan_messages(&ctx.messages);
        if PromptScanner::should_block(&threats) {
            ctx.blocked = true;
            ctx.status_code = 400;
            ctx.block_reason = Some(REASON_INJECTION.into());
            tracing::warn!("security_in: prompt injection blocked");
            return Ok(());
        }
        let user = ctx.user.as_ref().map(|u| i64::from(u.user_id));
        let org = org_of(ctx);
        let mut filtered = ctx.messages.clone();
        let mut pii = false;
        for msg in &mut filtered {
            match filter_content(&self.filter, msg, user, org).await {
                None => {
                    ctx.blocked = true;
                    ctx.status_code = 400;
                    ctx.block_reason = Some(REASON_PII.into());
                    tracing::warn!("security_in: PII hard block");
                    return Ok(());
                }
                Some(v) => pii |= v,
            }
        }
        ctx.messages = filtered;
        ctx.pii_detected = pii;
        tracing::debug!(messages = ctx.messages.len(), "security_in: scanned");
        Ok(())
    }
}

/// Filters the model response for PII/PCI before it returns to the caller.
pub struct SecurityOutStage {
    filter: Arc<ContentFilter>,
    flags: Arc<dyn FlagResolver>,
}

impl SecurityOutStage {
    /// Stage over a shared (already seeded) content filter.
    pub fn new(filter: Arc<ContentFilter>, flags: Arc<dyn FlagResolver>) -> Self {
        Self { filter, flags }
    }
}

#[async_trait]
impl Stage for SecurityOutStage {
    fn name(&self) -> &'static str {
        "security_out"
    }

    /// Redacts `ctx.response_text` in place; a filter block yields 400 `response_blocked_pii`.
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        if ctx.response_text.is_empty() {
            return Ok(());
        }
        if v2_on(self.flags.as_ref(), org_of(ctx)).await {
            return Err(StageError::Unsupported("security_v2"));
        }
        let user = ctx.user.as_ref().map(|u| i64::from(u.user_id));
        let r = self
            .filter
            .filter_output(&ctx.response_text, user, org_of(ctx), None)
            .await;
        if !r.allowed {
            ctx.blocked = true;
            ctx.status_code = 400;
            ctx.block_reason = Some(REASON_OUTPUT.into());
            tracing::warn!("security_out: response blocked");
            return Ok(());
        }
        ctx.response_text = r.filtered_text;
        tracing::debug!(violations = r.violations.len(), "security_out: filtered");
        Ok(())
    }
}
