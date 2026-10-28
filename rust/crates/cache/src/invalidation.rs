//! Cache-invalidation subscriber.
//!
//! The Python side invalidates by `DEL`-ing keys (no pub/sub channel exists),
//! so this listens to Valkey keyspace `del` events and yields the deleted key
//! names matching the requested prefixes.

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
}

impl Invalidations {
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
/// (empty = all). Enables keyspace `del` events (`Eg`) best-effort, merging
/// with flags already configured; managed Valkey that forbids `CONFIG SET`
/// must have `notify-keyspace-events` including `Eg` set by the operator.
pub async fn subscribe_invalidations(
    cfg: &Config,
    prefixes: Vec<String>,
) -> Result<Invalidations, CacheError> {
    let client = redis::Client::open(cfg.redis_url.as_str())?;
    enable_notifications(&client).await;
    let mut pubsub = client.get_async_pubsub().await?;
    pubsub.psubscribe(DEL_PATTERN).await?;
    let (tx, rx) = mpsc::channel(BUFFER);
    let task = tokio::spawn(async move {
        let mut stream = pubsub.into_on_message();
        while let Some(msg) = stream.next().await {
            let Ok(key) = msg.get_payload::<String>() else {
                continue;
            };
            if !prefixes.is_empty() && !prefixes.iter().any(|p| key.starts_with(p.as_str())) {
                continue;
            }
            if tx.try_send(Invalidation { key }).is_err() && tx.is_closed() {
                break;
            }
        }
    });
    Ok(Invalidations { rx, task })
}

/// Ensures `E` and `g` are in `notify-keyspace-events`; logs and continues on failure.
async fn enable_notifications(client: &redis::Client) {
    let result: Result<(), redis::RedisError> = async {
        let mut c = client.get_multiplexed_async_connection().await?;
        let cur: Vec<String> = redis::cmd("CONFIG")
            .arg("GET")
            .arg("notify-keyspace-events")
            .query_async(&mut c)
            .await?;
        let mut flags = cur.get(1).cloned().unwrap_or_default();
        let mut changed = false;
        for f in ['E', 'g'] {
            if !flags.contains(f) && !(f == 'g' && flags.contains('A')) {
                flags.push(f);
                changed = true;
            }
        }
        if changed {
            redis::cmd("CONFIG")
                .arg("SET")
                .arg("notify-keyspace-events")
                .arg(&flags)
                .query_async::<()>(&mut c)
                .await?;
        }
        Ok(())
    }
    .await;
    if let Err(e) = result {
        tracing::warn!(error = %e, "could not enable keyspace notifications; operator must set notify-keyspace-events=Eg");
    }
}
