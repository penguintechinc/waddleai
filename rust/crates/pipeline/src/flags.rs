//! Flag resolution seam for the driver, with a fail-safe adapter over P0 `flags::enabled`.

use std::collections::HashSet;
use std::time::Duration;

/// Resolves whether a feature flag is on for a distinct id. Never errors:
/// any failure resolves to `false` (fail-safe OFF).
#[async_trait::async_trait]
pub trait FlagResolver: Send + Sync {
    /// True only when the flag is positively enabled.
    async fn enabled(&self, flag: &str, distinct_id: Option<&str>) -> bool;
}

/// Production resolver delegating to the process-wide P0 `flags` client; a lookup
/// that exceeds the timeout resolves OFF. A missing distinct id evaluates as the
/// empty id (anonymous), which PostHog treats as no per-user match.
pub struct PosthogFlagResolver {
    timeout: Duration,
}

impl PosthogFlagResolver {
    /// Resolver with a 2s per-lookup ceiling.
    pub fn new() -> Self {
        Self {
            timeout: Duration::from_secs(2),
        }
    }
}

impl Default for PosthogFlagResolver {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait::async_trait]
impl FlagResolver for PosthogFlagResolver {
    async fn enabled(&self, flag: &str, distinct_id: Option<&str>) -> bool {
        match tokio::time::timeout(
            self.timeout,
            flags::enabled(flag, distinct_id.unwrap_or("")),
        )
        .await
        {
            Ok(v) => v,
            Err(_) => {
                tracing::warn!(flag, "flag lookup timed out; resolving OFF");
                false
            }
        }
    }
}

/// Fixed-set resolver: flags in the set are ON, all others OFF. For tests and offline runs.
#[derive(Default)]
pub struct StaticFlagResolver {
    on: HashSet<String>,
}

impl StaticFlagResolver {
    /// Resolver with the given flags enabled.
    pub fn new<I, S>(on: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            on: on.into_iter().map(Into::into).collect(),
        }
    }
}

#[async_trait::async_trait]
impl FlagResolver for StaticFlagResolver {
    async fn enabled(&self, flag: &str, _distinct_id: Option<&str>) -> bool {
        self.on.contains(flag)
    }
}
