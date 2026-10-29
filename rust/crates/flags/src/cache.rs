//! Bounded last-known-value cache with TTL freshness and age reporting.

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Map of key -> (value, resolved-at). Locks are never held across `.await`.
pub(crate) struct LastKnown<K, V> {
    inner: Mutex<HashMap<K, (V, Instant)>>,
    max_entries: usize,
}

impl<K: Hash + Eq + Clone, V: Copy> LastKnown<K, V> {
    pub(crate) fn new(max_entries: usize) -> Self {
        Self {
            inner: Mutex::new(HashMap::new()),
            max_entries: max_entries.max(1),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, HashMap<K, (V, Instant)>> {
        // A poisoned cache holds only plain data; keep serving it.
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Value resolved less than `ttl` ago.
    pub(crate) fn fresh(&self, key: &K, ttl: Duration) -> Option<V> {
        let g = self.lock();
        g.get(key)
            .filter(|(_, at)| at.elapsed() < ttl)
            .map(|(v, _)| *v)
    }

    /// Last-known value and its age, regardless of TTL.
    pub(crate) fn any(&self, key: &K) -> Option<(V, Duration)> {
        self.lock().get(key).map(|(v, at)| (*v, at.elapsed()))
    }

    /// Stores a value, evicting the oldest entry when over budget.
    pub(crate) fn put(&self, key: K, value: V) {
        let mut g = self.lock();
        g.insert(key, (value, Instant::now()));
        while g.len() > self.max_entries {
            let oldest = g
                .iter()
                .min_by_key(|(_, (_, at))| *at)
                .map(|(k, _)| k.clone());
            match oldest {
                Some(k) => g.remove(&k),
                None => break,
            };
        }
    }

    pub(crate) fn len(&self) -> usize {
        self.lock().len()
    }
}
