//! P3/P4 seam traits and their support types, plus the P2 impls that fail loud or pass through.

use async_trait::async_trait;
use futures_core::stream::BoxStream;
use serde_json::Value;

use crate::context::{PipelineContext, PseudonymMap};

/// Kinds of PII entity the NER tier can report.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EntityKind {
    /// Person name.
    Person,
    /// Email address.
    Email,
    /// Phone number.
    Phone,
    /// Postal address / location.
    Location,
    /// Organization name.
    Organization,
    /// Any other kind.
    Other,
}

/// Input to a tier-3 NER call. Carries text for analysis only; never logged.
pub struct NerRequest<'a> {
    /// Text to analyze.
    pub text: &'a str,
    /// Entity kinds to look for.
    pub entities: &'a [EntityKind],
    /// Owning org.
    pub org_id: i64,
    /// Content hash for caching/dedup.
    pub content_hash: [u8; 32],
}

/// A detected entity span (byte offsets) with its confidence.
#[derive(Debug, Clone, PartialEq)]
pub struct EntitySpan {
    /// Entity kind.
    pub kind: EntityKind,
    /// Start offset.
    pub start: usize,
    /// End offset (exclusive).
    pub end: usize,
    /// Confidence score.
    pub score: f32,
}

/// NER failure modes; drive the degraded fail-open/closed policy.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum NerError {
    /// No NER backend available.
    #[error("ner unavailable")]
    Unavailable,
    /// The backend timed out.
    #[error("ner timeout")]
    Timeout,
    /// The backend rejected the request.
    #[error("ner rejected: {0}")]
    Rejected(String),
}

/// Tier-3 PII-NER client; returns detected spans only.
#[async_trait]
pub trait NerClient: Send + Sync {
    /// Analyzes text and returns spans; the proxy substitutes and holds the map.
    async fn analyze(&self, req: &NerRequest<'_>) -> Result<Vec<EntitySpan>, NerError>;
}

/// P2 NER impl: always `Unavailable`, which exercises the degraded-policy path.
pub struct NoNer;

#[async_trait]
impl NerClient for NoNer {
    async fn analyze(&self, _req: &NerRequest<'_>) -> Result<Vec<EntitySpan>, NerError> {
        Err(NerError::Unavailable)
    }
}

/// One normalized streaming chunk.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StreamChunk {
    /// Text delta.
    pub delta: String,
    /// Finish reason, on the final chunk.
    pub finish_reason: Option<String>,
}

/// Dispatch-level failure.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DispatchError {
    /// No connector for the provider.
    #[error("no connector: {0}")]
    NoConnector(String),
    /// A connector failed.
    #[error("connector: {0}")]
    Connector(String),
}

/// Upstream dispatcher (local in P2; P3 adds commercial providers).
#[async_trait]
pub trait Dispatcher: Send + Sync {
    /// Non-streaming upstream call; fills ctx.response_text/usage/provider/finish_reason.
    async fn dispatch(&self, ctx: &mut PipelineContext) -> Result<(), DispatchError>;
    /// True streaming; yields normalized chunks, accumulating ctx.response_text/usage on completion.
    fn stream_dispatch<'a>(
        &'a self,
        ctx: &'a mut PipelineContext,
    ) -> BoxStream<'a, Result<StreamChunk, DispatchError>>;
}

/// Provider credential handed to a connector (P3 injects it). Secret; never logged.
pub struct Credential(pub secrecy::SecretString);

/// Request to one provider connector.
pub struct ConnectorRequest {
    /// Target model.
    pub model: String,
    /// Chat messages.
    pub messages: Vec<Value>,
    /// Extra provider params.
    pub params: Value,
    /// None for local providers.
    pub credential: Option<Credential>,
}

/// Non-streaming connector reply.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ConnectorResponse {
    /// Response text.
    pub text: String,
    /// Prompt tokens.
    pub input_tokens: u64,
    /// Completion tokens.
    pub output_tokens: u64,
    /// Finish reason.
    pub finish_reason: Option<String>,
}

/// Connector failure.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConnectorError {
    /// Upstream returned an error or was unreachable.
    #[error("upstream: {0}")]
    Upstream(String),
    /// The connector does not support the request.
    #[error("unsupported: {0}")]
    Unsupported(&'static str),
}

/// One provider connector (P2: ollama, llamacpp).
#[async_trait]
pub trait Connector: Send + Sync {
    /// Provider name.
    fn provider(&self) -> &'static str;
    /// Non-streaming chat.
    async fn chat(&self, req: &ConnectorRequest) -> Result<ConnectorResponse, ConnectorError>;
    /// Streaming chat.
    fn chat_stream(
        &self,
        req: &ConnectorRequest,
    ) -> BoxStream<'static, Result<StreamChunk, ConnectorError>>;
}

/// Stateful per-stream transform (rolling buffer across SSE chunks).
pub trait ChunkTransform: Send {
    /// Transforms one chunk of text, possibly holding bytes back.
    fn push(&mut self, chunk: &str) -> String;
    /// Flushes anything held back at end of stream.
    fn finish(&mut self) -> String;
}

/// Restores pseudonymized placeholders in model output (P3).
pub trait Depseudonymizer: Send + Sync {
    /// Restores placeholders in a complete string.
    fn restore_text(&self, map: &PseudonymMap, s: &str) -> String;
    /// Builds a transform restoring placeholders across streamed chunks.
    fn restore_stream(&self, map: &PseudonymMap) -> Box<dyn ChunkTransform>;
}

/// P2 depseudonymizer: returns text unchanged (the map is empty in P2).
pub struct IdentityDepseudonymizer;

struct IdentityTransform;

impl ChunkTransform for IdentityTransform {
    fn push(&mut self, chunk: &str) -> String {
        chunk.to_owned()
    }
    fn finish(&mut self) -> String {
        String::new()
    }
}

impl Depseudonymizer for IdentityDepseudonymizer {
    fn restore_text(&self, _map: &PseudonymMap, s: &str) -> String {
        s.to_owned()
    }
    fn restore_stream(&self, _map: &PseudonymMap) -> Box<dyn ChunkTransform> {
        Box::new(IdentityTransform)
    }
}
