//! WaddleAI proxy pipeline foundation.
//!
//! Owns the [`Stage`] trait, the [`PipelineContext`] and every type it carries,
//! the [`ProxyPipeline`] driver (a port of Python `_run_stage`), and the P3/P4
//! seam traits. Downstream crates depend on this one-way and implement the traits.

mod context;
mod driver;
mod flags;
mod seams;
mod stage;

pub use context::{
    AbortSnapshot, AuthMode, CacheStatus, Extensions, PipelineContext, PseudonymMap, QuotaGuard,
    QuotaReservation, ResponseFormat, Usage, WriteBack,
};
pub use driver::ProxyPipeline;
pub use flags::{FlagResolver, PosthogFlagResolver, StaticFlagResolver};
pub use seams::{
    ChunkTransform, Connector, ConnectorError, ConnectorRequest, ConnectorResponse, Credential,
    Depseudonymizer, DispatchError, Dispatcher, EntityKind, EntitySpan, IdentityDepseudonymizer,
    NerClient, NerError, NerRequest, NoNer, StreamChunk,
};
pub use stage::{Stage, StageError};
