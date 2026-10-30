//! Cache-stampede protection (port of `shared/cache/singleflight.py`).
//!
//! Two layers: an in-process single-flight (the first caller for a key becomes the leader,
//! concurrent callers wait on a `watch` channel for its value) and an optional cross-process
//! Valkey lease (`SET key:lock NX PX`). Everything degrades to "you are the leader, compute your
//! own value": it never blocks past the wait timeout and never fails the request.
//!
//! Async design: the registry is a `std::sync::Mutex` held only for map bookkeeping and never
//! across an `.await`; followers wait on a cloned `watch::Receiver`, so no lock is held while
//! suspended. A leader that is dropped without resolving (blocked/failed request) wakes its
//! followers with "no value" instead of stranding them until their timeout.

use std::collections::HashMap;
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use redis::aio::MultiplexedConnection;
use tokio::sync::watch;

use crate::error::StoreError;
use crate::exact::CachedResponse;
use crate::metrics::CacheMetrics;

/// Opt-out kill-switch flag (unseen/OFF = mechanism ON, ON = legacy independent dispatch).
pub const DISABLE_SINGLEFLIGHT_FLAG: &str = "waddleai.disable-cache-singleflight";

/// Tunables, defaulting to the Python constants and overridable with the same env vars.
#[derive(Debug, Clone, PartialEq)]
pub struct SingleFlightConfig {
    /// How long a Valkey lease is held before it expires on its own.
    pub lease_ttl: Duration,
    /// How long a follower waits for the leader's value before computing its own.
    pub wait_timeout: Duration,
    /// Base poll interval on the cross-process wait path.
    pub poll_interval: Duration,
    /// +/- fraction applied to write TTLs so hot keys do not expire in lockstep.
    pub ttl_jitter_fraction: f64,
    /// Latency budget for the semantic layer's embedding call.
    pub embed_timeout: Duration,
}

impl Default for SingleFlightConfig {
    fn default() -> Self {
        Self {
            lease_ttl: Duration::from_secs(30),
            wait_timeout: Duration::from_secs(3),
            poll_interval: Duration::from_millis(100),
            ttl_jitter_fraction: 0.1,
            embed_timeout: Duration::from_millis(300),
        }
    }
}

impl SingleFlightConfig {
    /// Defaults overridden by `CACHE_STAMPEDE_LEASE_TTL_SECONDS`,
    /// `CACHE_STAMPEDE_WAIT_TIMEOUT_SECONDS`, `CACHE_STAMPEDE_POLL_INTERVAL_SECONDS`,
    /// `CACHE_TTL_JITTER_FRACTION` and `SEMANTIC_CACHE_EMBED_TIMEOUT_MS`. An unparsable value
    /// keeps the default and is logged.
    pub fn from_env() -> Self {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// [`Self::from_env`] over an injectable lookup (tests).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Self {
        let d = Self::default();
        let num = |name: &str, default: f64| -> f64 {
            match get(name) {
                None => default,
                Some(v) => match v.trim().parse::<f64>() {
                    Ok(n) if n.is_finite() && n >= 0.0 => n,
                    _ => {
                        tracing::warn!(
                            var = name,
                            "ignoring unparsable cache tunable; using default"
                        );
                        default
                    }
                },
            }
        };
        Self {
            lease_ttl: Duration::from_secs_f64(num(
                "CACHE_STAMPEDE_LEASE_TTL_SECONDS",
                d.lease_ttl.as_secs_f64(),
            )),
            wait_timeout: Duration::from_secs_f64(num(
                "CACHE_STAMPEDE_WAIT_TIMEOUT_SECONDS",
                d.wait_timeout.as_secs_f64(),
            )),
            poll_interval: Duration::from_secs_f64(num(
                "CACHE_STAMPEDE_POLL_INTERVAL_SECONDS",
                d.poll_interval.as_secs_f64(),
            )),
            ttl_jitter_fraction: num("CACHE_TTL_JITTER_FRACTION", d.ttl_jitter_fraction),
            embed_timeout: Duration::from_secs_f64(
                num(
                    "SEMANTIC_CACHE_EMBED_TIMEOUT_MS",
                    d.embed_timeout.as_secs_f64() * 1000.0,
                ) / 1000.0,
            ),
        }
    }
}

/// Uniform sample in `[0, 1)` from a v4 UUID; non-cryptographic (TTL/poll spread only).
fn unit_random() -> f64 {
    let bits = (uuid::Uuid::new_v4().as_u128() >> 75) as u64; // top 53 bits
    bits as f64 / (1u64 << 53) as f64
}

/// `base_secs` +/- `fraction` (uniform), floored at 1; applied on every cache write so a burst of
/// entries does not expire in lockstep and re-trigger a synchronized stampede.
pub fn jittered_ttl(base_secs: u64, fraction: f64) -> u64 {
    if base_secs == 0 || fraction <= 0.0 {
        return base_secs;
    }
    let spread = base_secs as f64 * fraction;
    let jittered = base_secs as f64 + (unit_random() * 2.0 - 1.0) * spread;
    (jittered.round().max(1.0)) as u64
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// `None` = leader still working; `Some(v)` = finished, with `v = None` meaning "no value".
type Slot<T> = Option<Option<T>>;

struct Entry<T> {
    id: u64,
    tx: watch::Sender<Slot<T>>,
    created: Instant,
}

struct Inner<T> {
    pending: Mutex<HashMap<String, Entry<T>>>,
    next_id: AtomicU64,
}

/// Per-worker de-dup of concurrent callers for the same key. Not a cache: only coordination.
pub struct InProcessSingleFlight<T> {
    inner: Arc<Inner<T>>,
}

impl<T> Clone for InProcessSingleFlight<T> {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}

impl<T> Default for InProcessSingleFlight<T> {
    fn default() -> Self {
        Self::new()
    }
}

/// Outcome of [`InProcessSingleFlight::enter`].
pub enum Entered<T> {
    /// This caller computes; resolve or drop the token when done.
    Leader(LeaderToken<T>),
    /// Another caller is computing; wait for its value.
    Follower(Follower<T>),
}

/// Waits for a leader's value.
pub struct Follower<T> {
    rx: watch::Receiver<Slot<T>>,
}

impl<T: Clone> Follower<T> {
    /// The leader's value, or `None` when the leader produced none, was dropped, or `timeout`
    /// elapsed. Holds no lock while suspended.
    pub async fn wait(mut self, timeout: Duration) -> Option<T> {
        let fut = async {
            match self.rx.wait_for(|s| s.is_some()).await {
                Ok(slot) => slot.clone().flatten(),
                Err(_) => None,
            }
        };
        tokio::time::timeout(timeout, fut).await.ok().flatten()
    }
}

/// The leader's obligation: resolve with the value, or drop to release followers with none.
pub struct LeaderToken<T> {
    inner: Arc<Inner<T>>,
    key: String,
    id: u64,
    done: bool,
}

impl<T> LeaderToken<T> {
    /// Completes the flight: followers receive `value` and the registry entry is cleared.
    pub fn resolve(mut self, value: Option<T>) {
        self.finish(value);
    }

    fn finish(&mut self, value: Option<T>) {
        if self.done {
            return;
        }
        self.done = true;
        let entry = {
            let mut p = lock(&self.inner.pending);
            match p.get(&self.key) {
                Some(e) if e.id == self.id => p.remove(&self.key),
                _ => None,
            }
        };
        if let Some(e) = entry {
            e.tx.send_replace(Some(value));
        }
    }
}

impl<T> Drop for LeaderToken<T> {
    /// An unresolved leader (request blocked or errored) releases followers with no value.
    fn drop(&mut self) {
        self.finish(None);
    }
}

impl<T> InProcessSingleFlight<T> {
    /// Empty registry.
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                pending: Mutex::new(HashMap::new()),
                next_id: AtomicU64::new(1),
            }),
        }
    }

    /// Registers as leader or follower for `key`. An unfinished entry older than `max_age` (a dead
    /// leader) is replaced rather than stranding every later caller behind it.
    pub fn enter(&self, key: &str, max_age: Duration) -> Entered<T> {
        let mut p = lock(&self.inner.pending);
        if let Some(e) = p.get(key) {
            if e.tx.borrow().is_none() && e.created.elapsed() < max_age {
                return Entered::Follower(Follower {
                    rx: e.tx.subscribe(),
                });
            }
        }
        let id = self.inner.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, _rx) = watch::channel(None);
        p.insert(
            key.to_owned(),
            Entry {
                id,
                tx,
                created: Instant::now(),
            },
        );
        Entered::Leader(LeaderToken {
            inner: Arc::clone(&self.inner),
            key: key.to_owned(),
            id,
            done: false,
        })
    }

    /// Entries currently in flight (tests/diagnostics).
    pub fn in_flight(&self) -> usize {
        lock(&self.inner.pending).len()
    }
}

/// Runs `compute` once per concurrent identical `key` in this process; followers share the
/// leader's value. A follower whose leader failed or timed out computes for itself.
pub async fn dedup_compute<T, E, F, Fut>(
    flight: &InProcessSingleFlight<T>,
    key: &str,
    max_age: Duration,
    compute: F,
) -> Result<T, E>
where
    T: Clone,
    F: Fn() -> Fut,
    Fut: Future<Output = Result<T, E>>,
{
    match flight.enter(key, max_age) {
        Entered::Follower(f) => match f.wait(max_age).await {
            Some(v) => Ok(v),
            None => compute().await,
        },
        Entered::Leader(token) => match compute().await {
            Ok(v) => {
                token.resolve(Some(v.clone()));
                Ok(v)
            }
            Err(e) => {
                token.resolve(None);
                Err(e)
            }
        },
    }
}

/// Cross-process lease over a key-value store (Valkey in production).
#[async_trait::async_trait]
pub trait LeaseStore: Send + Sync {
    /// Attempts to take the lease for `cache_key`; true iff this caller now holds it.
    async fn try_acquire(
        &self,
        cache_key: &str,
        ttl: Duration,
        token: &str,
    ) -> Result<bool, StoreError>;
    /// Best-effort release: frees the lease only if `token` still owns it.
    async fn release(&self, cache_key: &str, token: &str);
}

/// Valkey lease: `SET {key}:lock token NX PX ttl`, release = get-compare-delete (non-atomic, the
/// same documented trade-off as Python: a lost race lets the lease expire on its own TTL).
pub struct ValkeyLease {
    conn: MultiplexedConnection,
}

impl ValkeyLease {
    /// Binds a multiplexed Valkey connection.
    pub fn new(conn: MultiplexedConnection) -> Self {
        Self { conn }
    }
}

#[async_trait::async_trait]
impl LeaseStore for ValkeyLease {
    async fn try_acquire(
        &self,
        cache_key: &str,
        ttl: Duration,
        token: &str,
    ) -> Result<bool, StoreError> {
        let mut c = self.conn.clone();
        let px = ttl.as_millis().max(1) as u64;
        let reply: Option<String> = redis::cmd("SET")
            .arg(cache::keys::lock(cache_key))
            .arg(token)
            .arg("NX")
            .arg("PX")
            .arg(px)
            .query_async(&mut c)
            .await?;
        Ok(reply.is_some())
    }

    async fn release(&self, cache_key: &str, token: &str) {
        let mut c = self.conn.clone();
        let lock_key = cache::keys::lock(cache_key);
        let current: Result<Option<String>, _> =
            redis::cmd("GET").arg(&lock_key).query_async(&mut c).await;
        match current {
            Ok(Some(held)) if held == token => {
                let del: Result<i64, _> =
                    redis::cmd("DEL").arg(&lock_key).query_async(&mut c).await;
                if let Err(e) = del {
                    tracing::warn!(error = %e, "stampede lease release failed; it will expire");
                }
            }
            Ok(_) => {}
            Err(e) => tracing::warn!(error = %e, "stampede lease release check failed"),
        }
    }
}

/// A held cross-process lease.
struct LeaseHold {
    store: Arc<dyn LeaseStore>,
    key: String,
    token: String,
}

/// Everything a leader owes the system after its write attempt: resolve the in-process flight and
/// release the Valkey lease. Dropping it unfinished (request blocked/errored) releases both so
/// followers are never stranded.
#[derive(Default)]
pub struct FlightGuard {
    leader: Option<LeaderToken<CachedResponse>>,
    lease: Option<LeaseHold>,
}

impl FlightGuard {
    /// A guard holding nothing (kill-switch on, or a follower that fell through).
    pub fn none() -> Self {
        Self::default()
    }

    /// Resolves followers with `value` and releases the lease.
    pub async fn finish(mut self, value: Option<CachedResponse>) {
        if let Some(l) = self.leader.take() {
            l.resolve(value);
        }
        if let Some(h) = self.lease.take() {
            h.store.release(&h.key, &h.token).await;
        }
    }
}

impl Drop for FlightGuard {
    /// Unfinished guard: followers are released by the token's own drop; the lease is released on
    /// a background task when a runtime is available, otherwise it expires on its TTL.
    fn drop(&mut self) {
        if let Some(h) = self.lease.take() {
            match tokio::runtime::Handle::try_current() {
                Ok(rt) => {
                    rt.spawn(async move { h.store.release(&h.key, &h.token).await });
                }
                Err(_) => tracing::warn!("no runtime to release stampede lease; it will expire"),
            }
        }
    }
}

/// Outcome of [`guard_miss`].
pub struct MissGuard {
    /// True when this caller must compute (dispatch upstream / embed+query) and write back.
    pub is_leader: bool,
    /// A value another leader already produced; equivalent to a cache hit.
    pub cached: Option<CachedResponse>,
    /// Obligations to settle after the write attempt.
    pub flight: FlightGuard,
}

/// Inputs to [`guard_miss`].
pub struct MissGuardInput<'a> {
    /// Lease/flight key (org-scoped; see `cache::keys::exact_lease`).
    pub cache_key: &'a str,
    /// In-process registry for this layer.
    pub in_process: &'a InProcessSingleFlight<CachedResponse>,
    /// Cross-process lease store, when Valkey is available.
    pub lease: Option<&'a Arc<dyn LeaseStore>>,
    /// Metric layer label (`exact` or `semantic`).
    pub layer: &'static str,
    /// The caller's resolved kill-switch state.
    pub enabled: bool,
    /// Timing tunables.
    pub cfg: &'a SingleFlightConfig,
    /// Metric sink.
    pub metrics: &'a CacheMetrics,
}

/// Jittered-poll `fetch` until it yields a value or `timeout` elapses.
async fn wait_for_value<F, Fut>(
    fetch: F,
    timeout: Duration,
    poll: Duration,
) -> Option<CachedResponse>
where
    F: Fn() -> Fut,
    Fut: Future<Output = Option<CachedResponse>>,
{
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(v) = fetch().await {
            return Some(v);
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return None;
        }
        let spread = poll.as_secs_f64() * 0.3;
        let jittered = poll.as_secs_f64() + (unit_random() * 2.0 - 1.0) * spread;
        let delay = Duration::from_secs_f64(jittered.max(0.01)).min(remaining);
        tokio::time::sleep(delay).await;
    }
}

/// Single-flight guard for one cache-key miss, shared by the exact and semantic layers. Never
/// raises and never blocks past the wait timeout: every degraded path resolves to "you are the
/// leader, compute your own value".
pub async fn guard_miss<F, Fut>(input: MissGuardInput<'_>, fetch_cached: F) -> MissGuard
where
    F: Fn() -> Fut,
    Fut: Future<Output = Option<CachedResponse>>,
{
    let MissGuardInput {
        cache_key,
        in_process,
        lease,
        layer,
        enabled,
        cfg,
        metrics,
    } = input;
    if !enabled {
        metrics.lookup(layer, "bypass");
        return MissGuard {
            is_leader: true,
            cached: None,
            flight: FlightGuard::none(),
        };
    }

    let token = match in_process.enter(cache_key, cfg.lease_ttl) {
        Entered::Follower(f) => {
            return match f.wait(cfg.wait_timeout).await {
                Some(v) => {
                    metrics.lookup(layer, "stampede_wait");
                    MissGuard {
                        is_leader: false,
                        cached: Some(v),
                        flight: FlightGuard::none(),
                    }
                }
                None => {
                    metrics.lookup(layer, "stampede_fallthrough");
                    MissGuard {
                        is_leader: true,
                        cached: None,
                        flight: FlightGuard::none(),
                    }
                }
            };
        }
        Entered::Leader(t) => t,
    };

    let Some(store) = lease else {
        // No cross-process coordination available: in-process single-flight still applies.
        return MissGuard {
            is_leader: true,
            cached: None,
            flight: FlightGuard {
                leader: Some(token),
                lease: None,
            },
        };
    };

    let lease_token = uuid::Uuid::new_v4().simple().to_string()[..16].to_owned();
    match store
        .try_acquire(cache_key, cfg.lease_ttl, &lease_token)
        .await
    {
        Err(e) => {
            tracing::warn!(error = %e, layer, "stampede lease unavailable; degrading to in-process only");
            MissGuard {
                is_leader: true,
                cached: None,
                flight: FlightGuard {
                    leader: Some(token),
                    lease: None,
                },
            }
        }
        Ok(true) => MissGuard {
            is_leader: true,
            cached: None,
            flight: FlightGuard {
                leader: Some(token),
                lease: Some(LeaseHold {
                    store: Arc::clone(store),
                    key: cache_key.to_owned(),
                    token: lease_token,
                }),
            },
        },
        Ok(false) => {
            let found = wait_for_value(fetch_cached, cfg.wait_timeout, cfg.poll_interval).await;
            token.resolve(found.clone());
            match found {
                Some(v) => {
                    metrics.lookup(layer, "stampede_wait");
                    MissGuard {
                        is_leader: false,
                        cached: Some(v),
                        flight: FlightGuard::none(),
                    }
                }
                None => {
                    metrics.lookup(layer, "stampede_fallthrough");
                    MissGuard {
                        is_leader: true,
                        cached: None,
                        flight: FlightGuard::none(),
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicUsize;

    use super::*;

    fn metrics() -> CacheMetrics {
        CacheMetrics::new(Arc::new(
            telemetry::Metrics::new("sf-test").expect("registry"),
        ))
    }

    fn val(s: &str) -> CachedResponse {
        CachedResponse {
            response: serde_json::json!({ "s": s }),
            usage: serde_json::json!({}),
            stored_at: 0.0,
        }
    }

    #[test]
    fn jitter_stays_within_fraction_and_floors_at_one() {
        for _ in 0..500 {
            let t = jittered_ttl(1000, 0.1);
            assert!((900..=1100).contains(&t), "{t}");
        }
        assert_eq!(jittered_ttl(0, 0.1), 0);
        assert_eq!(jittered_ttl(50, 0.0), 50);
        assert!(jittered_ttl(1, 0.9) >= 1);
        // not constant: jitter actually varies
        let distinct: std::collections::HashSet<u64> =
            (0..200).map(|_| jittered_ttl(100_000, 0.1)).collect();
        assert!(distinct.len() > 10);
    }

    #[test]
    fn config_reads_env_names_and_ignores_garbage() {
        let cfg = SingleFlightConfig::from_lookup(|k| match k {
            "CACHE_STAMPEDE_LEASE_TTL_SECONDS" => Some("12".into()),
            "CACHE_STAMPEDE_WAIT_TIMEOUT_SECONDS" => Some("0.5".into()),
            "CACHE_STAMPEDE_POLL_INTERVAL_SECONDS" => Some("nope".into()),
            "CACHE_TTL_JITTER_FRACTION" => Some("0.25".into()),
            "SEMANTIC_CACHE_EMBED_TIMEOUT_MS" => Some("50".into()),
            _ => None,
        });
        assert_eq!(cfg.lease_ttl, Duration::from_secs(12));
        assert_eq!(cfg.wait_timeout, Duration::from_millis(500));
        assert_eq!(cfg.poll_interval, Duration::from_millis(100));
        assert_eq!(cfg.ttl_jitter_fraction, 0.25);
        assert_eq!(cfg.embed_timeout, Duration::from_millis(50));
        assert_eq!(
            SingleFlightConfig::from_lookup(|_| None),
            SingleFlightConfig::default()
        );
    }

    #[tokio::test]
    async fn follower_receives_leader_value_and_entry_clears() {
        let sf = InProcessSingleFlight::<String>::new();
        let Entered::Leader(l) = sf.enter("k", Duration::from_secs(5)) else {
            panic!("first caller leads")
        };
        let Entered::Follower(f) = sf.enter("k", Duration::from_secs(5)) else {
            panic!("second caller follows")
        };
        assert_eq!(sf.in_flight(), 1);
        l.resolve(Some("v".into()));
        assert_eq!(f.wait(Duration::from_secs(1)).await.as_deref(), Some("v"));
        assert_eq!(sf.in_flight(), 0);
    }

    #[tokio::test]
    async fn follower_times_out_without_cancelling_the_leader() {
        let sf = InProcessSingleFlight::<String>::new();
        let Entered::Leader(l) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        let Entered::Follower(f1) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        let Entered::Follower(f2) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        assert_eq!(f1.wait(Duration::from_millis(20)).await, None);
        l.resolve(Some("late".into()));
        // f1's timeout must not have poisoned the shared flight for f2
        assert_eq!(
            f2.wait(Duration::from_secs(1)).await.as_deref(),
            Some("late")
        );
    }

    #[tokio::test]
    async fn dropped_leader_releases_followers_with_none() {
        let sf = InProcessSingleFlight::<String>::new();
        let Entered::Leader(l) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        let Entered::Follower(f) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        drop(l);
        assert_eq!(f.wait(Duration::from_secs(1)).await, None);
        assert_eq!(sf.in_flight(), 0);
    }

    #[tokio::test]
    async fn stale_leader_is_replaced_and_old_resolve_cannot_clobber() {
        let sf = InProcessSingleFlight::<String>::new();
        let Entered::Leader(old) = sf.enter("k", Duration::from_millis(1)) else {
            panic!()
        };
        tokio::time::sleep(Duration::from_millis(5)).await;
        let Entered::Leader(new) = sf.enter("k", Duration::from_millis(1)) else {
            panic!("stale entry must be replaced")
        };
        let Entered::Follower(f) = sf.enter("k", Duration::from_secs(5)) else {
            panic!()
        };
        old.resolve(Some("stale".into())); // id mismatch: ignored
        assert_eq!(sf.in_flight(), 1);
        new.resolve(Some("fresh".into()));
        assert_eq!(
            f.wait(Duration::from_secs(1)).await.as_deref(),
            Some("fresh")
        );
    }

    #[tokio::test]
    async fn dedup_compute_runs_once_and_falls_back_after_leader_error() {
        let sf = InProcessSingleFlight::<u32>::new();
        let calls = Arc::new(AtomicUsize::new(0));
        let mk = |calls: Arc<AtomicUsize>| {
            move || {
                let calls = calls.clone();
                async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    Ok::<u32, String>(7)
                }
            }
        };
        let (a, b, c) = tokio::join!(
            dedup_compute(&sf, "e", Duration::from_secs(5), mk(calls.clone())),
            dedup_compute(&sf, "e", Duration::from_secs(5), mk(calls.clone())),
            dedup_compute(&sf, "e", Duration::from_secs(5), mk(calls.clone())),
        );
        assert_eq!((a, b, c), (Ok(7), Ok(7), Ok(7)));
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        // leader error: error returned to the leader, follower recomputes
        let n = Arc::new(AtomicUsize::new(0));
        let failing = |n: Arc<AtomicUsize>| {
            move || {
                let n = n.clone();
                async move {
                    let i = n.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(30)).await;
                    if i == 0 {
                        Err("boom".to_owned())
                    } else {
                        Ok(9u32)
                    }
                }
            }
        };
        let (x, y) = tokio::join!(
            dedup_compute(&sf, "f", Duration::from_secs(5), failing(n.clone())),
            dedup_compute(&sf, "f", Duration::from_secs(5), failing(n.clone())),
        );
        assert_eq!(x, Err("boom".to_owned()));
        assert_eq!(y, Ok(9));
    }

    struct FakeLease {
        held: Mutex<Option<String>>,
        refuse: bool,
        fail: bool,
        released: AtomicUsize,
    }

    impl FakeLease {
        fn new(refuse: bool, fail: bool) -> Arc<Self> {
            Arc::new(Self {
                held: Mutex::new(None),
                refuse,
                fail,
                released: AtomicUsize::new(0),
            })
        }
    }

    #[async_trait::async_trait]
    impl LeaseStore for FakeLease {
        async fn try_acquire(
            &self,
            _k: &str,
            _t: Duration,
            token: &str,
        ) -> Result<bool, StoreError> {
            if self.fail {
                return Err(StoreError("valkey down".into()));
            }
            if self.refuse {
                return Ok(false);
            }
            *lock(&self.held) = Some(token.to_owned());
            Ok(true)
        }
        async fn release(&self, _k: &str, token: &str) {
            let mut h = lock(&self.held);
            if h.as_deref() == Some(token) {
                *h = None;
                self.released.fetch_add(1, Ordering::SeqCst);
            }
        }
    }

    fn input<'a>(
        sf: &'a InProcessSingleFlight<CachedResponse>,
        lease: Option<&'a Arc<dyn LeaseStore>>,
        cfg: &'a SingleFlightConfig,
        m: &'a CacheMetrics,
        enabled: bool,
    ) -> MissGuardInput<'a> {
        MissGuardInput {
            cache_key: "k",
            in_process: sf,
            lease,
            layer: "exact",
            enabled,
            cfg,
            metrics: m,
        }
    }

    #[tokio::test]
    async fn killswitch_off_bypasses_with_metric() {
        let (sf, cfg, m) = (
            InProcessSingleFlight::new(),
            SingleFlightConfig::default(),
            metrics(),
        );
        let g = guard_miss(input(&sf, None, &cfg, &m, false), || async { None }).await;
        assert!(g.is_leader && g.cached.is_none());
        assert_eq!(sf.in_flight(), 0, "kill-switch registers nothing");
    }

    #[tokio::test]
    async fn lease_acquired_is_released_by_finish() {
        let (sf, cfg, m) = (
            InProcessSingleFlight::new(),
            SingleFlightConfig::default(),
            metrics(),
        );
        let fake = FakeLease::new(false, false);
        let lease: Arc<dyn LeaseStore> = fake.clone();
        let g = guard_miss(input(&sf, Some(&lease), &cfg, &m, true), || async { None }).await;
        assert!(g.is_leader);
        assert!(lock(&fake.held).is_some());
        g.flight.finish(Some(val("x"))).await;
        assert!(lock(&fake.held).is_none());
        assert_eq!(fake.released.load(Ordering::SeqCst), 1);
        assert_eq!(sf.in_flight(), 0);
    }

    #[tokio::test]
    async fn dropped_guard_releases_lease_in_background() {
        let (sf, cfg, m) = (
            InProcessSingleFlight::new(),
            SingleFlightConfig::default(),
            metrics(),
        );
        let fake = FakeLease::new(false, false);
        let lease: Arc<dyn LeaseStore> = fake.clone();
        let g = guard_miss(input(&sf, Some(&lease), &cfg, &m, true), || async { None }).await;
        drop(g);
        for _ in 0..50 {
            if lock(&fake.held).is_none() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(lock(&fake.held).is_none(), "background release ran");
    }

    #[tokio::test]
    async fn lease_error_degrades_to_in_process_leader() {
        let (sf, cfg, m) = (
            InProcessSingleFlight::new(),
            SingleFlightConfig::default(),
            metrics(),
        );
        let lease: Arc<dyn LeaseStore> = FakeLease::new(false, true);
        let g = guard_miss(input(&sf, Some(&lease), &cfg, &m, true), || async { None }).await;
        assert!(g.is_leader && g.cached.is_none());
        assert_eq!(sf.in_flight(), 1, "still the in-process leader");
    }

    #[tokio::test]
    async fn lease_denied_polls_until_value_then_serves_it() {
        let (sf, m) = (InProcessSingleFlight::new(), metrics());
        let cfg = SingleFlightConfig {
            wait_timeout: Duration::from_secs(2),
            poll_interval: Duration::from_millis(10),
            ..SingleFlightConfig::default()
        };
        let lease: Arc<dyn LeaseStore> = FakeLease::new(true, false);
        let polls = Arc::new(AtomicUsize::new(0));
        let p = polls.clone();
        let g = guard_miss(input(&sf, Some(&lease), &cfg, &m, true), move || {
            let p = p.clone();
            async move {
                if p.fetch_add(1, Ordering::SeqCst) >= 2 {
                    Some(val("peer"))
                } else {
                    None
                }
            }
        })
        .await;
        assert!(!g.is_leader);
        assert_eq!(
            g.cached.map(|c| c.response["s"].clone()),
            Some("peer".into())
        );
        assert!(polls.load(Ordering::SeqCst) >= 3);
        assert_eq!(sf.in_flight(), 0);
    }

    #[tokio::test]
    async fn lease_denied_and_no_value_falls_through_as_leader() {
        let (sf, m) = (InProcessSingleFlight::new(), metrics());
        let cfg = SingleFlightConfig {
            wait_timeout: Duration::from_millis(40),
            poll_interval: Duration::from_millis(10),
            ..SingleFlightConfig::default()
        };
        let lease: Arc<dyn LeaseStore> = FakeLease::new(true, false);
        let g = guard_miss(input(&sf, Some(&lease), &cfg, &m, true), || async { None }).await;
        assert!(g.is_leader && g.cached.is_none());
        assert_eq!(sf.in_flight(), 0);
    }
}
