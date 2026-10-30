//! Pipeline stages `security_in` / `security_out`: v1 port of Python `SecurityInStage` and
//! `SecurityOutStage`. They live here (not in `pipeline`) because `security` already depends
//! on `pipeline`; the reverse edge would be a cycle.

use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{FlagResolver, PipelineContext, Stage, StageError};
use serde_json::Value;

use crate::content_filter::ContentFilter;
use crate::prompt::{message_slots, Policy, PromptScanner, SecurityError};

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
    /// Starts the stage: verifies the pattern table (`try_new`, loud) and seeds the filter's OWN
    /// rules store (global scope plus `seed_orgs`), so the filter this stage uses is guaranteed
    /// seeded. Build the output stage with [`SecurityInStage::out_stage`] so both share it.
    pub async fn start(
        policy: Policy,
        filter: Arc<ContentFilter>,
        seed_orgs: &[i64],
        flags: Arc<dyn FlagResolver>,
    ) -> Result<Self, StartError> {
        Self::build(PromptScanner::try_new(policy), filter, seed_orgs, flags).await
    }

    /// Assembles the stage from a scanner build result and seeds the filter's store.
    async fn build(
        scanner: Result<PromptScanner, SecurityError>,
        filter: Arc<ContentFilter>,
        seed_orgs: &[i64],
        flags: Arc<dyn FlagResolver>,
    ) -> Result<Self, StartError> {
        let scanner = scanner?;
        let report = filter.rules().seed(seed_orgs).await;
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

    /// The seeded content filter this stage uses.
    pub fn filter(&self) -> &Arc<ContentFilter> {
        &self.filter
    }

    /// Output stage sharing this stage's seeded filter and flag resolver (mis-wiring is
    /// structurally impossible).
    pub fn out_stage(&self) -> SecurityOutStage {
        SecurityOutStage {
            filter: Arc::clone(&self.filter),
            flags: Arc::clone(&self.flags),
        }
    }
}

/// Most text slots one message may carry before it is refused (fail closed, bounds work).
const MAX_SLOTS: usize = 256;

/// Filters one message in a single `filter_input` call over its joined text, then maps the
/// redaction back into the slots. `None` = hard block (a block verdict, an unsupported shape,
/// too many slots, or a redaction that cannot be mapped back); `Some(violated)` otherwise.
///
/// Slots come from the same walker as the prompt scanner, joined with `"\n"` (custom rules are
/// multiline, so anchors apply per part). Empty/whitespace-only slots are not sent. Mapping
/// back splits the redacted text on `"\n"`; if the line count changed (a span crossed a part
/// boundary) the message is blocked rather than guessed at.
async fn filter_message(
    filter: &ContentFilter,
    msg: &mut Value,
    user: Option<i64>,
    org: Option<i64>,
) -> Option<bool> {
    let (ok, ptrs) = message_slots(msg);
    if !ok || ptrs.len() > MAX_SLOTS {
        return None;
    }
    let active: Vec<&String> = ptrs
        .iter()
        .filter(|p| {
            msg.pointer(p)
                .and_then(Value::as_str)
                .is_some_and(|t| !t.trim().is_empty())
        })
        .collect();
    if active.is_empty() {
        return Some(false);
    }
    let texts: Vec<String> = active
        .iter()
        .filter_map(|p| msg.pointer(p).and_then(Value::as_str).map(str::to_owned))
        .collect();
    let joined = texts.join("\n");
    let r = filter.filter_input(&joined, user, org, None).await;
    if !r.allowed {
        return None;
    }
    if r.filtered_text == joined {
        return Some(!r.violations.is_empty());
    }
    let segments: Vec<&str> = r.filtered_text.split('\n').collect();
    let lines: Vec<usize> = texts.iter().map(|t| t.matches('\n').count() + 1).collect();
    if segments.len() != lines.iter().sum::<usize>() {
        tracing::warn!("security_in: redaction crossed a part boundary; blocking");
        return None;
    }
    let mut at = 0;
    for (p, n) in active.iter().zip(&lines) {
        let Some(Value::String(slot)) = msg.pointer_mut(p) else {
            return None;
        };
        *slot = segments[at..at + n].join("\n");
        at += n;
    }
    Some(true)
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
            match filter_message(&self.filter, msg, user, org).await {
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
    /// The shared content filter this stage uses.
    pub fn filter(&self) -> &Arc<ContentFilter> {
        &self.filter
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::content_filter::FailModes;
    use crate::rules::{RawOrgConfig, RulesError, RulesSource, RulesStore};
    use pipeline::StaticFlagResolver;

    /// Source that loads only the global scope and fails every org scope.
    struct GlobalOnly;

    #[async_trait]
    impl RulesSource for GlobalOnly {
        async fn load(&self, org: Option<i64>) -> Result<RawOrgConfig, RulesError> {
            match org {
                None => Ok(RawOrgConfig::default()),
                Some(_) => Err(RulesError::Source("down".into())),
            }
        }
    }

    /// Source that always fails.
    struct Dead;

    #[async_trait]
    impl RulesSource for Dead {
        async fn load(&self, _: Option<i64>) -> Result<RawOrgConfig, RulesError> {
            Err(RulesError::Source("down".into()))
        }
    }

    fn filter(src: Arc<dyn RulesSource>) -> Arc<ContentFilter> {
        let store = Arc::new(RulesStore::new(src, None));
        Arc::new(ContentFilter::new(store, None, FailModes::default()).unwrap())
    }

    fn flags() -> Arc<dyn FlagResolver> {
        Arc::new(StaticFlagResolver::default())
    }

    #[tokio::test]
    async fn broken_pattern_table_fails_start_loudly() {
        let err = SecurityError::Compile {
            index: 0,
            message: "bad".into(),
        };
        let r = SecurityInStage::build(Err(err), filter(Arc::new(GlobalOnly)), &[], flags()).await;
        assert!(matches!(r, Err(StartError::Scanner(_))));
    }

    #[tokio::test]
    async fn zero_seeded_scopes_fails_start_loudly() {
        let r = SecurityInStage::start(
            Policy::named("strict"),
            filter(Arc::new(Dead)),
            &[],
            flags(),
        )
        .await;
        assert!(matches!(r, Err(StartError::Seed(1))));
    }

    #[tokio::test]
    async fn partial_seed_still_starts() {
        let r = SecurityInStage::start(
            Policy::named("strict"),
            filter(Arc::new(GlobalOnly)),
            &[9],
            flags(),
        )
        .await;
        assert!(r.is_ok());
    }
}
