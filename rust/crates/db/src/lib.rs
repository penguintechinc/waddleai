//! Pooled SeaORM connection for the WaddleAI data plane.
//!
//! Rust never owns the schema: `entities/` is generated from the
//! Alembic-migrated database by `scripts/gen-entities.sh` and guarded by the
//! `entity_drift` test.

pub mod entities;

use std::time::Duration;

use config::Config;
use sea_orm::{ConnectOptions, Database, DatabaseConnection};

pub use sea_orm::DatabaseConnection as Connection;

/// Per-attempt connect/acquire bound so a black-holed host cannot stall startup.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Errors produced while establishing the database connection.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    /// All connection attempts failed; carries the last driver error.
    #[error("database connection failed after {attempts} attempt(s): {source}")]
    Connect {
        /// Number of attempts made.
        attempts: u32,
        /// Last underlying driver error.
        #[source]
        source: sea_orm::DbErr,
    },
    /// A pooling/retry environment variable was set but unparsable.
    #[error("invalid env var {var}: expected an unsigned integer")]
    InvalidOption {
        /// Variable name.
        var: &'static str,
    },
}

/// Pool size and retry policy (`DB_POOL_SIZE`, `DB_MAX_RETRIES`, `DB_RETRY_DELAY`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DbOptions {
    /// Maximum pooled connections.
    pub pool_size: u32,
    /// Total connection attempts (>= 1).
    pub max_retries: u32,
    /// Base delay between attempts; doubles each retry.
    pub retry_delay: Duration,
}

impl Default for DbOptions {
    fn default() -> Self {
        Self {
            pool_size: 10,
            max_retries: 5,
            retry_delay: Duration::from_secs(5),
        }
    }
}

fn env_u32(var: &'static str, default: u32) -> Result<u32, DbError> {
    match std::env::var(var).ok().filter(|v| !v.is_empty()) {
        None => Ok(default),
        Some(v) => v.parse().map_err(|_| DbError::InvalidOption { var }),
    }
}

impl DbOptions {
    /// Read the options from the environment, falling back to the defaults.
    pub fn from_env() -> Result<Self, DbError> {
        let d = Self::default();
        Ok(Self {
            pool_size: env_u32("DB_POOL_SIZE", d.pool_size)?.max(1),
            max_retries: env_u32("DB_MAX_RETRIES", d.max_retries)?.max(1),
            retry_delay: Duration::from_secs(u64::from(env_u32(
                "DB_RETRY_DELAY",
                d.retry_delay.as_secs() as u32,
            )?)),
        })
    }
}

/// Connect using `cfg.database_url` and the env-derived [`DbOptions`].
pub async fn connect(cfg: &Config) -> Result<DatabaseConnection, DbError> {
    connect_with(&cfg.database_url, &DbOptions::from_env()?).await
}

/// Connect to `url` with a bounded retry loop and exponential backoff,
/// verifying each new pool with a ping before returning it.
pub async fn connect_with(url: &str, opts: &DbOptions) -> Result<DatabaseConnection, DbError> {
    let attempts = opts.max_retries.max(1);
    let mut delay = opts.retry_delay;
    let mut attempt = 0;
    loop {
        attempt += 1;
        let mut co = ConnectOptions::new(url.to_owned());
        co.max_connections(opts.pool_size.max(1))
            .connect_timeout(CONNECT_TIMEOUT)
            .acquire_timeout(CONNECT_TIMEOUT)
            .sqlx_logging(false);
        let result = match Database::connect(co).await {
            Ok(conn) => conn.ping().await.map(|()| conn),
            Err(e) => Err(e),
        };
        match result {
            Ok(conn) => return Ok(conn),
            Err(source) if attempt >= attempts => {
                return Err(DbError::Connect { attempts, source })
            }
            Err(_) => {
                tracing::warn!(attempt, attempts, "database connect failed; retrying");
                tokio::time::sleep(delay).await;
                delay = delay.saturating_mul(2);
            }
        }
    }
}
