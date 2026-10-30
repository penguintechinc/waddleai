//! [`PipelineContext`] and every type it carries (ruling R1: all owned here).

use std::any::{Any, TypeId};
use std::collections::{BTreeMap, HashMap};

use bytes::Bytes;
use futures_core::stream::BoxStream;
use secrecy::SecretString;
use serde_json::{Map, Value};

use crate::stage::StageError;

/// Provider-credential mode for a request (spec 5.1.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum AuthMode {
    /// WaddleAI-managed (virtual) credentials; the default.
    #[default]
    Virtual,
    /// Caller supplies the provider key, forwarded as-is.
    Passthrough,
}

/// Token usage plus provider-specific extras.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Usage {
    /// Prompt tokens.
    pub input_tokens: u64,
    /// Completion tokens.
    pub output_tokens: u64,
    /// Provider-specific extra counters.
    pub extra: Map<String, Value>,
}

/// A held quota reservation (plain data; the quota crate fills it).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct QuotaReservation {
    /// Reservation id.
    pub id: String,
    /// Tokens reserved.
    pub tokens: u64,
}

/// Owned copy of the context fields quota settlement needs, taken before the
/// settle task is spawned (the task is `'static`, so it cannot borrow the context).
/// Carries model names, counts and text for token counting; never logged by the driver.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AbortSnapshot {
    /// Provider that served (or was serving) the request; "cache" on a cache hit.
    pub provider: Option<String>,
    /// Resolved model.
    pub model: Option<String>,
    /// Model as originally requested.
    pub requested_model: Option<String>,
    /// Whether the response came from cache.
    pub cache_hit: bool,
    /// True when no response text had accumulated.
    pub response_empty: bool,
    /// Accumulated response text (for local token counting when usage is absent).
    pub response_text: String,
    /// Chat messages (for local prompt token counting when usage is absent).
    pub messages: Vec<Value>,
    /// Upstream usage, when reported.
    pub usage: Option<Usage>,
}

impl AbortSnapshot {
    /// Snapshots the settlement-relevant fields of a context.
    pub fn from_ctx(ctx: &PipelineContext) -> Self {
        Self {
            provider: ctx.provider.clone(),
            model: ctx.model.clone(),
            requested_model: ctx.requested_model.clone(),
            cache_hit: ctx.cache_hit,
            response_empty: ctx.response_text.is_empty(),
            response_text: ctx.response_text.clone(),
            messages: ctx.messages.clone(),
            usage: ctx.usage.clone(),
        }
    }

    /// True when upstream tokens were actually consumed and must be CHARGED, not refunded:
    /// a non-empty provider other than "cache", no cache hit, and either response text
    /// or non-zero reported usage. Mirrors Python `settle_aborted`.
    pub fn consumed(&self) -> bool {
        let used = self
            .usage
            .as_ref()
            .is_some_and(|u| u.input_tokens > 0 || u.output_tokens > 0);
        self.provider
            .as_deref()
            .is_some_and(|p| !p.is_empty() && p != "cache")
            && !self.cache_hit
            && (!self.response_empty || used)
    }
}

/// Owned handle on a held quota reservation. The driver `take()`s it from the
/// context and settles it exactly once, by value, inside a cancellation shield.
/// There is deliberately no default that refunds: the abort path must decide.
#[async_trait::async_trait]
pub trait QuotaGuard: Send {
    /// Zero-usage refund of the whole reservation. NOT called by the driver on the
    /// abort path; settlement goes through [`QuotaGuard::settle_aborted`].
    async fn release(self: Box<Self>) -> Result<(), StageError>;
    /// Settles a reservation when the request ends without a normal finalize (a later
    /// stage blocked or errored). Implementations MUST charge (reconcile) when
    /// `snap.consumed()` and refund only zero-usage requests; a blind refund of
    /// consumed spend is a quota bypass. The driver always calls this method.
    async fn settle_aborted(self: Box<Self>, snap: AbortSnapshot) -> Result<(), StageError>;
}

/// Wire format of the response; part of the response-cache key.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ResponseFormat {
    /// OpenAI chat-completions shape.
    #[default]
    Openai,
    /// Anthropic messages shape.
    Anthropic,
}

/// Outcome of the response-cache lookup.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CacheStatus {
    /// Exact-match hit.
    Exact,
    /// Semantic-similarity hit.
    Semantic,
    /// No hit.
    #[default]
    Miss,
}

/// Commits a dispatched response into the response cache. Implemented by the cache crate and
/// invoked by the caller only after the whole pipeline (incl. output filtering) passed clean.
#[async_trait::async_trait]
pub trait WriteBackSink: Send + Sync {
    /// Stores `response` (client-facing body) and its `usage`; never fails the request.
    async fn commit(&self, response: &Value, usage: &Value);
}

/// A response pending write into the response cache after dispatch.
#[derive(Clone, Default)]
pub struct WriteBack {
    /// Cache key.
    pub key: String,
    /// Time-to-live in seconds, when bounded.
    pub ttl_secs: Option<u64>,
    /// Commit handle; `None` for a data-only write-back.
    pub sink: Option<std::sync::Arc<dyn WriteBackSink>>,
}

impl WriteBack {
    /// Commits through the sink (no-op without one). Callers gate this on a clean pipeline.
    pub async fn commit(&self, response: &Value, usage: &Value) {
        if let Some(sink) = &self.sink {
            sink.commit(response, usage).await;
        }
    }
}

impl std::fmt::Debug for WriteBack {
    /// Key and ttl only; the sink is opaque.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WriteBack")
            .field("key", &self.key)
            .field("ttl_secs", &self.ttl_secs)
            .field("has_sink", &self.sink.is_some())
            .finish()
    }
}

impl PartialEq for WriteBack {
    /// Equality over the data fields; sinks are identity-less trait objects.
    fn eq(&self, other: &Self) -> bool {
        self.key == other.key && self.ttl_secs == other.ttl_secs
    }
}

/// Request-scoped placeholder-to-original map (P3 seam; empty in P2).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PseudonymMap(BTreeMap<String, String>);

impl PseudonymMap {
    /// Records a placeholder and the value it stands for.
    pub fn insert(&mut self, placeholder: String, original: String) {
        self.0.insert(placeholder, original);
    }
    /// The original for a placeholder.
    pub fn get(&self, placeholder: &str) -> Option<&str> {
        self.0.get(placeholder).map(String::as_str)
    }
    /// Iterates (placeholder, original) pairs.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v.as_str()))
    }
    /// True when no placeholders exist.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
    /// Number of placeholders.
    pub fn len(&self) -> usize {
        self.0.len()
    }
}

/// Typed map for P4 plugin-hook data; one value per concrete type.
#[derive(Default)]
pub struct Extensions(HashMap<TypeId, Box<dyn Any + Send + Sync>>);

impl Extensions {
    /// Stores a value, returning the previous one of the same type.
    pub fn insert<T: Any + Send + Sync>(&mut self, value: T) -> Option<T> {
        self.0
            .insert(TypeId::of::<T>(), Box::new(value))
            .and_then(|b| b.downcast::<T>().ok().map(|b| *b))
    }
    /// Borrows the stored value of type `T`.
    pub fn get<T: Any + Send + Sync>(&self) -> Option<&T> {
        self.0
            .get(&TypeId::of::<T>())
            .and_then(|b| b.downcast_ref())
    }
    /// Mutably borrows the stored value of type `T`.
    pub fn get_mut<T: Any + Send + Sync>(&mut self) -> Option<&mut T> {
        self.0
            .get_mut(&TypeId::of::<T>())
            .and_then(|b| b.downcast_mut())
    }
    /// Removes and returns the stored value of type `T`.
    pub fn remove<T: Any + Send + Sync>(&mut self) -> Option<T> {
        self.0
            .remove(&TypeId::of::<T>())
            .and_then(|b| b.downcast::<T>().ok().map(|b| *b))
    }
}

/// Per-request state threaded through every stage; a field-for-field port of the
/// Python `PipelineContext` plus the P3/P4 seams.
pub struct PipelineContext {
    /// Authenticated principal, once auth has run.
    pub user: Option<auth::UserContext>,
    /// Virtual (managed) vs passthrough provider credentials.
    pub auth_mode: AuthMode,
    /// Caller's provider key to forward (P3); never logged.
    pub passthrough_credential: Option<SecretString>,
    /// Raw request body.
    pub body: Value,
    /// Resolved model.
    pub model: Option<String>,
    /// Chat messages.
    pub messages: Vec<Value>,
    /// Flattened prompt text.
    pub prompt_text: String,
    /// Accumulated response text.
    pub response_text: String,
    /// Token usage, once known.
    pub usage: Option<Usage>,
    /// True once a stage has short-circuited the request.
    pub blocked: bool,
    /// Why the request was blocked.
    pub block_reason: Option<String>,
    /// HTTP status to return (default 200).
    pub status_code: u16,
    /// Whether the client asked for streaming.
    pub stream: bool,
    /// Ordered "ran:"/"skipped:"/"short-circuit:" entries per stage.
    pub stage_log: Vec<String>,
    /// Provider that served the request.
    pub provider: Option<String>,
    /// Model as originally requested (before routing).
    pub requested_model: Option<String>,
    /// Upstream finish reason.
    pub finish_reason: Option<String>,
    /// Token-budget reservation id (TokenBudget -> Meter).
    pub reservation_id: Option<String>,
    /// Held quota reservation data.
    pub quota_reservation: Option<QuotaReservation>,
    /// Settle-once handle replacing the Python release closure.
    pub quota_release: Option<Box<dyn QuotaGuard>>,
    /// Response wire format.
    pub response_format: ResponseFormat,
    /// Whether the response came from cache.
    pub cache_hit: bool,
    /// Cache outcome.
    pub cache_status: CacheStatus,
    /// Tokens avoided by a cache hit.
    pub tokens_saved: u64,
    /// Response to write into the cache after dispatch.
    pub cache_write_back: Option<WriteBack>,
    /// Preferred backend hint.
    pub preferred_backend: Option<String>,
    /// Synthetic SSE replay stream (streaming cache hit).
    pub stream_iter: Option<BoxStream<'static, Result<Bytes, std::io::Error>>>,
    /// Session id.
    pub session_id: Option<String>,
    /// Extra usage metadata.
    pub usage_meta: Map<String, Value>,
    /// Whether PII was detected in the request.
    pub pii_detected: bool,
    /// Ordered fallback backends.
    pub fallback_chain: Vec<String>,
    /// Routing provenance when the model was rerouted.
    pub routed_from: Option<Value>,
    /// Explicit tool-type hint from the caller.
    pub explicit_tool_type_hint: Option<String>,
    /// Escalation hint.
    pub escalate_hint: Option<String>,
    /// True when security scanning ran in a degraded mode.
    pub security_degraded: bool,
    /// Upstream mapping id.
    pub upstream_mapping_id: Option<String>,
    /// P3 seam: placeholder map, request-scoped, empty in P2.
    pub pseudonym_map: PseudonymMap,
    /// P4 seam: typed plugin-hook data.
    pub extensions: Extensions,
}

impl std::fmt::Debug for PipelineContext {
    /// Redacting summary: never prints the passthrough credential, body, or prompt text.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PipelineContext")
            .field("auth_mode", &self.auth_mode)
            .field("has_user", &self.user.is_some())
            .field(
                "passthrough_credential",
                &self.passthrough_credential.as_ref().map(|_| "<redacted>"),
            )
            .field("blocked", &self.blocked)
            .field("status_code", &self.status_code)
            .field("block_reason", &self.block_reason)
            .field("model", &self.model)
            .field("stage_log", &self.stage_log)
            .finish_non_exhaustive()
    }
}

impl PipelineContext {
    /// Fresh context for a request body with Python-matching defaults (status 200, not blocked).
    pub fn new(body: Value) -> Self {
        Self {
            user: None,
            auth_mode: AuthMode::default(),
            passthrough_credential: None,
            body,
            model: None,
            messages: Vec::new(),
            prompt_text: String::new(),
            response_text: String::new(),
            usage: None,
            blocked: false,
            block_reason: None,
            status_code: 200,
            stream: false,
            stage_log: Vec::new(),
            provider: None,
            requested_model: None,
            finish_reason: None,
            reservation_id: None,
            quota_reservation: None,
            quota_release: None,
            response_format: ResponseFormat::default(),
            cache_hit: false,
            cache_status: CacheStatus::default(),
            tokens_saved: 0,
            cache_write_back: None,
            preferred_backend: None,
            stream_iter: None,
            session_id: None,
            usage_meta: Map::new(),
            pii_detected: false,
            fallback_chain: Vec::new(),
            routed_from: None,
            explicit_tool_type_hint: None,
            escalate_hint: None,
            security_degraded: false,
            upstream_mapping_id: None,
            pseudonym_map: PseudonymMap::default(),
            extensions: Extensions::default(),
        }
    }
}

impl Default for PipelineContext {
    fn default() -> Self {
        Self::new(Value::Null)
    }
}
