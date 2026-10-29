//! The fail-safe client: env override -> TTL cache -> bounded remote lookup ->
//! last-known value -> safe default. Never returns an error to the caller.

use std::collections::HashMap;
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use config::{FlagCfg, LicenseCfg};

use crate::cache::LastKnown;
use crate::error::SourceError;
use crate::license::HttpLicenseSource;
use crate::posthog::HttpFlagSource;
use crate::{FlagLookup, FlagSettings, FlagSource, LicenseSource, Scope, Tier};

/// `(flag, distinct_id)` cache key.
type FlagKey = (String, String);
/// Single-flight gate; held across the live fetch.
type Gate = Arc<tokio::sync::Mutex<()>>;

/// Opt-out kill-switch for the teams mechanism: unseen/OFF => teams ON.
pub const DISABLE_TEAMS_FLAG: &str = "waddleai.disable-teams";

/// Minimum spacing of outage warnings per key (avoids log spam under load).
const WARN_INTERVAL: Duration = Duration::from_secs(60);
const WARN_KEYS_MAX: usize = 1024;
const INFLIGHT_MAX: usize = 4096;

/// Flag + license client with in-memory last-known caches. Cheap to share via `Arc`.
pub struct FlagClient {
    settings: FlagSettings,
    flag_src: Option<Arc<dyn FlagSource>>,
    license_src: Option<Arc<dyn LicenseSource>>,
    flags: LastKnown<(String, String), bool>,
    features: LastKnown<(Scope, String), bool>,
    tiers: LastKnown<Scope, Tier>,
    /// Unseen/failed verdicts are keyed on the flag NAME only: an undefined flag
    /// or a dead server is the same for every distinct_id.
    flag_unseen: LastKnown<String, ()>,
    /// Per-id null verdict: the flag exists but evaluated null for THIS id only.
    flag_null: LastKnown<FlagKey, ()>,
    flag_failed: LastKnown<String, ()>,
    /// Single-flight: one in-progress live fetch per `(flag, distinct_id)`.
    inflight: Mutex<HashMap<FlagKey, Gate>>,
    feature_failed: LastKnown<(Scope, String), ()>,
    tier_failed: LastKnown<Scope, ()>,
    warned: Mutex<HashMap<String, Instant>>,
}

/// `waddleai.memory-org-scope` -> `WADDLEAI_FLAG_MEMORY_ORG_SCOPE` (Python parity).
pub fn env_override_name(flag: &str) -> String {
    let suffix = flag.split_once('.').map_or(flag, |(_, s)| s);
    format!(
        "WADDLEAI_FLAG_{}",
        suffix.replace(['-', '.'], "_").to_ascii_uppercase()
    )
}

/// Runs `fut` bounded by `limit`; a timeout becomes a [`SourceError`].
async fn bounded<T>(
    limit: Duration,
    fut: impl Future<Output = Result<T, SourceError>>,
) -> Result<T, SourceError> {
    match tokio::time::timeout(limit, fut).await {
        Ok(r) => r,
        Err(_) => Err(SourceError::Unreachable("timed out".into())),
    }
}

impl FlagClient {
    /// Client over explicit sources; `None` means that backend is unconfigured.
    pub fn new(
        settings: FlagSettings,
        flag_src: Option<Arc<dyn FlagSource>>,
        license_src: Option<Arc<dyn LicenseSource>>,
    ) -> Self {
        let max = settings.cache_max_entries;
        Self {
            settings,
            flag_src,
            license_src,
            flags: LastKnown::new(max),
            features: LastKnown::new(max),
            tiers: LastKnown::new(max),
            flag_unseen: LastKnown::new(max),
            flag_null: LastKnown::new(max),
            inflight: Mutex::new(HashMap::new()),
            flag_failed: LastKnown::new(max),
            feature_failed: LastKnown::new(max),
            tier_failed: LastKnown::new(max),
            warned: Mutex::new(HashMap::new()),
        }
    }

    /// Production wiring from the `config` crate: PostHog is disabled when no
    /// project key is set, license checks when no license key is set.
    pub fn from_config(
        posthog: &FlagCfg,
        license: &LicenseCfg,
        settings: FlagSettings,
    ) -> Result<Self, SourceError> {
        let http = reqwest::Client::builder()
            .build()
            .map_err(|e| SourceError::Build(e.without_url().to_string()))?;
        let flag_src = posthog.key.clone().map(|key| {
            Arc::new(HttpFlagSource::new(
                http.clone(),
                &posthog.host,
                key,
                settings.flag_timeout,
            )) as Arc<dyn FlagSource>
        });
        let license_src = (!license.key.is_empty()).then(|| {
            Arc::new(HttpLicenseSource::new(
                http.clone(),
                &license.server_url,
                license.key.clone(),
                settings.product.clone(),
                settings.license_timeout,
            )) as Arc<dyn LicenseSource>
        });
        if flag_src.is_none() {
            tracing::warn!("POSTHOG_KEY unset: feature flags resolve to defaults");
        }
        if license_src.is_none() {
            tracing::warn!("LICENSE_KEY unset: entitlements denied, tier is free");
        }
        Ok(Self::new(settings, flag_src, license_src))
    }

    /// Number of cached `(flag, distinct_id)` entries (bounded by settings).
    pub fn cached_flag_entries(&self) -> usize {
        self.flags.len()
    }

    /// Logs a fallback warning naming `name` (flag/feature), at most once per
    /// [`WARN_INTERVAL`] per `(kind, name)` so a sustained outage cannot spam logs.
    fn warn_fallback(&self, kind: &str, name: &str, reason: &str) {
        let now = Instant::now();
        let key = format!("{kind}:{name}");
        let mut g = self.warned.lock().unwrap_or_else(|p| p.into_inner());
        if g.len() >= WARN_KEYS_MAX && !g.contains_key(&key) {
            // Evict the oldest throttle entry only; never reset them all.
            if let Some(oldest) = g.iter().min_by_key(|(_, t)| **t).map(|(k, _)| k.clone()) {
                g.remove(&oldest);
            }
        }
        if g.get(&key)
            .is_some_and(|t| now.duration_since(*t) < WARN_INTERVAL)
        {
            return;
        }
        g.insert(key, now);
        drop(g);
        tracing::warn!(target: "flags", kind, name, reason, "flag/license fallback in effect");
    }

    /// Feature flag, default OFF when unseen/unreachable. Never errors.
    pub async fn enabled(&self, flag: &str, distinct_id: &str) -> bool {
        self.enabled_or(flag, distinct_id, false).await
    }

    /// As [`enabled`](Self::enabled) with a caller default for the unknown case
    /// (the kill-switch path uses `false` = "not disabled").
    pub async fn enabled_or(&self, flag: &str, distinct_id: &str, default: bool) -> bool {
        if let Ok(v) = std::env::var(env_override_name(flag)) {
            return matches!(
                v.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            );
        }
        let id = if distinct_id.is_empty() {
            "server"
        } else {
            distinct_id
        };
        if let Some(v) = self.cached_flag(flag, id, default) {
            return v;
        }
        let Some(src) = &self.flag_src else {
            self.warn_fallback(
                "flag-unconfigured",
                flag,
                "POSTHOG_KEY unset; using default",
            );
            return default;
        };
        // Single-flight: concurrent misses for the same (flag, id) share one
        // live call. The guard releases on every exit (success, error, timeout,
        // cancellation, panic); followers re-check the caches once they hold it.
        let key = (flag.to_owned(), id.to_owned());
        let gate = self.gate(&key);
        let _held = gate.lock().await;
        if let Some(v) = self.cached_flag(flag, id, default) {
            return v;
        }
        match bounded(self.settings.flag_timeout, src.fetch(flag, id)).await {
            Ok(FlagLookup::Value(v)) => {
                self.flag_null.remove(&key);
                self.flags.put(key, v);
                self.flag_unseen.remove(flag);
                self.flag_failed.remove(flag);
                v
            }
            Ok(FlagLookup::Null) => {
                // Per-id only: never promoted flag-wide (cross-tenant poisoning).
                self.flag_null.put(key, ());
                self.warn_fallback("flag-null", flag, "flag null for this id; using default");
                default
            }
            Ok(FlagLookup::Absent) => {
                self.flag_unseen.put(flag.to_owned(), ());
                self.warn_fallback("flag-unseen", flag, "flag unknown to server; using default");
                default
            }
            Err(e) => {
                self.flag_failed.put(flag.to_owned(), ());
                let reason = format!("lookup failed ({e}); using last-known or default");
                self.warn_fallback("flag-outage", flag, &reason);
                self.flags.any(&key).map_or(default, |(v, _)| v)
            }
        }
    }

    /// Cache-only resolution (silent: a hit is the success path, not a fallback).
    /// Order: per-id positive -> per-id null -> flag-wide unseen -> flag-wide failure.
    fn cached_flag(&self, flag: &str, id: &str, default: bool) -> Option<bool> {
        let key = (flag.to_owned(), id.to_owned());
        if let Some(v) = self.flags.fresh(&key, self.settings.flag_ttl) {
            return Some(v);
        }
        if self.flag_null.fresh(&key, self.settings.flag_ttl).is_some() {
            return Some(default);
        }
        if self
            .flag_unseen
            .fresh(flag, self.settings.flag_ttl)
            .is_some()
        {
            return Some(default);
        }
        if self
            .flag_failed
            .fresh(flag, self.settings.failure_ttl)
            .is_some()
        {
            return Some(self.flags.any(&key).map_or(default, |(v, _)| v));
        }
        None
    }

    /// The single-flight gate for `key`, pruning idle gates once over budget.
    fn gate(&self, key: &FlagKey) -> Gate {
        let mut g = self.inflight.lock().unwrap_or_else(|p| p.into_inner());
        if g.len() >= INFLIGHT_MAX && !g.contains_key(key) {
            g.retain(|_, v| Arc::strong_count(v) > 1);
        }
        Arc::clone(g.entry(key.clone()).or_default())
    }

    /// Whether `feature` is entitled for `scope`. Stale-while-error up to
    /// `max_stale`; denies when never fetched or too old. Never errors.
    pub async fn has_feature(&self, feature: &str, scope: &Scope) -> bool {
        let key = (scope.clone(), feature.to_owned());
        if let Some(v) = self.features.fresh(&key, self.settings.license_ttl) {
            return v;
        }
        if self
            .feature_failed
            .fresh(&key, self.settings.failure_ttl)
            .is_some()
        {
            return self.stale_or(feature, self.features.any(&key), false);
        }
        let Some(src) = &self.license_src else {
            self.warn_fallback(
                "license-unconfigured",
                feature,
                "LICENSE_KEY unset; denying",
            );
            return false;
        };
        match bounded(self.settings.license_timeout, src.feature(feature, scope)).await {
            Ok(v) => {
                self.features.put(key, v);
                v
            }
            Err(e) => {
                self.feature_failed.put(key.clone(), ());
                let reason = format!("lookup failed ({e}); serving stale or denying");
                self.warn_fallback("license-outage", feature, &reason);
                self.stale_or(feature, self.features.any(&key), false)
            }
        }
    }

    /// Licensed tier for `scope`; last-known (within `max_stale`) else `free`.
    pub async fn get_tier(&self, scope: &Scope) -> Tier {
        if let Some(t) = self.tiers.fresh(scope, self.settings.license_ttl) {
            return t;
        }
        if self
            .tier_failed
            .fresh(scope, self.settings.failure_ttl)
            .is_some()
        {
            return self.stale_or(&scope.tenant_id, self.tiers.any(scope), Tier::Free);
        }
        let Some(src) = &self.license_src else {
            self.warn_fallback(
                "tier-unconfigured",
                &scope.tenant_id,
                "LICENSE_KEY unset; tier is free",
            );
            return Tier::Free;
        };
        match bounded(self.settings.license_timeout, src.tier(scope)).await {
            Ok(t) => {
                self.tiers.put(scope.clone(), t);
                t
            }
            Err(e) => {
                self.tier_failed.put(scope.clone(), ());
                let reason = format!("lookup failed ({e}); serving stale or free");
                self.warn_fallback("tier-outage", &scope.tenant_id, &reason);
                self.stale_or(&scope.tenant_id, self.tiers.any(scope), Tier::Free)
            }
        }
    }

    /// True when the scope's tier is at least `required` (tiers are cumulative).
    pub async fn has_tier(&self, required: Tier, scope: &Scope) -> bool {
        self.get_tier(scope).await >= required
    }

    /// `waddleai.disable-teams` kill-switch resolved for `auth::authenticate_api_key`:
    /// flag unseen/OFF/unreachable => `true` (teams on); flag ON => `false`.
    pub async fn teams_enabled(&self, scope: &Scope) -> bool {
        !self
            .enabled_or(DISABLE_TEAMS_FLAG, scope.distinct_id(), false)
            .await
    }

    /// A cached value served during an outage only while younger than `max_stale`.
    fn stale_or<V>(&self, name: &str, cached: Option<(V, Duration)>, deny: V) -> V {
        match cached {
            Some((v, age)) if age < self.settings.max_stale => v,
            Some(_) => {
                self.warn_fallback(
                    "license-stale-max",
                    name,
                    "cached value past max_stale; denying",
                );
                deny
            }
            None => deny,
        }
    }
}
