//! Virtual-key budget payload types shared through `PipelineContext.extensions`.
//!
//! Owned by `pipeline` (ruling R1) so the auth stage can insert them and the `tokens`
//! crate can read them without a pipeline <-> tokens dependency cycle.

/// Rate limit configuration for a virtual key; `None` means "no limit".
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct KeyLimits {
    /// Tokens per minute.
    pub tpm_limit: Option<i64>,
    /// Tokens per calendar month.
    pub monthly_token_limit: Option<i64>,
    /// Micro-USD per calendar month.
    pub monthly_usd_limit: Option<i64>,
}

impl KeyLimits {
    /// True when no limit is configured, so gating can be skipped entirely.
    pub fn is_unlimited(&self) -> bool {
        self.tpm_limit.is_none()
            && self.monthly_token_limit.is_none()
            && self.monthly_usd_limit.is_none()
    }
}

/// The virtual-key budget subject the auth stage places in `ctx.extensions`. Absent means the
/// request has no virtual key (budget stage skips); `limits: None` means the key has no limits.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VkeyBudget {
    /// Virtual key id the budget is charged against.
    pub vkey_id: i64,
    /// The key's configured limits, if any.
    pub limits: Option<KeyLimits>,
}
