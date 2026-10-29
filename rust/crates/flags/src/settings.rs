//! Tunables for TTLs, timeouts and bounds (env names match the Python modules).

use std::time::Duration;

/// Cache/timeout settings. `from_env` reads the same variables as Python.
#[derive(Debug, Clone)]
pub struct FlagSettings {
    /// `FEATURE_FLAG_TTL_SECONDS` (default 30s): freshness of a resolved flag.
    pub flag_ttl: Duration,
    /// `FEATURE_FLAG_TIMEOUT_SECONDS` (default 3s): bound on a PostHog call.
    pub flag_timeout: Duration,
    /// License entitlement/tier freshness (default 300s).
    pub license_ttl: Duration,
    /// Bound on a license-server call (default 3s).
    pub license_timeout: Duration,
    /// `LICENSE_MAX_STALE_SECONDS` (default 7 days): oldest entitlement served in an outage.
    pub max_stale: Duration,
    /// `FEATURE_FLAG_FAILURE_TTL_SECONDS` (default 5s): after a failed lookup, serve
    /// last-known/default without another live call for this long.
    pub failure_ttl: Duration,
    /// `FEATURE_FLAG_CACHE_MAX_ENTRIES` (default 2048), applied to each cache.
    pub cache_max_entries: usize,
    /// `PRODUCT_NAME` (default `waddleai`): product sent to the license server.
    pub product: String,
}

impl Default for FlagSettings {
    fn default() -> Self {
        Self {
            flag_ttl: Duration::from_secs(30),
            flag_timeout: Duration::from_secs(3),
            failure_ttl: Duration::from_secs(5),
            license_ttl: Duration::from_secs(300),
            license_timeout: Duration::from_secs(3),
            max_stale: Duration::from_secs(7 * 24 * 60 * 60),
            cache_max_entries: 2048,
            product: "waddleai".to_owned(),
        }
    }
}

fn secs(var: &str, default: Duration) -> Duration {
    std::env::var(var)
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .filter(|s| s.is_finite() && *s >= 0.0)
        .map_or(default, Duration::from_secs_f64)
}

impl FlagSettings {
    /// Reads overrides from the environment; bad values fall back to defaults.
    pub fn from_env() -> Self {
        let d = Self::default();
        Self {
            flag_ttl: secs("FEATURE_FLAG_TTL_SECONDS", d.flag_ttl),
            flag_timeout: secs("FEATURE_FLAG_TIMEOUT_SECONDS", d.flag_timeout),
            failure_ttl: secs("FEATURE_FLAG_FAILURE_TTL_SECONDS", d.failure_ttl),
            max_stale: secs("LICENSE_MAX_STALE_SECONDS", d.max_stale),
            cache_max_entries: std::env::var("FEATURE_FLAG_CACHE_MAX_ENTRIES")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(d.cache_max_entries),
            product: std::env::var("PRODUCT_NAME")
                .ok()
                .filter(|v| !v.is_empty())
                .unwrap_or(d.product),
            ..d
        }
    }
}
