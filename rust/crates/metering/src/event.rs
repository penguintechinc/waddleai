//! Metering events and the per-minute aggregation, ported from `shared/utils/metering.py`.
//!
//! Events are aggregated by `(virtual_key_id, model, provider, minute)` before they are written,
//! so a burst of requests becomes one row update. A missing usage is metered as the Python
//! placeholder estimate (1 input / 1 output token) and flagged `estimated`.

use std::collections::HashMap;

use chrono::{NaiveDateTime, Timelike};

/// Provider-reported token counts for one request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EventUsage {
    /// Prompt tokens.
    pub input_tokens: u64,
    /// Completion tokens.
    pub output_tokens: u64,
}

/// One request's usage, queued for the batched `token_usage` write.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MeteringEvent {
    /// Virtual key the usage is billed to.
    pub virtual_key_id: i64,
    /// Resolved model name.
    pub model: String,
    /// Provider that served the request (`cache` for a response-cache hit).
    pub provider: String,
    /// Provider usage; `None` triggers the estimate at aggregation time.
    pub usage: Option<EventUsage>,
    /// When the request completed (UTC, naive like the Python `utcnow()`).
    pub timestamp: NaiveDateTime,
    /// Carried for parity with Python, which never reads it (the flag comes from `usage`).
    pub estimated: bool,
    /// `exact|semantic|miss` response-cache outcome.
    pub cache_status: Option<String>,
    /// Tokens avoided by a cache hit (0 for misses).
    pub tokens_saved: u64,
}

/// Aggregated usage for one `(virtual_key_id, model, provider, minute)` bucket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Aggregate {
    /// Virtual key id.
    pub virtual_key_id: i64,
    /// Model name.
    pub model: String,
    /// Provider name.
    pub provider: String,
    /// Event timestamp truncated to the minute.
    pub minute_bucket: NaiveDateTime,
    /// Summed prompt tokens.
    pub total_input_tokens: u64,
    /// Summed completion tokens.
    pub total_output_tokens: u64,
    /// Number of events in the bucket.
    pub request_count: u64,
    /// True when any event in the bucket had no provider usage.
    pub has_estimated_usage: bool,
    /// Summed `tokens_saved`.
    pub total_tokens_saved: u64,
    /// Last non-`None` cache status in the bucket (last-write-wins, as in Python).
    pub cache_status: Option<String>,
}

/// Bucket key: vkey, model, provider, minute.
type BucketKey = (i64, String, String, NaiveDateTime);

/// Truncates a timestamp to the minute.
fn minute_bucket(ts: NaiveDateTime) -> NaiveDateTime {
    ts.with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .unwrap_or(ts)
}

/// Aggregates events per bucket, preserving first-seen bucket order (Python dict order).
pub fn aggregate_events(events: Vec<MeteringEvent>) -> Vec<Aggregate> {
    let mut index: HashMap<BucketKey, usize> = HashMap::new();
    let mut out: Vec<Aggregate> = Vec::new();
    for ev in events {
        let bucket = minute_bucket(ev.timestamp);
        let key = (
            ev.virtual_key_id,
            ev.model.clone(),
            ev.provider.clone(),
            bucket,
        );
        let slot = *index.entry(key).or_insert_with(|| {
            out.push(Aggregate {
                virtual_key_id: ev.virtual_key_id,
                model: ev.model.clone(),
                provider: ev.provider.clone(),
                minute_bucket: bucket,
                total_input_tokens: 0,
                total_output_tokens: 0,
                request_count: 0,
                has_estimated_usage: false,
                total_tokens_saved: 0,
                cache_status: None,
            });
            out.len() - 1
        });
        let agg = &mut out[slot];
        let (input, output) = match ev.usage {
            Some(u) => (u.input_tokens, u.output_tokens),
            None => {
                agg.has_estimated_usage = true;
                (1, 1)
            }
        };
        agg.total_input_tokens = agg.total_input_tokens.saturating_add(input);
        agg.total_output_tokens = agg.total_output_tokens.saturating_add(output);
        agg.request_count += 1;
        agg.total_tokens_saved = agg.total_tokens_saved.saturating_add(ev.tokens_saved);
        if ev.cache_status.is_some() {
            agg.cache_status = ev.cache_status;
        }
    }
    out
}
