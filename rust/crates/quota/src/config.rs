//! Quota config: the Valkey read-through cache shared with Python (`shared/quota/config.py`).
//!
//! The proxy never queries config tables per request: it reads a JSON blob from Valkey
//! (`wa:quota:cfg:*`, the same keys Python writes) and loads from the DB through a
//! [`ConfigLoader`] on a miss. Management deletes the blob on every edit; the TTL bounds
//! staleness. A DB failure fails OPEN as "unconfigured" (logged at error level).

use std::collections::HashMap;
use std::sync::Arc;

use cache::keys::quota as keys;
use cache::Cache;
use chrono::NaiveDate;
use serde_json::{json, Value};

use crate::types::{OrgQuotaConfig, OverrideRule, TeamQuotaConfig, UserReservation};

/// The quota config could not be loaded.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("quota config unavailable: {0}")]
pub struct ConfigError(pub String);

/// Source of org/team quota config consulted by the engine on every reserve.
#[async_trait::async_trait]
pub trait ConfigStore: Send + Sync {
    /// The org's config, or `None` when it has none.
    async fn get_org(&self, org_id: i64) -> Result<Option<OrgQuotaConfig>, ConfigError>;
    /// The team's config, or `None` when it has none.
    async fn get_team(&self, team_id: i64) -> Result<Option<TeamQuotaConfig>, ConfigError>;
}

/// Database loader behind the Valkey cache (a miss path; never per request).
#[async_trait::async_trait]
pub trait ConfigLoader: Send + Sync {
    /// Loads the org's pool switch, anchor and overrides.
    async fn load_org(&self, org_id: i64) -> Result<Option<OrgQuotaConfig>, ConfigError>;
    /// Loads the team's pool/buffer and per-user reservations.
    async fn load_team(&self, team_id: i64) -> Result<Option<TeamQuotaConfig>, ConfigError>;
}

fn opt_i64(v: &Value) -> Option<i64> {
    v.as_i64()
}

fn opt_str(v: &Value) -> Option<String> {
    v.as_str().map(str::to_owned)
}

/// Serializes an org config exactly as Python `org_config_to_json` (same field names).
pub fn org_config_to_json(cfg: &OrgQuotaConfig) -> String {
    json!({
        "org_id": cfg.org_id,
        "enabled": cfg.enabled,
        "anchor": cfg.cycle_anchor_date.map(|d| d.format("%Y-%m-%d").to_string()),
        "pool": cfg.pool_credits,
        "overrides": cfg.overrides.iter().map(|o| json!({
            "id": o.id, "cap": o.cap_credits, "user_id": o.user_id,
            "model": o.model, "provider": o.provider,
        })).collect::<Vec<_>>(),
    })
    .to_string()
}

/// Inverse of [`org_config_to_json`]; the literal `null` decodes to `None`.
pub fn org_config_from_json(raw: &[u8]) -> Result<Option<OrgQuotaConfig>, ConfigError> {
    let bad = |m: &str| ConfigError(format!("malformed org config: {m}"));
    let data: Value = serde_json::from_slice(raw).map_err(|e| bad(&e.to_string()))?;
    if data.is_null() {
        return Ok(None);
    }
    let anchor = match data.get("anchor") {
        Some(Value::String(s)) => {
            Some(NaiveDate::parse_from_str(s, "%Y-%m-%d").map_err(|e| bad(&e.to_string()))?)
        }
        _ => None,
    };
    let overrides = data
        .get("overrides")
        .and_then(Value::as_array)
        .ok_or_else(|| bad("overrides"))?
        .iter()
        .map(|o| {
            Ok(OverrideRule {
                id: o.get("id").and_then(opt_i64).ok_or_else(|| bad("id"))?,
                cap_credits: o.get("cap").and_then(opt_i64).ok_or_else(|| bad("cap"))?,
                user_id: o.get("user_id").and_then(opt_i64),
                model: o.get("model").and_then(opt_str),
                provider: o.get("provider").and_then(opt_str),
            })
        })
        .collect::<Result<Vec<_>, ConfigError>>()?;
    Ok(Some(OrgQuotaConfig {
        org_id: data
            .get("org_id")
            .and_then(opt_i64)
            .ok_or_else(|| bad("org_id"))?,
        enabled: data
            .get("enabled")
            .and_then(Value::as_bool)
            .ok_or_else(|| bad("enabled"))?,
        cycle_anchor_date: anchor,
        pool_credits: data
            .get("pool")
            .and_then(opt_i64)
            .ok_or_else(|| bad("pool"))?,
        overrides,
    }))
}

/// Serializes a team config exactly as Python `team_config_to_json`.
pub fn team_config_to_json(cfg: &TeamQuotaConfig) -> String {
    let mut rows: Vec<&UserReservation> = cfg.reservations.values().collect();
    rows.sort_by_key(|r| r.user_id);
    json!({
        "team_id": cfg.team_id,
        "org_id": cfg.org_id,
        "pool": cfg.pool_credits,
        "buffer": cfg.shared_buffer_credits,
        "reservations": rows.iter().map(|r| json!({
            "user_id": r.user_id, "guaranteed": r.guaranteed_credits, "daily": r.daily_subcap,
        })).collect::<Vec<_>>(),
    })
    .to_string()
}

/// Inverse of [`team_config_to_json`]; the literal `null` decodes to `None`.
pub fn team_config_from_json(raw: &[u8]) -> Result<Option<TeamQuotaConfig>, ConfigError> {
    let bad = |m: &str| ConfigError(format!("malformed team config: {m}"));
    let data: Value = serde_json::from_slice(raw).map_err(|e| bad(&e.to_string()))?;
    if data.is_null() {
        return Ok(None);
    }
    let int = |k: &str| data.get(k).and_then(opt_i64).ok_or_else(|| bad(k));
    let mut reservations = HashMap::new();
    for r in data
        .get("reservations")
        .and_then(Value::as_array)
        .ok_or_else(|| bad("reservations"))?
    {
        let user_id = r
            .get("user_id")
            .and_then(opt_i64)
            .ok_or_else(|| bad("user_id"))?;
        reservations.insert(
            user_id,
            UserReservation {
                user_id,
                guaranteed_credits: r
                    .get("guaranteed")
                    .and_then(opt_i64)
                    .ok_or_else(|| bad("guaranteed"))?,
                daily_subcap: r.get("daily").and_then(opt_i64),
            },
        );
    }
    Ok(Some(TeamQuotaConfig {
        team_id: int("team_id")?,
        org_id: int("org_id")?,
        pool_credits: int("pool")?,
        shared_buffer_credits: int("buffer")?,
        reservations,
    }))
}

/// Valkey read-through cache of org/team quota config.
pub struct ValkeyConfigStore {
    cache: Cache,
    loader: Arc<dyn ConfigLoader>,
    ttl_s: u64,
    negative_ttl_s: u64,
}

impl ValkeyConfigStore {
    /// Builds the store with the Python defaults (300s positive, 30s negative TTL).
    pub fn new(cache: Cache, loader: Arc<dyn ConfigLoader>) -> Self {
        Self {
            cache,
            loader,
            ttl_s: 300,
            negative_ttl_s: 30,
        }
    }

    /// Best-effort cache write: a failure to cache never fails the request.
    async fn put(&self, key: &str, body: &str, is_none: bool) {
        let ttl = if is_none {
            self.negative_ttl_s
        } else {
            self.ttl_s
        };
        if let Err(err) = self.cache.set(key, body.as_bytes(), ttl).await {
            tracing::warn!(error = %err, "quota config cache write failed");
        }
    }

    /// Cache hit decodes; a miss, outage or malformed entry falls back to the loader. A loader
    /// failure fails open as "unconfigured" (`Ok(None)`), logged at error level.
    async fn read_through<T, D, L>(
        &self,
        key: String,
        decode: D,
        load: L,
        encode: impl Fn(&T) -> String,
    ) -> Result<Option<T>, ConfigError>
    where
        D: Fn(&[u8]) -> Result<Option<T>, ConfigError>,
        L: std::future::Future<Output = Result<Option<T>, ConfigError>>,
    {
        match self.cache.get(&key).await {
            Ok(Some(raw)) => match decode(&raw) {
                Ok(v) => return Ok(v),
                Err(err) => {
                    tracing::warn!(error = %err, "quota config cache entry malformed; reloading")
                }
            },
            Ok(None) => {}
            Err(err) => tracing::warn!(error = %err, "quota config cache read failed; using DB"),
        }
        let value = match load.await {
            Ok(v) => v,
            Err(err) => {
                tracing::error!(error = %err, "quota config load failed; failing open as unconfigured");
                return Ok(None);
            }
        };
        let body = value.as_ref().map_or_else(|| "null".to_owned(), &encode);
        self.put(&key, &body, value.is_none()).await;
        Ok(value)
    }
}

#[async_trait::async_trait]
impl ConfigStore for ValkeyConfigStore {
    async fn get_org(&self, org_id: i64) -> Result<Option<OrgQuotaConfig>, ConfigError> {
        self.read_through(
            keys::org_config(org_id),
            org_config_from_json,
            self.loader.load_org(org_id),
            org_config_to_json,
        )
        .await
    }

    async fn get_team(&self, team_id: i64) -> Result<Option<TeamQuotaConfig>, ConfigError> {
        self.read_through(
            keys::team_config(team_id),
            team_config_from_json,
            self.loader.load_team(team_id),
            team_config_to_json,
        )
        .await
    }
}
