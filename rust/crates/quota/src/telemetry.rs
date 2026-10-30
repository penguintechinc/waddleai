//! OpenTelemetry instruments for the pooled quota (names match `shared/quota/telemetry.py`).
//!
//! Attributes are CLOSED enums only (outcome/reason/applied/kind): user, team and org ids are
//! PII-adjacent and unbounded, so they never become metric attributes. Emission is
//! best-effort and a no-op when no exporter is configured.

use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};

/// The quota counters and histograms bound to the global meter.
pub struct QuotaTelemetry {
    decisions: Counter<u64>,
    rejections: Counter<u64>,
    leases: Counter<u64>,
    reconciles: Counter<u64>,
    overages: Counter<u64>,
    faults: Counter<u64>,
    reserved: Histogram<u64>,
    actual: Histogram<u64>,
    lease: Histogram<u64>,
    latency: Histogram<f64>,
}

impl Default for QuotaTelemetry {
    fn default() -> Self {
        Self::new()
    }
}

impl QuotaTelemetry {
    /// Creates every instrument on the global meter.
    pub fn new() -> Self {
        let m = global::meter("waddleai-quota");
        let counter = |name: &'static str, desc: &'static str| {
            m.u64_counter(name)
                .with_unit("1")
                .with_description(desc)
                .build()
        };
        let credits = |name: &'static str, desc: &'static str| {
            m.u64_histogram(name)
                .with_unit("credit")
                .with_description(desc)
                .build()
        };
        Self {
            decisions: counter("waddleai.quota.decisions", "Reserve outcomes"),
            rejections: counter("waddleai.quota.rejections", "Requests refused with 429"),
            leases: counter(
                "waddleai.quota.buffer_leases",
                "Reserves leasing from the buffer",
            ),
            reconciles: counter("waddleai.quota.reconciles", "Reconcile attempts"),
            overages: counter("waddleai.quota.overages", "Buffer or override cap overages"),
            faults: counter(
                "waddleai.quota.faults",
                "Script errors and fail-open events",
            ),
            reserved: credits("waddleai.quota.credits_reserved", "Credits reserved"),
            actual: credits("waddleai.quota.credits_actual", "Actual credits reconciled"),
            lease: credits(
                "waddleai.quota.buffer_lease_credits",
                "Credits leased from buffer",
            ),
            latency: m
                .f64_histogram("waddleai.quota.reserve_duration")
                .with_unit("s")
                .with_description("Reserve latency incl. config")
                .build(),
        }
    }

    /// Counts one reserve outcome (`reason` only when there is one).
    pub fn decision(&self, outcome: &str, reason: Option<&str>) {
        let mut a = vec![KeyValue::new("outcome", outcome.to_owned())];
        if let Some(r) = reason {
            a.push(KeyValue::new("reason", r.to_owned()));
        }
        self.decisions.add(1, &a);
    }

    /// Counts one 429 by reason.
    pub fn rejection(&self, reason: &str) {
        self.rejections
            .add(1, &[KeyValue::new("reason", reason.to_owned())]);
    }

    /// Records one buffer lease and its size.
    pub fn lease(&self, credits: i64) {
        self.leases.add(1, &[]);
        self.lease.record(credits.max(0) as u64, &[]);
    }

    /// Records the credits reserved for an admitted request.
    pub fn reserved(&self, credits: i64) {
        self.reserved.record(credits.max(0) as u64, &[]);
    }

    /// Counts a reconcile and records the actual credits when it applied.
    pub fn reconciled(&self, applied: bool, credits: i64) {
        self.reconciles
            .add(1, &[KeyValue::new("applied", applied.to_string())]);
        if applied {
            self.actual.record(credits.max(0) as u64, &[]);
        }
    }

    /// Counts an overage (`kind` = buffer | override).
    pub fn overage(&self, kind: &str) {
        self.overages
            .add(1, &[KeyValue::new("kind", kind.to_owned())]);
    }

    /// Counts a fault so a fail-open or lost refund is visible, never silent.
    pub fn fault(&self, kind: &str) {
        self.faults
            .add(1, &[KeyValue::new("kind", kind.to_owned())]);
    }

    /// Records reserve latency in seconds.
    pub fn reserve_seconds(&self, secs: f64) {
        self.latency.record(secs, &[]);
    }
}
