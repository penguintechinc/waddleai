//! WaddleAI usage metering for the Rust proxy.
//!
//! - [`MeteringBuffer`] batches [`MeteringEvent`]s and upserts per `(vkey, day)` rows into the
//!   SAME `token_usage` table the Python buffer writes ([`SeaOrmUsageWriter`]).
//! - [`SeaOrmLedger`] is the durable `quota_usage_ledger` writer the quota engine needs
//!   (a separate table from `token_usage`).
//! - [`MeterStage`] is the finalize step: record usage, reconcile the token budget, reconcile
//!   the pooled quota exactly once.

mod buffer;
mod event;
mod ledger;
mod stage;
mod writer;

pub use buffer::{BufferConfig, MeteringBuffer};
pub use event::{
    aggregate_events, aggregate_key_events, Aggregate, EventUsage, KeyAggregate, KeyUsageEvent,
    MeteringEvent,
};
pub use ledger::SeaOrmLedger;
pub use stage::{KnownModels, MeterStage};
pub use writer::{SeaOrmUsageWriter, UsageWriter, WriteError, SOURCE};
