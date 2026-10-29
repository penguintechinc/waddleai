//! Behavioural tests for the fail-safe flag + license client, driven through
//! fake sources (no network) -- the HTTP sources are covered in `http.rs`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use flags::{
    BoxFut, FlagClient, FlagLookup, FlagSettings, FlagSource, LicenseSource, Scope, SourceError,
    Tier,
};

#[derive(Default)]
struct FakeFlags {
    down: AtomicBool,
    hang: AtomicBool,
    delay_ms: AtomicUsize,
    calls: AtomicUsize,
    values: Mutex<HashMap<String, bool>>,
    null_ids: Mutex<std::collections::HashSet<String>>,
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
        id: &'a str,
    ) -> BoxFut<'a, Result<FlagLookup, SourceError>> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let d = self.delay_ms.load(Ordering::SeqCst) as u64;
            if d > 0 {
                tokio::time::sleep(Duration::from_millis(d)).await;
            }
            if self.hang.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_secs(3600)).await;
            }
            if self.down.load(Ordering::SeqCst) {
                return Err(SourceError::Unreachable("down".into()));
            }
            if self.null_ids.lock().unwrap().contains(id) {
                return Ok(FlagLookup::Null);
            }
            Ok(self
                .values
                .lock()
                .unwrap()
                .get(flag)
                .map_or(FlagLookup::Absent, |v| FlagLookup::Value(*v)))
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
        failure_ttl: Duration::from_millis(0),
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

#[tokio::test]
async fn unseen_flag_is_negative_cached() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 60_000);
    assert!(c.teams_enabled(&scope()).await);
    assert!(c.teams_enabled(&scope()).await);
    assert!(!c.enabled("waddleai.unseen", "u").await);
    assert!(!c.enabled("waddleai.unseen", "u").await);
    // one call for disable-teams, one for waddleai.unseen
    assert_eq!(f.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn failures_are_negative_cached() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    l.down.store(true, Ordering::SeqCst);
    let mut s = settings(0);
    s.failure_ttl = Duration::from_secs(60);
    let c = FlagClient::new(s, Some(f.clone()), Some(l.clone()));
    for _ in 0..5 {
        assert!(!c.enabled("waddleai.feat", "u").await);
        assert!(c.teams_enabled(&scope()).await);
        assert!(!c.has_feature("x", &scope()).await);
        assert_eq!(c.get_tier(&scope()).await, Tier::Free);
    }
    // flags: 1 per distinct flag (feat, disable-teams); license: 1 feature + 1 tier
    assert_eq!(f.calls.load(Ordering::SeqCst), 2);
    assert_eq!(l.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn failure_cache_still_serves_last_known() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    let mut s = settings(0);
    s.failure_ttl = Duration::from_secs(60);
    let c = FlagClient::new(s, Some(f.clone()), Some(l));
    assert!(c.enabled("waddleai.feat", "u").await);
    f.down.store(true, Ordering::SeqCst);
    for _ in 0..3 {
        assert!(c.enabled("waddleai.feat", "u").await);
    }
    assert_eq!(f.calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn unseen_verdict_is_flag_keyed_across_many_scopes() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 60_000); // default cache_max_entries = 2048
    for i in 0..3000 {
        assert!(c.teams_enabled(&Scope::new(format!("tenant-{i}"))).await);
    }
    assert_eq!(
        f.calls.load(Ordering::SeqCst),
        1,
        ">2048 scopes, one server call"
    );
}

#[tokio::test]
async fn failure_verdict_is_flag_keyed_across_many_scopes() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let mut s = settings(0);
    s.failure_ttl = Duration::from_secs(60);
    let c = FlagClient::new(s, Some(f.clone()), Some(l));
    for i in 0..3000 {
        assert!(c.teams_enabled(&Scope::new(format!("tenant-{i}"))).await);
    }
    assert_eq!(f.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn concurrent_cold_burst_is_single_flight() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    f.delay_ms.store(20, Ordering::SeqCst);
    let c = Arc::new(client(&f, &l, 60_000));
    let mut hs = Vec::new();
    for _ in 0..25 {
        let c = c.clone();
        hs.push(tokio::spawn(async move {
            c.enabled("waddleai.feat", "u1").await
        }));
    }
    for h in hs {
        assert!(h.await.unwrap());
    }
    assert_eq!(f.calls.load(Ordering::SeqCst), 1, "exactly one live call");
}

#[tokio::test]
async fn single_flight_cancellation_releases_gate() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    f.hang.store(true, Ordering::SeqCst);
    let c = Arc::new(client(&f, &l, 60_000));
    let c2 = c.clone();
    let h = tokio::spawn(async move { c2.enabled("waddleai.feat", "u1").await });
    tokio::time::sleep(Duration::from_millis(10)).await;
    h.abort();
    let _ = h.await;
    f.hang.store(false, Ordering::SeqCst);
    assert!(
        c.enabled("waddleai.feat", "u1").await,
        "gate not wedged by a cancelled leader"
    );
}

#[tokio::test]
async fn failure_entry_expires_and_real_value_resumes() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    f.down.store(true, Ordering::SeqCst);
    let mut s = settings(0);
    s.failure_ttl = Duration::from_millis(60);
    let c = FlagClient::new(s, Some(f.clone()), Some(l));
    assert!(!c.enabled("waddleai.feat", "u").await);
    assert!(!c.enabled("waddleai.feat", "u").await);
    assert_eq!(f.calls.load(Ordering::SeqCst), 1);
    f.down.store(false, Ordering::SeqCst);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        c.enabled("waddleai.feat", "u").await,
        "recovered after failure_ttl"
    );
}

#[tokio::test]
async fn unseen_flag_later_defined_supersedes_negative_entry() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 60);
    assert!(!c.enabled("waddleai.feat", "u").await);
    f.set("waddleai.feat", true);
    assert!(
        !c.enabled("waddleai.feat", "u").await,
        "still negative within TTL"
    );
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(c.enabled("waddleai.feat", "u").await, "defined after TTL");
    assert!(
        c.enabled("waddleai.feat", "other").await,
        "positive removed unseen verdict"
    );
}

#[tokio::test]
async fn warn_throttle_map_is_bounded_without_reset() {
    // Many distinct failing flags must not grow or panic the throttle map.
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let c = client(&f, &l, 0);
    for i in 0..1300 {
        assert!(!c.enabled(&format!("waddleai.f{i}"), "u").await);
    }
}

#[tokio::test]
async fn per_id_null_does_not_poison_other_ids() {
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.set("waddleai.feat", true);
    f.null_ids.lock().unwrap().insert("B".into());
    let c = client(&f, &l, 60_000);
    assert!(c.enabled("waddleai.feat", "A").await);
    assert!(
        !c.enabled("waddleai.feat", "B").await,
        "B is null => default"
    );
    assert!(
        c.enabled("waddleai.feat", "A").await,
        "A's real value not masked by B's null"
    );
    assert_eq!(f.calls.load(Ordering::SeqCst), 2);
    assert!(
        c.enabled("waddleai.feat", "C").await,
        "cold id C must fetch, not inherit B's null"
    );
    assert_eq!(f.calls.load(Ordering::SeqCst), 3);
    // B's own null is cached per-id.
    assert!(!c.enabled("waddleai.feat", "B").await);
    assert_eq!(f.calls.load(Ordering::SeqCst), 3);
}

/// Captures WARN+ events as "level kind=.. name=.. reason=.." lines.
#[derive(Clone, Default)]
struct Cap(Arc<Mutex<Vec<String>>>);

struct Fields(String);
impl tracing::field::Visit for Fields {
    fn record_str(&mut self, f: &tracing::field::Field, v: &str) {
        self.0.push_str(&format!(" {}={}", f.name(), v));
    }
    fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
        self.0.push_str(&format!(" {}={:?}", f.name(), v));
    }
}

impl tracing::Subscriber for Cap {
    fn enabled(&self, m: &tracing::Metadata<'_>) -> bool {
        *m.level() <= tracing::Level::WARN
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, e: &tracing::Event<'_>) {
        let mut v = Fields(String::new());
        e.record(&mut v);
        self.0.lock().unwrap().push(v.0);
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

fn count(cap: &Cap, needle: &str) -> usize {
    cap.0
        .lock()
        .unwrap()
        .iter()
        .filter(|l| l.contains(needle))
        .count()
}

#[tokio::test]
async fn negative_cache_hit_emits_no_warn() {
    let cap = Cap::default();
    let _g = tracing::subscriber::set_default(cap.clone());
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    let c = client(&f, &l, 60_000);
    assert!(!c.enabled("waddleai.unseen", "u").await);
    assert_eq!(
        count(&cap, "name=waddleai.unseen"),
        1,
        "transition warns once"
    );
    for i in 0..10 {
        assert!(!c.enabled("waddleai.unseen", &format!("id{i}")).await);
    }
    assert_eq!(count(&cap, "name=waddleai.unseen"), 1, "hits are silent");
    // Failure-cache hits are silent too.
    f.down.store(true, Ordering::SeqCst);
    let mut s = settings(0);
    s.failure_ttl = Duration::from_secs(60);
    let c2 = FlagClient::new(s, Some(f.clone()), None);
    assert!(!c2.enabled("waddleai.dead", "u").await);
    let before = count(&cap, "name=waddleai.dead");
    for _ in 0..10 {
        assert!(!c2.enabled("waddleai.dead", "u").await);
    }
    assert_eq!(count(&cap, "name=waddleai.dead"), before);
}

#[tokio::test]
async fn stale_license_warn_is_named_and_throttled() {
    let cap = Cap::default();
    let _g = tracing::subscriber::set_default(cap.clone());
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    l.entitled
        .lock()
        .unwrap()
        .insert("pii_ner_detection".into(), true);
    let mut s = settings(0);
    s.max_stale = Duration::from_millis(0);
    s.failure_ttl = Duration::from_secs(60);
    let c = FlagClient::new(s, Some(f), Some(l.clone()));
    assert!(c.has_feature("pii_ner_detection", &scope()).await);
    l.down.store(true, Ordering::SeqCst);
    for _ in 0..6 {
        assert!(!c.has_feature("pii_ner_detection", &scope()).await);
    }
    assert_eq!(
        count(&cap, "kind=license-stale-max name=pii_ner_detection"),
        1,
        "named, once despite 6 denials"
    );
}

#[tokio::test]
async fn warn_throttle_evicts_oldest_only() {
    let cap = Cap::default();
    let _g = tracing::subscriber::set_default(cap.clone());
    let (f, l) = (
        Arc::new(FakeFlags::default()),
        Arc::new(FakeLicense::default()),
    );
    f.down.store(true, Ordering::SeqCst);
    let c = client(&f, &l, 0); // failure_ttl 0 => every call fetches + tries to warn
    for i in 0..1024 {
        c.enabled(&format!("waddleai.f{i}"), "u").await;
    }
    // Cap reached: this insert evicts only the oldest (f0).
    c.enabled("waddleai.f1024", "u").await;
    let (before1, before0) = (
        count(&cap, "name=waddleai.f1 "),
        count(&cap, "name=waddleai.f0 "),
    );
    c.enabled("waddleai.f1", "u").await; // older-but-not-oldest: still throttled
    assert_eq!(
        count(&cap, "name=waddleai.f1 "),
        before1,
        "f1 survived eviction"
    );
    c.enabled("waddleai.f0", "u").await; // evicted: warns again
    assert_eq!(
        count(&cap, "name=waddleai.f0 "),
        before0 + 1,
        "f0 was the evicted one"
    );
}
