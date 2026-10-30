//! WaddleAI response cache (spec section 6): an exact Valkey layer and a restricted semantic
//! layer behind one [`ResponseCache`] facade, cache-stampede protection, synthetic SSE replay of
//! cached responses, and the [`CacheStage`] pipeline stage.
//!
//! Port of `shared/cache/*.py`. The exact layer shares Valkey with the live Python proxy during
//! cutover, so key derivation (`cache::derive_exact_key`), entry layout and the LRU/quota
//! bookkeeping are byte-compatible in both directions. Cache faults never fail a request: every
//! degradation is logged and counted (`waddleai_cache_lookups_total{result="bypass"}`).

mod affinity;
mod config;
mod embed;
mod error;
mod exact;
mod metrics;
mod replay;
mod response_cache;
mod semantic;
mod singleflight;
mod stage;

pub use affinity::{AffinityStore, ValkeyAffinity};
pub use config::{
    CacheConfigResolver, CacheConfigRow, ConfigRowStore, ConfigSource, ResolvedCacheConfig,
    SeaConfigStore,
};
pub use embed::{
    embedding_endpoint_label, resolve_embedding_url, EmbedError, Embedder, EmbeddingClient,
    DEFAULT_EMBEDDING_MODEL,
};
pub use error::StoreError;
pub use exact::{
    is_cacheable_response, is_exact_eligible, CachedResponse, ExactStore, PutLimits, ValkeyExact,
};
pub use metrics::CacheMetrics;
pub use replay::{replay_anthropic_sse, replay_openai_sse};
pub use response_cache::{LookupResult, ResponseCache};
pub use semantic::{
    default_classify_intent, is_semantic_eligible, CtxFlags, PgSemanticStore, SemanticCache,
    SemanticEntry, SemanticStore,
};
pub use singleflight::{
    jittered_ttl, FlightGuard, InProcessSingleFlight, LeaseStore, SingleFlightConfig, ValkeyLease,
    DISABLE_SINGLEFLIGHT_FLAG,
};
pub use stage::{commit_write_back, CacheStage, RESPONSE_CACHE_FLAG};
