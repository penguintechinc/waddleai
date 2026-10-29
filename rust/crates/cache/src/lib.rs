//! Valkey access for the WaddleAI data plane.
//!
//! The Rust proxy shares Valkey state with the Python proxy during cutover, so
//! [`keys`] emits byte-identical key strings; [`invalidation`] observes the
//! signal the Python side actually produces (key `DEL`).

pub mod client;
pub mod invalidation;
pub mod keys;

pub use client::{connect, Cache, CacheError};
pub use invalidation::{subscribe_invalidations, Invalidation, Invalidations};
