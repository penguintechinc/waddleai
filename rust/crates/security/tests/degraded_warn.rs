//! The degraded-policy WARN is rate-limited per (org, phase). Own binary because counting
//! events needs a process-global subscriber that other tests must not share.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use pipeline::{NoNer, StaticFlagResolver};
use security::{
    ContentFilter, FailModes, LicenseGate, NerTier, RawOrgConfig, RulesError, RulesSource,
    RulesStore, NER_TIER_FLAG,
};
use tracing_subscriber::layer::SubscriberExt;

static WARNS: AtomicUsize = AtomicUsize::new(0);

struct WarnCounter;
impl<S: tracing::Subscriber> tracing_subscriber::Layer<S> for WarnCounter {
    fn on_event(&self, e: &tracing::Event<'_>, _c: tracing_subscriber::layer::Context<'_, S>) {
        if *e.metadata().level() == tracing::Level::WARN {
            WARNS.fetch_add(1, Ordering::SeqCst);
        }
    }
}

struct Empty;
#[async_trait]
impl RulesSource for Empty {
    async fn load(&self, _org: Option<i64>) -> Result<RawOrgConfig, RulesError> {
        Ok(RawOrgConfig::default())
    }
}
struct Yes;
#[async_trait]
impl LicenseGate for Yes {
    async fn has_feature(&self, _f: &str, _o: Option<i64>) -> bool {
        true
    }
}

#[tokio::test]
async fn degraded_warn_is_rate_limited_per_org_and_phase() {
    tracing::subscriber::set_global_default(tracing_subscriber::registry().with(WarnCounter))
        .expect("sole test in this binary installs the global subscriber");
    let ner = NerTier::new(
        Arc::new(NoNer),
        Arc::new(StaticFlagResolver::new([NER_TIER_FLAG])),
        Arc::new(Yes),
    );
    let store = Arc::new(RulesStore::new(Arc::new(Empty), None));
    let f = ContentFilter::new(store, Some(ner), FailModes::default()).expect("builds");

    for _ in 0..20 {
        assert!(f.filter_input("x", None, Some(1), None).await.degraded);
    }
    assert_eq!(
        WARNS.load(Ordering::SeqCst),
        1,
        "20 degraded calls, one WARN"
    );
    // A different org and a different phase each get their own single WARN.
    for _ in 0..5 {
        f.filter_input("x", None, Some(2), None).await;
        f.filter_output("x", None, Some(1), None).await;
    }
    assert_eq!(WARNS.load(Ordering::SeqCst), 3);
}
