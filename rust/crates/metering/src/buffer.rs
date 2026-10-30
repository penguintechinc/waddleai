//! `MeteringBuffer`: batches usage events and flushes aggregates to the usage table.
//!
//! Port of the Python buffer. `record` is synchronous and never blocks the request. Flushes run
//! on a size threshold, on an interval, and at shutdown. A failed aggregate is kept for retry
//! (bounded; the OLDEST are dropped, loudly, past the bound) so a database blip never drops
//! billable usage and a dead database never fails a response. No lock is held across an await.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use telemetry::Metrics;
use tokio::sync::Notify;
use tokio::task::JoinHandle;

use crate::event::{
    aggregate_events, aggregate_key_events, Aggregate, KeyAggregate, KeyUsageEvent, MeteringEvent,
};
use crate::writer::UsageWriter;

/// Longest the shutdown waits for the background loop to wind down (Python: 5s).
const STOP_TIMEOUT: Duration = Duration::from_secs(5);

/// Flush policy.
#[derive(Debug, Clone)]
pub struct BufferConfig {
    /// Time between background flushes (Python default 1s).
    pub interval: Duration,
    /// Buffered events that trigger an immediate flush.
    pub size_threshold: usize,
    /// Failed aggregates retained for retry (Python `max_pending_aggregates` = 1000).
    pub max_pending: usize,
}

impl Default for BufferConfig {
    fn default() -> Self {
        Self {
            interval: Duration::from_secs(1),
            size_threshold: 500,
            max_pending: 1000,
        }
    }
}

/// Events awaiting aggregation plus aggregates awaiting a retry.
#[derive(Default)]
struct State {
    events: Vec<MeteringEvent>,
    pending: Vec<Aggregate>,
    key_events: Vec<KeyUsageEvent>,
    key_pending: Vec<KeyAggregate>,
}

/// Batched, retrying usage writer front-end.
pub struct MeteringBuffer {
    writer: Arc<dyn UsageWriter>,
    cfg: BufferConfig,
    metrics: Option<Arc<Metrics>>,
    state: Mutex<State>,
    wake: Notify,
    stop: Notify,
    stopped: AtomicBool,
    task: Mutex<Option<JoinHandle<()>>>,
}

/// Locks a std mutex, recovering from poisoning (state stays consistent: only pushes/takes).
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

impl MeteringBuffer {
    /// Builds the buffer; call [`MeteringBuffer::start`] to run the background flusher.
    pub fn new(
        writer: Arc<dyn UsageWriter>,
        cfg: BufferConfig,
        metrics: Option<Arc<Metrics>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            writer,
            cfg,
            metrics,
            state: Mutex::new(State::default()),
            wake: Notify::new(),
            stop: Notify::new(),
            stopped: AtomicBool::new(false),
            task: Mutex::new(None),
        })
    }

    /// Queues one event (never blocks, never fails); wakes the flusher at the size threshold.
    pub fn record(&self, event: MeteringEvent) {
        let len = {
            let mut st = lock(&self.state);
            st.events.push(event);
            st.events.len()
        };
        if len >= self.cfg.size_threshold {
            self.wake.notify_one();
        }
    }

    /// Queues the API-key-keyed usage event (the second Python writer); same guarantees.
    pub fn record_key_usage(&self, event: KeyUsageEvent) {
        let len = {
            let mut st = lock(&self.state);
            st.key_events.push(event);
            st.key_events.len()
        };
        if len >= self.cfg.size_threshold {
            self.wake.notify_one();
        }
    }

    /// Aggregates (vkey + API-key rows) awaiting a retry after a failed write.
    pub fn pending_len(&self) -> usize {
        let st = lock(&self.state);
        st.pending.len() + st.key_pending.len()
    }

    /// Starts the background flush loop (idempotent).
    pub fn start(self: &Arc<Self>) {
        let mut slot = lock(&self.task);
        if slot.is_some() {
            return;
        }
        self.stopped.store(false, Ordering::SeqCst);
        let this = Arc::clone(self);
        *slot = Some(tokio::spawn(async move {
            loop {
                tokio::select! {
                    () = tokio::time::sleep(this.cfg.interval) => {}
                    () = this.wake.notified() => {}
                    () = this.stop.notified() => {}
                }
                if this.stopped.load(Ordering::SeqCst) {
                    break;
                }
                this.flush().await;
            }
        }));
        tracing::info!(
            interval_ms = self.cfg.interval.as_millis() as u64,
            "metering buffer started"
        );
    }

    /// Stops the loop and flushes what is left, so no recorded usage is lost on shutdown.
    pub async fn shutdown(&self) {
        self.stopped.store(true, Ordering::SeqCst);
        self.stop.notify_one();
        let handle = lock(&self.task).take();
        if let Some(h) = handle {
            if tokio::time::timeout(STOP_TIMEOUT, h).await.is_err() {
                tracing::warn!("metering flush task did not stop within the timeout");
            }
        }
        self.flush().await;
        let lost = self.pending_len();
        if lost > 0 {
            tracing::error!(
                aggregates = lost,
                "metering shutdown with unwritten aggregates; usage will be unbilled"
            );
        }
        tracing::info!("metering buffer stopped");
    }

    /// Writes buffered events (and earlier failures); failures are re-queued, never raised.
    pub async fn flush(&self) {
        let (events, retries, key_events, key_retries) = {
            let mut st = lock(&self.state);
            (
                std::mem::take(&mut st.events),
                std::mem::take(&mut st.pending),
                std::mem::take(&mut st.key_events),
                std::mem::take(&mut st.key_pending),
            )
        };
        if events.is_empty()
            && retries.is_empty()
            && key_events.is_empty()
            && key_retries.is_empty()
        {
            return;
        }
        let mut to_write = retries;
        to_write.extend(aggregate_events(events));
        let mut failed = Vec::new();
        for agg in to_write {
            let started = Instant::now();
            let result = self.writer.write(&agg).await;
            self.db_metric(result.is_ok(), started.elapsed().as_secs_f64());
            if let Err(err) = result {
                tracing::error!(
                    vkey_id = agg.virtual_key_id,
                    error = %err,
                    "metering write failed; re-queueing"
                );
                failed.push(agg);
            }
        }
        let mut key_to_write = key_retries;
        key_to_write.extend(aggregate_key_events(key_events));
        let mut key_failed = Vec::new();
        for agg in key_to_write {
            let started = Instant::now();
            let result = self.writer.write_key(&agg).await;
            self.db_metric(result.is_ok(), started.elapsed().as_secs_f64());
            if let Err(err) = result {
                tracing::error!(error = %err, "api-key usage write failed; re-queueing");
                key_failed.push(agg);
            }
        }
        if failed.is_empty() && key_failed.is_empty() {
            return;
        }
        let mut st = lock(&self.state);
        st.pending.extend(failed);
        st.key_pending.extend(key_failed);
        let overflow = st.pending.len().saturating_sub(self.cfg.max_pending);
        if overflow > 0 {
            let dropped: Vec<Aggregate> = st.pending.drain(..overflow).collect();
            let input: u64 = dropped.iter().map(|a| a.total_input_tokens).sum();
            let output: u64 = dropped.iter().map(|a| a.total_output_tokens).sum();
            tracing::error!(
                dropped = overflow,
                input_tokens = input,
                output_tokens = output,
                max_pending = self.cfg.max_pending,
                "metering retry queue full; oldest aggregates dropped, tokens will be unbilled"
            );
        }
        let key_overflow = st.key_pending.len().saturating_sub(self.cfg.max_pending);
        if key_overflow > 0 {
            let dropped: Vec<KeyAggregate> = st.key_pending.drain(..key_overflow).collect();
            let credits: u64 = dropped.iter().map(|a| a.waddleai_tokens).sum();
            tracing::error!(
                dropped = key_overflow,
                waddleai_tokens = credits,
                max_pending = self.cfg.max_pending,
                "api-key usage retry queue full; oldest aggregates dropped, usage will be unbilled"
            );
        }
    }

    /// Python `record_database_operation("upsert", "token_usage", ..)`; failures only warn.
    fn db_metric(&self, ok: bool, secs: f64) {
        let Some(m) = &self.metrics else { return };
        let status = if ok { "success" } else { "error" };
        match m.counter("waddleai_database_operations_total") {
            Ok(c) => c
                .with_label_values(&["upsert", "token_usage", status])
                .inc(),
            Err(e) => tracing::warn!(error = %e, "db operation metric unavailable"),
        }
        match m.histogram("waddleai_database_operation_duration_seconds") {
            Ok(h) => h
                .with_label_values(&["upsert", "token_usage"])
                .observe(secs.max(0.0)),
            Err(e) => tracing::warn!(error = %e, "db duration metric unavailable"),
        }
    }
}
