//! Cache-invalidation subscriber.
//!
//! The Python side invalidates by `DEL`-ing keys (no pub/sub channel exists),
//! so this listens to Valkey keyspace `del` events and yields the deleted key
//! names matching the requested prefixes.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use config::Config;
use futures_util::StreamExt;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::client::CacheError;

/// Pattern for key-event `del` notifications on every database.
const DEL_PATTERN: &str = "__keyevent@*__:del";
/// Bounded buffer; a slow consumer drops events rather than growing memory.
const BUFFER: usize = 1024;

/// One observed invalidation: the key that was deleted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invalidation {
    /// The deleted key, byte-for-byte as stored.
    pub key: String,
}

/// Stream of invalidations; the listener task stops when this is dropped.
pub struct Invalidations {
    rx: mpsc::Receiver<Invalidation>,
    task: JoinHandle<()>,
    dropped: Arc<AtomicU64>,
}

impl Invalidations {
    /// Number of events dropped because the consumer let the buffer fill.
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    /// Awaits the next invalidation, `None` if the listener ended.
    pub async fn recv(&mut self) -> Option<Invalidation> {
        self.rx.recv().await
    }
}

impl Drop for Invalidations {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Subscribes to key deletions whose name starts with any of `prefixes`
/// (empty = all). Requires keyspace `del` events (`Eg`): sets them if possible,
/// then verifies; returns [`CacheError::NotificationsDisabled`] if not enabled.
pub async fn subscribe_invalidations(
    cfg: &Config,
    prefixes: Vec<String>,
) -> Result<Invalidations, CacheError> {
    let client = redis::Client::open(cfg.redis_url.as_str())?;
    enable_notifications(&client).await?;
    let mut pubsub = client.get_async_pubsub().await?;
    pubsub.psubscribe(DEL_PATTERN).await?;
    let (tx, rx) = mpsc::channel(BUFFER);
    let dropped = Arc::new(AtomicU64::new(0));
    let task_dropped = Arc::clone(&dropped);
    let task = tokio::spawn(async move {
        let mut stream = pubsub.into_on_message();
        while let Some(msg) = stream.next().await {
            let Ok(key) = msg.get_payload::<String>() else {
                continue;
            };
            if !prefixes.is_empty() && !prefixes.iter().any(|p| key.starts_with(p.as_str())) {
                continue;
            }
            match tx.try_send(Invalidation { key }) {
                Ok(()) => {}
                Err(mpsc::error::TrySendError::Closed(_)) => break,
                Err(mpsc::error::TrySendError::Full(_)) => {
                    let n = task_dropped.fetch_add(1, Ordering::Relaxed) + 1;
                    // Rate-limited: first drop, then every 1000th.
                    if n == 1 || n.is_multiple_of(1000) {
                        tracing::error!(
                            dropped_total = n,
                            "invalidation buffer full; cache may be stale"
                        );
                    }
                }
            }
        }
    });
    Ok(Invalidations { rx, task, dropped })
}

/// True when `flags` enables keyevent (`E`) and generic (`g`, or `A` superset) events.
fn flags_ok(flags: &str) -> bool {
    flags.contains('E') && (flags.contains('g') || flags.contains('A'))
}

/// Reads `notify-keyspace-events` (empty when unset).
async fn read_flags(c: &mut redis::aio::MultiplexedConnection) -> Result<String, CacheError> {
    let cur: Vec<String> = redis::cmd("CONFIG")
        .arg("GET")
        .arg("notify-keyspace-events")
        .query_async(c)
        .await?;
    Ok(cur.get(1).cloned().unwrap_or_default())
}

/// Ensures `E` and `g` are enabled, then re-reads the setting to confirm it.
/// A blocked `CONFIG` (managed Valkey/ACL) with the flags absent is a hard
/// error: a subscriber that can never fire would silently serve stale cache.
async fn enable_notifications(client: &redis::Client) -> Result<(), CacheError> {
    let mut c = client.get_multiplexed_async_connection().await?;
    let mut flags = read_flags(&mut c).await.unwrap_or_default();
    if !flags_ok(&flags) {
        for f in ['E', 'g'] {
            if !flags.contains(f) {
                flags.push(f);
            }
        }
        if let Err(e) = redis::cmd("CONFIG")
            .arg("SET")
            .arg("notify-keyspace-events")
            .arg(&flags)
            .query_async::<()>(&mut c)
            .await
        {
            tracing::warn!(error = %e, "CONFIG SET notify-keyspace-events failed");
        }
    }
    let confirmed = read_flags(&mut c).await.unwrap_or_default();
    if flags_ok(&confirmed) {
        Ok(())
    } else {
        Err(CacheError::NotificationsDisabled)
    }
}

#[cfg(test)]
mod tests {
    use super::flags_ok;

    #[test]
    fn flag_confirmation() {
        assert!(flags_ok("Eg"));
        assert!(flags_ok("KEA"));
        assert!(!flags_ok(""));
        assert!(!flags_ok("g"));
        assert!(!flags_ok("E"));
        assert!(!flags_ok("Kg"));
    }
}
