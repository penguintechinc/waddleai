//! Multiplexed async Valkey connection with a bounded connect and ping.

use std::time::Duration;

use config::Config;
use redis::aio::MultiplexedConnection;
use redis::AsyncCommands;

/// Bound on connect and per-ping so a black-holed host cannot stall startup.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Errors from the Valkey client.
#[derive(Debug, thiserror::Error)]
pub enum CacheError {
    /// The driver reported an error.
    #[error("valkey error: {0}")]
    Redis(#[from] redis::RedisError),
    /// An operation exceeded its time bound.
    #[error("valkey operation timed out")]
    Timeout,
}

/// Cloneable handle over one multiplexed Valkey connection.
#[derive(Clone)]
pub struct Cache {
    conn: MultiplexedConnection,
}

/// Connects to `Config.redis_url` and verifies the server answers `PING`.
pub async fn connect(cfg: &Config) -> Result<Cache, CacheError> {
    let client = redis::Client::open(cfg.redis_url.as_str())?;
    let conn = tokio::time::timeout(CONNECT_TIMEOUT, client.get_multiplexed_async_connection())
        .await
        .map_err(|_| CacheError::Timeout)??;
    let cache = Cache { conn };
    cache.ping().await?;
    Ok(cache)
}

impl Cache {
    /// Round-trips `PING`, erroring unless the server replies `PONG`.
    pub async fn ping(&self) -> Result<(), CacheError> {
        let mut c = self.conn.clone();
        let pong: String =
            tokio::time::timeout(CONNECT_TIMEOUT, redis::cmd("PING").query_async(&mut c))
                .await
                .map_err(|_| CacheError::Timeout)??;
        if pong == "PONG" {
            Ok(())
        } else {
            Err(CacheError::Redis(redis::RedisError::from((
                redis::ErrorKind::Server(redis::ServerErrorKind::ResponseError),
                "unexpected PING reply",
            ))))
        }
    }

    /// Reads a key's bytes, `None` when absent.
    pub async fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CacheError> {
        let mut c = self.conn.clone();
        Ok(c.get(key).await?)
    }

    /// Sets a key with a TTL in seconds (`SET key value EX ttl`).
    pub async fn set(&self, key: &str, value: &[u8], ttl_secs: u64) -> Result<(), CacheError> {
        let mut c = self.conn.clone();
        Ok(c.set_ex(key, value, ttl_secs).await?)
    }

    /// Deletes a key (the Python proxy's invalidation primitive).
    pub async fn del(&self, key: &str) -> Result<(), CacheError> {
        let mut c = self.conn.clone();
        let _: i64 = c.del(key).await?;
        Ok(())
    }
}
