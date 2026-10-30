//! Tier-2 org rules store: in-memory L1, Valkey L2, DB source, invalidation-refreshed.
//!
//! The store is seeded at startup and lazy-loads on a miss, so a cold or never-invalidated
//! cache can never leave an org with an empty (fail-closed or fail-open) rule set.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

/// Entries older than this are re-read from the source (invalidation is the fast path).
const L1_MAX_AGE: Duration = Duration::from_secs(300);
/// Valkey L2 TTL in seconds.
const L2_TTL_SECS: u64 = 300;

/// A rules-store failure.
#[derive(Debug, thiserror::Error)]
pub enum RulesError {
    /// The backing source (DB) failed and nothing is cached for the org.
    #[error("rules source unavailable: {0}")]
    Source(String),
}

/// One custom rule as stored in `content_filter_rules` (and in the Valkey payload).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FilterRule {
    /// Row id.
    pub id: i64,
    /// Rule name; becomes the redaction label.
    pub name: String,
    /// `custom_string` or `custom_regex` (other values are ignored, as in Python).
    pub rule_type: String,
    /// `input`, `output` or `both`.
    pub target: String,
    /// Substring or regex source.
    pub pattern: String,
    /// `block`, `redact` or `log`.
    pub action: String,
}

/// Raw per-org configuration: rules plus the builtin patterns the org disabled.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RawOrgConfig {
    /// Enabled rules visible to the org (its own plus global), in id order.
    pub rules: Vec<FilterRule>,
    /// Builtin pattern names disabled for the org (global union org).
    pub disabled_builtins: Vec<String>,
}

/// How a compiled rule matches.
pub(crate) enum Matcher {
    /// Case-insensitive literal, matched by byte span on the original text.
    Substring(regex::Regex),
    /// A literal that failed to compile: every scan counts as an aborted rule (fail loud).
    Broken,
    /// Case-insensitive, dot-matches-newline regex.
    Pattern(crate::content_filter::Pattern),
}

/// A rule with its matcher compiled once at load time.
pub(crate) struct CompiledRule {
    pub(crate) rule: FilterRule,
    pub(crate) matcher: Matcher,
}

/// Compiled, immutable org configuration shared across requests.
pub struct OrgConfig {
    pub(crate) rules: Vec<CompiledRule>,
    pub(crate) disabled_builtins: HashSet<String>,
}

impl OrgConfig {
    /// Compiles a raw config; invalid or empty non-block patterns are skipped with a warning (rule
    /// id only, Python parity). A BLOCK rule that cannot compile is kept as `Broken` and fails closed.
    pub fn compile(raw: RawOrgConfig) -> Self {
        let mut rules = Vec::with_capacity(raw.rules.len());
        for rule in raw.rules {
            let is_block = rule.action == "block";
            let matcher = match rule.rule_type.as_str() {
                "custom_string" if !rule.pattern.is_empty() => {
                    match regex::Regex::new(&format!("(?i){}", regex::escape(&rule.pattern))) {
                        Ok(re) => Matcher::Substring(re),
                        Err(_) => {
                            tracing::error!(rule_id = rule.id, "literal rule failed to compile");
                            Matcher::Broken
                        }
                    }
                }
                "custom_regex" if !rule.pattern.is_empty() => {
                    match crate::content_filter::build_regex(&rule.pattern) {
                        Ok(re) => Matcher::Pattern(re),
                        Err(_) if is_block => {
                            tracing::error!(
                                rule_id = rule.id,
                                "uncompilable BLOCK regex; rule fails closed at use"
                            );
                            Matcher::Broken
                        }
                        Err(_) => {
                            tracing::warn!(
                                rule_id = rule.id,
                                "invalid regex in content-filter rule; skipped"
                            );
                            continue;
                        }
                    }
                }
                "custom_string" | "custom_regex" if is_block => {
                    tracing::error!(
                        rule_id = rule.id,
                        "empty BLOCK pattern; rule fails closed at use"
                    );
                    Matcher::Broken
                }
                "custom_string" | "custom_regex" => {
                    tracing::warn!(
                        rule_id = rule.id,
                        "empty pattern in content-filter rule; skipped"
                    );
                    continue;
                }
                _ => continue,
            };
            rules.push(CompiledRule { rule, matcher });
        }
        Self {
            rules,
            disabled_builtins: raw.disabled_builtins.into_iter().collect(),
        }
    }

    /// Number of active compiled rules.
    pub fn rule_count(&self) -> usize {
        self.rules.len()
    }
}

/// Authoritative source of org rules (the database in production).
#[async_trait]
pub trait RulesSource: Send + Sync {
    /// Loads enabled rules for `org` (own plus global; global only for `None`) and its toggles.
    async fn load(&self, org: Option<i64>) -> Result<RawOrgConfig, RulesError>;
}

/// Minimal byte cache used as L2 (Valkey in production, a map in tests).
#[async_trait]
pub trait KvCache: Send + Sync {
    /// Reads a key; errors are reported as `None` by callers.
    async fn get(&self, key: &str) -> Option<Vec<u8>>;
    /// Stores a key with a TTL.
    async fn set(&self, key: &str, value: &[u8], ttl_secs: u64);
    /// Deletes a key.
    async fn del(&self, key: &str);
}

#[async_trait]
impl KvCache for cache::Cache {
    async fn get(&self, key: &str) -> Option<Vec<u8>> {
        match cache::Cache::get(self, key).await {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(error = %e, "valkey get failed; falling through to source");
                None
            }
        }
    }
    async fn set(&self, key: &str, value: &[u8], ttl_secs: u64) {
        if let Err(e) = cache::Cache::set(self, key, value, ttl_secs).await {
            tracing::warn!(error = %e, "valkey set failed");
        }
    }
    async fn del(&self, key: &str) {
        if let Err(e) = cache::Cache::del(self, key).await {
            tracing::warn!(error = %e, "valkey del failed");
        }
    }
}

/// SeaORM-backed source reading `content_filter_rules`.
///
/// Per-org builtin toggles have no table after migration 011 dropped `content_filter_config`,
/// so `disabled_builtins` is empty here until a new toggle store exists.
pub struct DbRulesSource {
    db: db::Connection,
}

impl DbRulesSource {
    /// Source over a pooled connection.
    pub fn new(db: db::Connection) -> Self {
        Self { db }
    }
}

#[async_trait]
impl RulesSource for DbRulesSource {
    async fn load(&self, org: Option<i64>) -> Result<RawOrgConfig, RulesError> {
        use db::entities::content_filter_rules::{Column, Entity};
        use sea_orm::{ColumnTrait, Condition, EntityTrait, QueryFilter, QueryOrder};

        let scope = match org {
            Some(o) => {
                let o = i32::try_from(o)
                    .map_err(|_| RulesError::Source("org id out of range".into()))?;
                Condition::any()
                    .add(Column::OrganizationId.eq(o))
                    .add(Column::OrganizationId.is_null())
            }
            None => Condition::all().add(Column::OrganizationId.is_null()),
        };
        let rows = Entity::find()
            .filter(Condition::all().add(Column::Enabled.eq(true)).add(scope))
            .order_by_asc(Column::Id)
            .all(&self.db)
            .await
            .map_err(|e| RulesError::Source(e.to_string()))?;
        Ok(RawOrgConfig {
            rules: rows
                .into_iter()
                .map(|r| FilterRule {
                    id: i64::from(r.id),
                    name: r.name,
                    rule_type: r.rule_type,
                    target: r.target,
                    pattern: r.pattern,
                    action: r.action,
                })
                .collect(),
            disabled_builtins: Vec::new(),
        })
    }
}

/// One L1 entry with its load time.
struct Entry {
    cfg: Arc<OrgConfig>,
    loaded: Instant,
}

/// Outcome of [`RulesStore::seed`].
#[derive(Debug, Default, PartialEq, Eq)]
pub struct SeedReport {
    /// Scopes loaded into L1.
    pub loaded: usize,
    /// Scopes whose load failed (they lazy-load on first use).
    pub failed: usize,
}

/// Normalizes an org id: `0`/`None` both mean the global scope (Python treats falsy org as global).
pub(crate) fn scope_of(org: Option<i64>) -> Option<i64> {
    org.filter(|o| *o != 0)
}

/// Rules cache. Reads never return an empty set because the cache is cold.
pub struct RulesStore {
    l1: RwLock<HashMap<Option<i64>, Entry>>,
    source: Arc<dyn RulesSource>,
    l2: Option<Arc<dyn KvCache>>,
}

impl RulesStore {
    /// Store over `source`, with an optional shared L2.
    pub fn new(source: Arc<dyn RulesSource>, l2: Option<Arc<dyn KvCache>>) -> Self {
        Self {
            l1: RwLock::new(HashMap::new()),
            source,
            l2,
        }
    }

    /// Startup seed: loads the global scope plus `orgs` into L1; failures lazy-load later.
    pub async fn seed(&self, orgs: &[i64]) -> SeedReport {
        let mut report = SeedReport::default();
        let scopes = std::iter::once(None).chain(orgs.iter().map(|o| scope_of(Some(*o))));
        for scope in scopes {
            match self.fetch(scope, true).await {
                Ok(cfg) => {
                    self.put(scope, cfg);
                    report.loaded += 1;
                }
                Err(e) => {
                    tracing::error!(error = %e, "content-filter rules seed failed; will lazy-load");
                    report.failed += 1;
                }
            }
        }
        report
    }

    /// Returns the org's config: L1 hit, else L2/source lazy-load. A stale entry whose refresh
    /// fails is served as-is; only a cold miss with a dead source is an error.
    pub async fn get(&self, org: Option<i64>) -> Result<Arc<OrgConfig>, RulesError> {
        let scope = scope_of(org);
        let stale = {
            let guard = self.l1.read().unwrap_or_else(|p| p.into_inner());
            match guard.get(&scope) {
                Some(e) if e.loaded.elapsed() < L1_MAX_AGE => return Ok(Arc::clone(&e.cfg)),
                Some(e) => Some(Arc::clone(&e.cfg)),
                None => None,
            }
        };
        match self.fetch(scope, stale.is_none()).await {
            Ok(cfg) => {
                self.put(scope, Arc::clone(&cfg));
                Ok(cfg)
            }
            Err(e) => match stale {
                Some(old) => {
                    tracing::warn!(error = %e, "rules refresh failed; serving stale entry");
                    Ok(old)
                }
                None => Err(e),
            },
        }
    }

    /// Re-reads `scope` straight from the source (authoritative; L2 is not consulted) and
    /// overwrites L1 and L2 with the result; failure keeps the previous entry. It never `DEL`s
    /// the L2 key: refresh is triggered by that key's deletion event, so a DEL would re-trigger it.
    pub async fn refresh(&self, org: Option<i64>) {
        let scope = scope_of(org);
        match self.fetch(scope, false).await {
            Ok(cfg) => self.put(scope, cfg),
            Err(e) => tracing::warn!(error = %e, "rules refresh failed; keeping previous entry"),
        }
    }

    /// Handles one observed key deletion; non-rules keys are ignored. A global-scope deletion
    /// refreshes every cached scope because global rules are part of each org's set.
    pub async fn on_invalidation(&self, key: &str) {
        let Some(tail) = key.strip_prefix(cache::keys::CONTENT_FILTER_RULES_PREFIX) else {
            return;
        };
        if tail == "global" {
            let scopes: Vec<Option<i64>> = {
                let guard = self.l1.read().unwrap_or_else(|p| p.into_inner());
                guard.keys().copied().collect()
            };
            self.refresh(None).await;
            for s in scopes.into_iter().filter(Option::is_some) {
                self.refresh(s).await;
            }
        } else if let Ok(id) = tail.parse::<i64>() {
            // An uncached scope has nothing stale here; it lazy-loads fresh on first use.
            let cached = self
                .l1
                .read()
                .unwrap_or_else(|p| p.into_inner())
                .contains_key(&Some(id));
            if cached {
                self.refresh(Some(id)).await;
            }
        }
    }

    /// Number of scopes currently cached in L1.
    pub fn cached_scopes(&self) -> usize {
        self.l1.read().unwrap_or_else(|p| p.into_inner()).len()
    }

    /// Replaces an L1 entry.
    fn put(&self, scope: Option<i64>, cfg: Arc<OrgConfig>) {
        self.l1.write().unwrap_or_else(|p| p.into_inner()).insert(
            scope,
            Entry {
                cfg,
                loaded: Instant::now(),
            },
        );
    }

    /// Loads a scope via L2 (when `use_l2`) then the source, writing L2 back on a source read.
    async fn fetch(&self, scope: Option<i64>, use_l2: bool) -> Result<Arc<OrgConfig>, RulesError> {
        let key = cache::keys::content_filter_rules(scope);
        if use_l2 {
            if let Some(l2) = &self.l2 {
                if let Some(bytes) = l2.get(&key).await {
                    match serde_json::from_slice::<RawOrgConfig>(&bytes) {
                        Ok(raw) => return Ok(Arc::new(OrgConfig::compile(raw))),
                        Err(_) => {
                            tracing::warn!("undecodable rules payload in L2; reloading from source")
                        }
                    }
                }
            }
        }
        let raw = self.source.load(scope).await?;
        if let Some(l2) = &self.l2 {
            if let Ok(bytes) = serde_json::to_vec(&raw) {
                l2.set(&key, &bytes, L2_TTL_SECS).await;
            }
        }
        Ok(Arc::new(OrgConfig::compile(raw)))
    }
}

/// Spawns a task feeding Valkey key-deletion events into the store.
pub fn spawn_invalidation_listener(
    store: Arc<RulesStore>,
    mut inv: cache::Invalidations,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some(event) = inv.recv().await {
            store.on_invalidation(&event.key).await;
        }
    })
}
