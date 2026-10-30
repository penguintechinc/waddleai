//! `cache_configs` resolution at key > org > global precedence (port of `shared/cache/config.py`).
//!
//! Each scope is cached independently in Valkey under `waddleai:cache:cfg:{scope}:{ref}` with a
//! short TTL, including a negative entry (`null`) when no row exists, so a cold resolve does one
//! combined DB read and later resolves are Valkey-only. Fields merge per-field: a NULL column
//! falls through to the next-broader scope instead of resetting to the hard default.

use std::sync::Arc;
use std::time::Duration;

use redis::aio::MultiplexedConnection;
use redis::AsyncCommands;
use sea_orm::{ColumnTrait, Condition, DatabaseConnection, EntityTrait, QueryFilter};
use serde::{Deserialize, Serialize};

use crate::error::StoreError;

/// The effective cache configuration for an (org, virtual key) pair.
#[derive(Debug, Clone, PartialEq)]
pub struct ResolvedCacheConfig {
    /// Exact layer on.
    pub exact_enabled: bool,
    /// Semantic layer on (default OFF).
    pub semantic_enabled: bool,
    /// Minimum cosine similarity for a semantic hit.
    pub semantic_threshold: f64,
    /// Entry TTL in seconds.
    pub ttl_seconds: u64,
    /// Largest cacheable entry, KiB.
    pub max_entry_kb: u64,
    /// Anthropic upstream prompt-cache annotation on.
    pub anthropic_cache_control: bool,
}

impl Default for ResolvedCacheConfig {
    /// The hard defaults (`ResolvedCacheConfig()` in Python).
    fn default() -> Self {
        Self {
            exact_enabled: true,
            semantic_enabled: false,
            semantic_threshold: 0.95,
            ttl_seconds: 86_400,
            max_entry_kb: 256,
            anthropic_cache_control: true,
        }
    }
}

/// Resolves the effective config for a request.
#[async_trait::async_trait]
pub trait ConfigSource: Send + Sync {
    /// The merged config; an `Err` makes the caller skip caching for this request (visibly).
    async fn resolve(
        &self,
        org_id: i64,
        vkey_id: Option<i64>,
    ) -> Result<ResolvedCacheConfig, StoreError>;
}

/// One `cache_configs` row (also the Valkey JSON shape the Python resolver writes).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct CacheConfigRow {
    /// `global`, `org` or `key`.
    pub scope_type: String,
    /// Org id / virtual-key id as a string; absent for `global`.
    pub scope_ref: Option<String>,
    /// Exact layer toggle.
    pub exact_enabled: Option<bool>,
    /// Semantic layer toggle.
    pub semantic_enabled: Option<bool>,
    /// Semantic similarity threshold.
    pub semantic_threshold: Option<f64>,
    /// Entry TTL seconds.
    pub ttl_seconds: Option<i64>,
    /// Entry size bound, KiB.
    pub max_entry_kb: Option<i64>,
    /// Anthropic cache-control toggle.
    pub anthropic_cache_control: Option<bool>,
}

/// Reads the rows for every scope a resolution needs in one query.
#[async_trait::async_trait]
pub trait ConfigRowStore: Send + Sync {
    /// Rows for `global`, `org:{org_id}` and (when given) `key:{vkey_id}`.
    async fn fetch(
        &self,
        org_id: i64,
        vkey_id: Option<i64>,
    ) -> Result<Vec<CacheConfigRow>, StoreError>;
}

/// SeaORM [`ConfigRowStore`] over the generated `cache_configs` entity.
pub struct SeaConfigStore {
    db: DatabaseConnection,
}

impl SeaConfigStore {
    /// Binds a pooled connection.
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

#[async_trait::async_trait]
impl ConfigRowStore for SeaConfigStore {
    async fn fetch(
        &self,
        org_id: i64,
        vkey_id: Option<i64>,
    ) -> Result<Vec<CacheConfigRow>, StoreError> {
        use db::entities::cache_configs::{Column, Entity};
        let mut cond = Condition::any().add(Column::ScopeType.eq("global"));
        cond = cond.add(
            Condition::all()
                .add(Column::ScopeType.eq("org"))
                .add(Column::ScopeRef.eq(org_id.to_string())),
        );
        if let Some(k) = vkey_id {
            cond = cond.add(
                Condition::all()
                    .add(Column::ScopeType.eq("key"))
                    .add(Column::ScopeRef.eq(k.to_string())),
            );
        }
        let rows = Entity::find().filter(cond).all(&self.db).await?;
        Ok(rows
            .into_iter()
            .map(|r| CacheConfigRow {
                scope_type: r.scope_type,
                scope_ref: r.scope_ref,
                exact_enabled: Some(r.exact_enabled),
                semantic_enabled: Some(r.semantic_enabled),
                semantic_threshold: Some(r.semantic_threshold),
                ttl_seconds: Some(i64::from(r.ttl_seconds)),
                max_entry_kb: Some(i64::from(r.max_entry_kb)),
                anthropic_cache_control: Some(r.anthropic_cache_control),
            })
            .collect())
    }
}

/// Resolves `cache_configs` through a Valkey hot path (when connected) over a row store.
pub struct CacheConfigResolver {
    rows: Arc<dyn ConfigRowStore>,
    valkey: Option<MultiplexedConnection>,
    scope_ttl: Duration,
}

impl CacheConfigResolver {
    /// Resolver over `rows`; without `valkey` every resolve reads the store.
    pub fn new(rows: Arc<dyn ConfigRowStore>, valkey: Option<MultiplexedConnection>) -> Self {
        Self {
            rows,
            valkey,
            scope_ttl: Duration::from_secs(30),
        }
    }

    /// Overrides the per-scope Valkey TTL (default 30s).
    pub fn with_scope_ttl(mut self, ttl: Duration) -> Self {
        self.scope_ttl = ttl;
        self
    }

    /// Busts the Valkey-cached entry for one scope; the next resolve re-reads the DB.
    pub async fn invalidate(
        &self,
        scope_type: &str,
        scope_ref: Option<&str>,
    ) -> Result<(), StoreError> {
        if let Some(conn) = &self.valkey {
            let mut c = conn.clone();
            let _: i64 = c
                .del(cache::keys::scope_cache(scope_type, scope_ref))
                .await?;
        }
        Ok(())
    }

    /// Field-level merge, broadest scope first; narrower scopes override per field.
    pub fn merge(rows: &[Option<CacheConfigRow>]) -> ResolvedCacheConfig {
        let mut m = ResolvedCacheConfig::default();
        for row in rows.iter().flatten() {
            if let Some(v) = row.exact_enabled {
                m.exact_enabled = v;
            }
            if let Some(v) = row.semantic_enabled {
                m.semantic_enabled = v;
            }
            if let Some(v) = row.semantic_threshold {
                m.semantic_threshold = v;
            }
            if let Some(v) = row.ttl_seconds.and_then(|v| u64::try_from(v).ok()) {
                m.ttl_seconds = v;
            }
            if let Some(v) = row.max_entry_kb.and_then(|v| u64::try_from(v).ok()) {
                m.max_entry_kb = v;
            }
            if let Some(v) = row.anthropic_cache_control {
                m.anthropic_cache_control = v;
            }
        }
        m
    }
}

#[async_trait::async_trait]
impl ConfigSource for CacheConfigResolver {
    async fn resolve(
        &self,
        org_id: i64,
        vkey_id: Option<i64>,
    ) -> Result<ResolvedCacheConfig, StoreError> {
        let org_ref = org_id.to_string();
        let key_ref = vkey_id.map(|k| k.to_string());
        let mut scopes: Vec<(&str, Option<&str>)> = vec![("global", None), ("org", Some(&org_ref))];
        if let Some(k) = key_ref.as_deref() {
            scopes.push(("key", Some(k)));
        }

        let mut found: Vec<Option<Option<CacheConfigRow>>> = vec![None; scopes.len()];
        if let Some(conn) = &self.valkey {
            let mut c = conn.clone();
            for (i, (t, r)) in scopes.iter().enumerate() {
                let raw: Result<Option<Vec<u8>>, _> = c.get(cache::keys::scope_cache(t, *r)).await;
                match raw {
                    Ok(Some(bytes)) => {
                        match serde_json::from_slice::<Option<CacheConfigRow>>(&bytes) {
                            Ok(row) => found[i] = Some(row),
                            Err(e) => {
                                tracing::warn!(error = %e, scope = *t, "corrupt cache_configs entry; re-reading")
                            }
                        }
                    }
                    Ok(None) => {}
                    Err(e) => {
                        tracing::warn!(error = %e, "cache_configs valkey read failed; using the store")
                    }
                }
            }
        }

        if found.iter().any(Option::is_none) {
            let fetched = self.rows.fetch(org_id, vkey_id).await?;
            for (i, (t, r)) in scopes.iter().enumerate() {
                if found[i].is_some() {
                    continue;
                }
                let row = fetched
                    .iter()
                    .find(|x| x.scope_type == *t && x.scope_ref.as_deref() == *r)
                    .cloned();
                if let Some(conn) = &self.valkey {
                    let mut c = conn.clone();
                    let body = serde_json::to_vec(&row).map_err(|e| StoreError(e.to_string()))?;
                    let set: Result<(), _> = c
                        .set_ex(
                            cache::keys::scope_cache(t, *r),
                            body,
                            self.scope_ttl.as_secs().max(1),
                        )
                        .await;
                    if let Err(e) = set {
                        tracing::warn!(error = %e, "cache_configs valkey write failed");
                    }
                }
                found[i] = Some(row);
            }
        }
        let rows: Vec<Option<CacheConfigRow>> = found.into_iter().map(Option::flatten).collect();
        Ok(Self::merge(&rows))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    fn row(t: &str, r: Option<&str>) -> CacheConfigRow {
        CacheConfigRow {
            scope_type: t.into(),
            scope_ref: r.map(Into::into),
            ..CacheConfigRow::default()
        }
    }

    #[test]
    fn defaults_match_python() {
        let d = ResolvedCacheConfig::default();
        assert!(d.exact_enabled && !d.semantic_enabled && d.anthropic_cache_control);
        assert_eq!(
            (d.semantic_threshold, d.ttl_seconds, d.max_entry_kb),
            (0.95, 86_400, 256)
        );
    }

    #[test]
    fn merge_is_per_field_narrow_over_broad() {
        let mut global = row("global", None);
        global.ttl_seconds = Some(100);
        global.semantic_enabled = Some(true);
        let mut org = row("org", Some("5"));
        org.ttl_seconds = Some(200);
        let mut key = row("key", Some("9"));
        key.semantic_enabled = Some(false); // overrides only this field
        key.max_entry_kb = Some(-5); // invalid negative ignored
        let m = CacheConfigResolver::merge(&[Some(global), Some(org), None, Some(key)]);
        assert_eq!(m.ttl_seconds, 200, "org beats global, key leaves it alone");
        assert!(!m.semantic_enabled, "key beats global");
        assert_eq!(m.max_entry_kb, 256, "negative falls through to default");
        assert!(m.exact_enabled);
    }

    #[test]
    fn python_scope_json_deserializes() {
        let py = br#"{"scope_type":"org","scope_ref":"5","exact_enabled":true,"semantic_enabled":false,"semantic_threshold":0.9,"ttl_seconds":60,"max_entry_kb":8,"anthropic_cache_control":null}"#;
        let r: Option<CacheConfigRow> = serde_json::from_slice(py).unwrap();
        assert_eq!(r.as_ref().unwrap().ttl_seconds, Some(60));
        assert_eq!(r.unwrap().anthropic_cache_control, None);
        assert_eq!(
            serde_json::from_slice::<Option<CacheConfigRow>>(b"null").unwrap(),
            None
        );
    }

    struct Rows(Vec<CacheConfigRow>, AtomicUsize, bool);

    #[async_trait::async_trait]
    impl ConfigRowStore for Rows {
        async fn fetch(&self, _o: i64, _v: Option<i64>) -> Result<Vec<CacheConfigRow>, StoreError> {
            self.1.fetch_add(1, Ordering::SeqCst);
            if self.2 {
                return Err(StoreError("db down".into()));
            }
            Ok(self.0.clone())
        }
    }

    #[tokio::test]
    async fn resolve_without_valkey_reads_store_each_time() {
        let mut g = row("global", None);
        g.ttl_seconds = Some(7);
        let mut o = row("org", Some("5"));
        o.semantic_enabled = Some(true);
        let store = Arc::new(Rows(
            vec![g, o, row("org", Some("6"))],
            AtomicUsize::new(0),
            false,
        ));
        let r = CacheConfigResolver::new(store.clone(), None);
        let c = r.resolve(5, None).await.unwrap();
        assert!(c.semantic_enabled);
        assert_eq!(c.ttl_seconds, 7);
        r.resolve(5, Some(1)).await.unwrap();
        assert_eq!(store.1.load(Ordering::SeqCst), 2);
        r.invalidate("org", Some("5")).await.unwrap();
    }

    #[tokio::test]
    async fn store_error_propagates_so_the_caller_can_degrade_visibly() {
        let store = Arc::new(Rows(vec![], AtomicUsize::new(0), true));
        let r = CacheConfigResolver::new(store, None);
        assert!(r.resolve(5, None).await.is_err());
    }
}
