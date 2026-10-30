//! Per-org routing registry cache: offers, aliases, assignments, policy and rules.
//!
//! Seeded at startup and lazy-loaded on a miss, so a cold cache can never route with an empty
//! (fail-closed) model universe. Entries are refreshed on Valkey key-deletion events for the
//! assignment/policy keys the Python management API `DEL`s; tables with no invalidation key
//! (offers, aliases, rules) are bounded by a short L1 age. The L1 lock is never held across an
//! await: readers clone an `Arc` snapshot out under the lock.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;

use crate::alias::AliasRow;
use crate::cascade::HeuristicRule;
use crate::offers::{build_offers, ModelOffer, RawModelConfig};
use crate::policy::RoutingPolicy;

/// Entries older than this are re-read (aliases/offers have no invalidation signal).
pub const L1_MAX_AGE: Duration = Duration::from_secs(30);
/// Key prefix of cached assignments in the Python resolver (`waddleai:route:assign:{org}:{tt}`).
pub const ASSIGN_KEY_PREFIX: &str = "waddleai:route:assign:";
/// Key prefix of cached policies (`waddleai:route:policy:{org}`).
pub const POLICY_KEY_PREFIX: &str = "waddleai:route:policy:";

/// A registry load failure.
#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    /// The backing source failed and nothing is cached for the org.
    #[error("registry source unavailable: {0}")]
    Source(String),
}

/// One `model_assignments` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AssignmentRow {
    /// Org scope, `None` = global.
    pub org: Option<i64>,
    /// Kebab-case tool type.
    pub tool_type: String,
    /// Default model.
    pub default_model: String,
    /// Escalation model.
    pub escalation_model: Option<String>,
}

/// Everything the decision needs for one org.
#[derive(Debug, Clone, Default)]
pub struct OrgRouting {
    /// Candidate offers (global universe).
    pub offers: Vec<ModelOffer>,
    /// Enabled aliases (org + global).
    pub aliases: Vec<AliasRow>,
    /// Enabled assignments (org + global).
    pub assignments: Vec<AssignmentRow>,
    /// Resolved policy.
    pub policy: RoutingPolicy,
    /// Heuristic rules (org + global).
    pub rules: Vec<HeuristicRule>,
}

impl OrgRouting {
    /// Org assignment first, else the global one.
    pub fn assignment(&self, tool_type: &str, org: i64) -> Option<&AssignmentRow> {
        self.assignments
            .iter()
            .find(|a| a.tool_type == tool_type && a.org == Some(org))
            .or_else(|| {
                self.assignments
                    .iter()
                    .find(|a| a.tool_type == tool_type && a.org.is_none())
            })
    }

    /// Builds a snapshot from raw model rows plus the other tables.
    pub fn from_parts(
        models: &[RawModelConfig],
        aliases: Vec<AliasRow>,
        assignments: Vec<AssignmentRow>,
        policy: RoutingPolicy,
        rules: Vec<HeuristicRule>,
    ) -> Result<Self, crate::RoutingError> {
        Ok(Self {
            offers: build_offers(models)?,
            aliases,
            assignments,
            policy,
            rules,
        })
    }
}

/// Authoritative source of per-org routing data (the database in production).
#[async_trait]
pub trait RegistrySource: Send + Sync {
    /// Loads the snapshot for `org` (0 = global scope only).
    async fn load(&self, org: i64) -> Result<OrgRouting, RegistryError>;
}

/// One L1 entry with its load time.
struct Entry {
    snap: Arc<OrgRouting>,
    loaded: Instant,
}

/// Outcome of [`RegistryCache::seed`].
#[derive(Debug, Default, PartialEq, Eq)]
pub struct SeedReport {
    /// Scopes loaded.
    pub loaded: usize,
    /// Scopes whose load failed (they lazy-load on first use).
    pub failed: usize,
}

/// Normalises an org id: anything non-positive is the global scope.
fn scope_of(org: i64) -> i64 {
    org.max(0)
}

/// The registry cache the `RoutingStage` reads.
pub struct RegistryCache {
    l1: RwLock<HashMap<i64, Entry>>,
    source: Arc<dyn RegistrySource>,
}

impl RegistryCache {
    /// Cache over `source`.
    pub fn new(source: Arc<dyn RegistrySource>) -> Self {
        Self {
            l1: RwLock::new(HashMap::new()),
            source,
        }
    }

    /// Startup seed: loads the global scope plus `orgs`; failures lazy-load later.
    pub async fn seed(&self, orgs: &[i64]) -> SeedReport {
        let mut report = SeedReport::default();
        for scope in std::iter::once(0).chain(orgs.iter().map(|o| scope_of(*o))) {
            match self.source.load(scope).await {
                Ok(s) => {
                    self.put(scope, s);
                    report.loaded += 1;
                }
                Err(e) => {
                    tracing::error!(error = %e, "routing registry seed failed; will lazy-load");
                    report.failed += 1;
                }
            }
        }
        report
    }

    /// Returns the org snapshot: a fresh L1 hit, else a lazy load. A stale entry whose refresh
    /// fails is served as-is; only a cold miss with a dead source is an error.
    pub async fn get(&self, org: i64) -> Result<Arc<OrgRouting>, RegistryError> {
        let scope = scope_of(org);
        let stale = {
            let guard = self.l1.read().unwrap_or_else(|p| p.into_inner());
            match guard.get(&scope) {
                Some(e) if e.loaded.elapsed() < L1_MAX_AGE => return Ok(Arc::clone(&e.snap)),
                Some(e) => Some(Arc::clone(&e.snap)),
                None => None,
            }
        };
        match self.source.load(scope).await {
            Ok(s) => Ok(self.put(scope, s)),
            Err(e) => match stale {
                Some(old) => {
                    tracing::warn!(error = %e, "routing registry refresh failed; serving stale entry");
                    Ok(old)
                }
                None => Err(e),
            },
        }
    }

    /// Re-reads a scope from the source; failure keeps the previous entry.
    pub async fn refresh(&self, org: i64) {
        let scope = scope_of(org);
        match self.source.load(scope).await {
            Ok(s) => {
                self.put(scope, s);
            }
            Err(e) => tracing::warn!(error = %e, "routing registry refresh failed; keeping entry"),
        }
    }

    /// Handles one observed key deletion; unrelated keys are ignored. A global assignment
    /// deletion refreshes every cached scope (global rows are part of each org's set).
    pub async fn on_invalidation(&self, key: &str) {
        let scopes: Vec<i64> = if let Some(rest) = key.strip_prefix(ASSIGN_KEY_PREFIX) {
            match rest.split(':').next() {
                Some("global") => self.cached_scope_ids(),
                Some(id) => id.parse::<i64>().into_iter().collect(),
                None => return,
            }
        } else if let Some(rest) = key.strip_prefix(POLICY_KEY_PREFIX) {
            rest.parse::<i64>().into_iter().collect()
        } else {
            return;
        };
        for s in scopes {
            // An uncached scope has nothing stale; it lazy-loads fresh on first use.
            if self
                .l1
                .read()
                .unwrap_or_else(|p| p.into_inner())
                .contains_key(&scope_of(s))
            {
                self.refresh(s).await;
            }
        }
    }

    /// Number of cached scopes.
    pub fn cached_scopes(&self) -> usize {
        self.l1.read().unwrap_or_else(|p| p.into_inner()).len()
    }

    /// Cached scope ids.
    fn cached_scope_ids(&self) -> Vec<i64> {
        self.l1
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .keys()
            .copied()
            .collect()
    }

    /// Replaces an L1 entry and returns the stored snapshot.
    fn put(&self, scope: i64, snap: OrgRouting) -> Arc<OrgRouting> {
        let snap = Arc::new(snap);
        self.l1.write().unwrap_or_else(|p| p.into_inner()).insert(
            scope,
            Entry {
                snap: Arc::clone(&snap),
                loaded: Instant::now(),
            },
        );
        snap
    }
}

/// Spawns a task feeding Valkey key-deletion events into the cache.
pub fn spawn_invalidation_listener(
    registry: Arc<RegistryCache>,
    mut inv: cache::Invalidations,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some(event) = inv.recv().await {
            registry.on_invalidation(&event.key).await;
        }
    })
}
