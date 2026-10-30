//! WaddleAI token accounting for the Rust proxy.
//!
//! Byte-for-byte ports of the Python `TokenManager` counting/conversion paths, the
//! `estimate_io_tokens` heuristic, the Valkey-Lua `TokenLimiter`, and the
//! `TokenBudgetStage` that reserves budget before dispatch. Parity is gated by a corpus
//! generated from the real Python code (`scripts/gen-token-parity-corpus.py`).

mod convert;
mod count;
mod estimate;
mod limiter;
mod stage;

pub use convert::{
    calculate_waddleai_tokens, ConversionRate, ConversionRates, RateRow, DEFAULT_RATE_ROWS,
};
pub use count::{
    count_path, count_tokens, count_tokens_native, encoding_for, CountError, CountPath, Encoding,
};
pub use estimate::estimate_io_tokens;
pub use limiter::{
    BudgetGate, GateDecision, TokenLimiter, LUA_RECONCILE, LUA_RESERVE, NATIVE_RATE_LIMIT_FLAG,
};
pub use pipeline::{KeyLimits, VkeyBudget};
pub use stage::TokenBudgetStage;
