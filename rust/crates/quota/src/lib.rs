//! WaddleAI pooled-quota engine for the Rust proxy.
//!
//! The Lua scripts are the Python constants exported verbatim (`scripts/export-quota-lua.py`)
//! and drift-tested; keys come from `cache::keys::quota`, so Rust and Python read and write the
//! same Valkey counters. ONE authoritative writer per tenant (spec 11): Rust never
//! double-reserves a tenant that Python serves -- that is a routing-level invariant.

mod config;
mod credits;
mod engine;
mod guard;
mod lease;
mod stage;
mod telemetry;
mod types;

pub use config::{
    org_config_from_json, org_config_to_json, team_config_from_json, team_config_to_json,
    ConfigError, ConfigLoader, ConfigStore, ValkeyConfigStore,
};
pub use credits::{CreditError, CreditModel, TokenCredits};
pub use engine::{
    Ledger, LedgerEntry, LedgerError, LedgerScope, QuotaBackend, QuotaEngine, QuotaError,
    POOLED_QUOTA_FLAG,
};
pub use guard::QuotaGuard;
pub use lease::{
    Clock, QuotaLimiter, ReserveRequest, COUNTER_TTL_S, LUA_RECONCILE, LUA_RESERVE,
    OVERRIDE_PREFIX, RECORD_TTL_S, RESERVATION_TTL_S,
};
pub use stage::QuotaStage;
pub use telemetry::QuotaTelemetry;
pub use types::{
    OrgQuotaConfig, Outcome, OverrideRule, QuotaDecision, ReconcileResult, Reservation,
    TeamQuotaConfig, UserReservation, REASON_DAILY, REASON_EXHAUSTED, REASON_OVERRIDE,
};
