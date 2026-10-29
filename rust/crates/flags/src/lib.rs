//! Fail-safe PostHog feature flags and license-server entitlements.
//!
//! Mirrors `shared/utils/feature_flags.py` and `shared/licensing/python_client.py`:
//! a lookup that errors or times out never reaches the caller -- it yields the
//! last-known value, and if none exists the safe default (OFF for feature flags,
//! `free` tier, ON for the `waddleai.disable-teams` kill-switch's mechanism).
//!
//! Build a [`FlagClient`] from config with [`FlagClient::from_config`], then
//! either use it directly or [`install`] it once and use the module-level
//! helpers ([`enabled`], [`has_feature`], [`get_tier`], [`teams_enabled`]).

mod cache;
mod client;
mod error;
mod license;
mod posthog;
mod scope;
mod settings;
mod tier;

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

pub use client::{env_override_name, FlagClient, DISABLE_TEAMS_FLAG};
pub use error::SourceError;
pub use license::HttpLicenseSource;
pub use posthog::HttpFlagSource;
pub use scope::Scope;
pub use settings::FlagSettings;
pub use tier::{Tier, TierParseError};

/// Boxed `Send` future returned by the source traits (keeps them object-safe).
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// What a flag server said about one `(flag, distinct_id)` evaluation.
///
/// The absent/null split matters for caching: an [`Absent`](Self::Absent) key is
/// undefined for everyone (safe to cache flag-wide), while [`Null`](Self::Null)
/// is specific to this distinct_id and must never be shared across ids.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FlagLookup {
    /// The flag evaluated to a value for this id.
    Value(bool),
    /// The flag key is not defined on the server (global).
    Absent,
    /// The flag exists but evaluated to null for this id (per-id).
    Null,
}

/// Remote source of PostHog flag values.
pub trait FlagSource: Send + Sync {
    /// Transport/server failures are `Err`; see [`FlagLookup`] for the rest.
    fn fetch<'a>(
        &'a self,
        flag: &'a str,
        distinct_id: &'a str,
    ) -> BoxFut<'a, Result<FlagLookup, SourceError>>;
}

/// Remote source of license entitlements and tier.
pub trait LicenseSource: Send + Sync {
    /// Whether `feature` is entitled for `scope`.
    fn feature<'a>(
        &'a self,
        feature: &'a str,
        scope: &'a Scope,
    ) -> BoxFut<'a, Result<bool, SourceError>>;
    /// The licensed tier for `scope`.
    fn tier<'a>(&'a self, scope: &'a Scope) -> BoxFut<'a, Result<Tier, SourceError>>;
}

static GLOBAL: OnceLock<FlagClient> = OnceLock::new();
static WARNED_UNINSTALLED: AtomicBool = AtomicBool::new(false);

/// The installed client; warns once if a decision-gating helper runs before `install`.
fn global() -> Option<&'static FlagClient> {
    let c = GLOBAL.get();
    if c.is_none() && !WARNED_UNINSTALLED.swap(true, Ordering::Relaxed) {
        tracing::warn!(
            target: "flags",
            "flags used before install(): serving fail-safe defaults (flags OFF, teams ON, tier free)"
        );
    }
    c
}

/// A process-wide client was already installed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("flag client already installed")]
pub struct AlreadyInstalled;

/// Installs the process-wide client once; a second call is rejected.
pub fn install(client: FlagClient) -> Result<(), AlreadyInstalled> {
    GLOBAL.set(client).map_err(|_| AlreadyInstalled)
}

/// Global [`FlagClient::enabled`]; OFF if no client is installed.
pub async fn enabled(flag: &str, distinct_id: &str) -> bool {
    match global() {
        Some(c) => c.enabled(flag, distinct_id).await,
        None => false,
    }
}

/// Global [`FlagClient::has_feature`]; denied if no client is installed.
pub async fn has_feature(feature: &str, scope: &Scope) -> bool {
    match global() {
        Some(c) => c.has_feature(feature, scope).await,
        None => false,
    }
}

/// Global [`FlagClient::get_tier`]; `free` if no client is installed.
pub async fn get_tier(scope: &Scope) -> Tier {
    match global() {
        Some(c) => c.get_tier(scope).await,
        None => Tier::Free,
    }
}

/// Global [`FlagClient::teams_enabled`]; teams ON if no client is installed.
pub async fn teams_enabled(scope: &Scope) -> bool {
    match global() {
        Some(c) => c.teams_enabled(scope).await,
        None => true,
    }
}
