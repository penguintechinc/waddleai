//! Behavioural tests for the fail-safe flag + license client, driven through
//! fake sources (no network) -- the HTTP sources are covered in `http.rs`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use flags::{
    BoxFut, FlagClient, FlagSettings, FlagSource, LicenseSource, Scope, SourceError, Tier,
};

#[derive(Default)]
struct FakeFlags {
    down: AtomicBool,
    hang: AtomicBool,
    calls: AtomicUsize,
    values: Mutex<HashMap<String, bool>>,
}

impl FakeFlags {
    fn set(&self, k: &str, v: bool) {
        self.values.lock().unwrap().insert(k.to_owned(), v);
    }
}

impl FlagSource for FakeFlags {
    fn fetch<'a>(
        &'a self,
        flag: &'a str,
        _id: &'a str,
    ) -> BoxFut<'a, Result<Option<bool>, SourceError>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.hang.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_secs(3600)).await;
            }
            if self.down.load(Ordering::SeqCst) {
                return Err(SourceError::Unreachable("down".into()));
            }
            Ok(self.values.lock().unwrap().get(flag).copied())
        })
    }
}

#[derive(Default)]
struct FakeLicense {
    down: AtomicBool,
    calls: AtomicUsize,
    entitled: Mutex<HashMap<String, bool>>,
    tier: Mutex<Option<Tier>>,
}

impl LicenseSource for FakeLicense {
    fn feature<'a>(
        &'a self,
        feature: &'a str,
        _s: &'a Scope,
    ) -> BoxFut<'a, Result<bool, SourceError>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.down.load(Ordering::SeqCst) {
                return Err(SourceError::Unreachable("down".into()));
            }
            Ok(self
                .entitled
                .lock()
                .unwrap()
                .get(feature)
                .copied()
                .unwrap_or(false))
        })
    }
    fn tier<'a>(&'a self, _s: &'a Scope) -> BoxFut<'a, Result<Tier, SourceError>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.down.load(Ordering::SeqCst) {
                return Err(SourceError::Unreachable("down".into()));
            }
            Ok((*self.tier.lock().unwrap()).unwrap_or(Tier::Free))
        })
    }
}

fn settings(flag_ttl_ms: u64) -> FlagSettings {
    FlagSettings {
        flag_ttl: Duration::from_millis(flag_ttl_ms),
        flag_timeout: Duration::from_millis(50),
        license_ttl: Duration::from_millis(0),
        license_timeout: Duration::from_millis(50),
        ..FlagSettings::default()
    }
}

fn client(f: &Arc<FakeFlags>, l: &Arc<FakeLicense>, ttl_ms: u64) -> FlagClient {
    FlagClient::new(settings(ttl_ms), Some(f.clone()), Some(l.clone()))
}

fn scope() -> Scope {
    Scope::new("tenant-1").with_team("team-1")
}

#[tokio::test]
async fn unseen_flag_defaults_off() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 0);
    assert!(!c.enabled("waddleai.never_seen", "u1").await);
    // Unconfigured client (no sources at all) is also OFF.
    assert!(
        !FlagClient::new(settings(0), None, None)
            .enabled("waddleai.x", "u")
            .await
    );
}

#[tokio::test]
async fn server_unreachable_uses_last_known() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    let c = client(&f, &l, 0); // ttl 0 => always refetch
    assert!(c.enabled("waddleai.feat", "u1").await);
    f.down.store(true, Ordering::SeqCst);
    assert!(
        c.enabled("waddleai.feat", "u1").await,
        "outage serves last-known"
    );
    // Last-known OFF stays OFF too.
    f.down.store(false, Ordering::SeqCst);
    f.set("waddleai.feat", false);
    assert!(!c.enabled("waddleai.feat", "u1").await);
    f.down.store(true, Ordering::SeqCst);
    assert!(!c.enabled("waddleai.feat", "u1").await);
}

#[tokio::test]
async fn last_known_absent_and_unreachable_is_off() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let c = client(&f, &l, 0);
    assert!(!c.enabled("waddleai.feat", "u1").await);
    // Hung server is bounded by the timeout and also fails safe.
    f.down.store(false, Ordering::SeqCst);
    f.hang.store(true, Ordering::SeqCst);
    assert!(!c.enabled("waddleai.feat", "u1").await);
}

#[tokio::test]
async fn license_entitlement_gates_feature() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 0);
    assert!(!c.has_feature("pii_ner_detection", &scope()).await);
    l.entitled
        .lock()
        .unwrap()
        .insert("pii_ner_detection".into(), true);
    assert!(c.has_feature("pii_ner_detection", &scope()).await);
    // Outage after a good fetch: stale-while-error keeps the entitlement.
    l.down.store(true, Ordering::SeqCst);
    assert!(c.has_feature("pii_ner_detection", &scope()).await);
    // Never-fetched feature during outage is denied.
    assert!(!c.has_feature("other", &scope()).await);
}

#[tokio::test]
async fn license_stale_past_max_is_denied() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    l.entitled.lock().unwrap().insert("x".into(), true);
    let mut s = settings(0);
    s.max_stale = Duration::from_millis(0);
    let c = FlagClient::new(s, Some(f), Some(l.clone()));
    assert!(c.has_feature("x", &scope()).await);
    l.down.store(true, Ordering::SeqCst);
    assert!(
        !c.has_feature("x", &scope()).await,
        "cache older than max_stale"
    );
}

#[tokio::test]
async fn unconfigured_license_denies() {
    let c = FlagClient::new(settings(0), None, None);
    assert!(!c.has_feature("x", &scope()).await);
    assert_eq!(c.get_tier(&scope()).await, Tier::Free);
}

#[tokio::test]
async fn tier_resolution_and_ordering() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 0);
    assert_eq!(c.get_tier(&scope()).await, Tier::Free);
    *l.tier.lock().unwrap() = Some(Tier::Professional);
    assert_eq!(c.get_tier(&scope()).await, Tier::Professional);
    assert!(c.has_tier(Tier::Free, &scope()).await);
    assert!(c.has_tier(Tier::Professional, &scope()).await);
    assert!(!c.has_tier(Tier::Enterprise, &scope()).await);
    // Outage: last-known tier, then Free for a never-seen scope.
    l.down.store(true, Ordering::SeqCst);
    assert_eq!(c.get_tier(&scope()).await, Tier::Professional);
    assert_eq!(c.get_tier(&Scope::new("other")).await, Tier::Free);
    assert!(Tier::Free < Tier::Professional && Tier::Professional < Tier::Enterprise);
}

#[test]
fn tier_parse_and_names() {
    assert_eq!("free".parse::<Tier>().unwrap(), Tier::Free);
    assert_eq!("Professional".parse::<Tier>().unwrap(), Tier::Professional);
    assert_eq!("ENTERPRISE".parse::<Tier>().unwrap(), Tier::Enterprise);
    assert!("platinum".parse::<Tier>().is_err());
    assert_eq!(Tier::Enterprise.as_str(), "enterprise");
    assert_eq!(Tier::Free.to_string(), "free");
}

#[tokio::test]
async fn ttl_fresh_value_skips_server() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    let c = client(&f, &l, 60_000);
    assert!(c.enabled("waddleai.feat", "u1").await);
    assert!(c.enabled("waddleai.feat", "u1").await);
    assert_eq!(f.calls.load(Ordering::SeqCst), 1);
    // distinct ids are cached independently.
    assert!(c.enabled("waddleai.feat", "u2").await);
    assert_eq!(f.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn teams_enabled_kill_switch_semantics() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 0);
    // Unseen => mechanism ON.
    assert!(c.teams_enabled(&scope()).await);
    // Kill-switch ON => legacy fallback => teams disabled.
    f.set("waddleai.disable-teams", true);
    assert!(!c.teams_enabled(&scope()).await);
    // Outage keeps last-known (disabled).
    f.down.store(true, Ordering::SeqCst);
    assert!(!c.teams_enabled(&scope()).await);
    // Kill-switch OFF => teams on.
    f.down.store(false, Ordering::SeqCst);
    f.set("waddleai.disable-teams", false);
    assert!(c.teams_enabled(&scope()).await);
}

#[tokio::test]
async fn teams_enabled_unreachable_no_cache_is_on() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let c = client(&f, &l, 0);
    assert!(
        c.teams_enabled(&scope()).await,
        "dead server must not disable teams"
    );
    assert!(
        FlagClient::new(settings(0), None, None)
            .teams_enabled(&scope())
            .await
    );
}

#[tokio::test]
async fn enabled_or_honours_caller_default_when_unknown() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let c = client(&f, &l, 0);
    assert!(c.enabled_or("waddleai.k", "u", true).await);
    assert!(!c.enabled_or("waddleai.k", "u", false).await);
}

#[tokio::test]
async fn cache_is_bounded() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let mut s = settings(60_000);
    s.cache_max_entries = 2;
    let c = FlagClient::new(s, Some(f.clone()), Some(l));
    for i in 0..5 {
        f.set("waddleai.feat", true);
        assert!(c.enabled("waddleai.feat", &format!("u{i}")).await);
    }
    assert!(c.cached_flag_entries() <= 2);
}

#[test]
fn env_override_name_matches_python() {
    assert_eq!(
        flags::env_override_name("waddleai.memory-org-scope"),
        "WADDLEAI_FLAG_MEMORY_ORG_SCOPE"
    );
}

#[tokio::test]
async fn env_override_wins_without_server() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 0);
    std::env::set_var("WADDLEAI_FLAG_TEST_ENV_OVERRIDE_ON", "yes");
    std::env::set_var("WADDLEAI_FLAG_TEST_ENV_OVERRIDE_OFF", "0");
    assert!(c.enabled("waddleai.test-env-override-on", "u").await);
    assert!(!c.enabled("waddleai.test-env-override-off", "u").await);
    assert_eq!(f.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn global_helpers_fail_safe_then_use_installed_client() {
    // Not installed yet: fail-safe defaults.
    assert!(!flags::enabled("waddleai.g", "u").await);
    assert!(flags::teams_enabled(&scope()).await);
    assert!(!flags::has_feature("x", &scope()).await);
    assert_eq!(flags::get_tier(&scope()).await, Tier::Free);
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.g", true);
    assert!(flags::install(client(&f, &l, 0)).is_ok());
    assert!(flags::enabled("waddleai.g", "u").await);
    assert!(
        flags::install(client(&f, &l, 0)).is_err(),
        "second install rejected"
    );
}
