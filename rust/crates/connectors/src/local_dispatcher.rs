//! `LocalDispatcher`: dispatches to the local connectors with retryable-only failover over
//! `ctx.fallback_chain` (port of `_prepare_dispatch` / `_record_dispatch_error`).

use std::sync::Arc;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use futures_core::stream::BoxStream;
use futures_util::StreamExt;
use opentelemetry::metrics::{Counter, Histogram};
use opentelemetry::{global, KeyValue};
use pipeline::{
    ConnectorError, ConnectorRequest, Depseudonymizer, DispatchError, Dispatcher, PipelineContext,
    StreamChunk, Usage,
};
use routing::{split_provider_prefix, RoutingMeta};
use serde_json::{json, Value};

use crate::dispatch::record_dispatch_error;
use crate::registry::ConnectorRegistry;

/// Providers this dispatcher can reach; everything else is P3 (commercial).
pub(crate) const LOCAL_PROVIDERS: [&str; 2] = ["ollama", "llamacpp"];

/// Tunables for [`LocalDispatcher`].
#[derive(Debug, Clone)]
pub struct DispatchConfig {
    /// Provider used when neither the model prefix nor the routing hint names a local one.
    pub default_provider: &'static str,
    /// Upper bound on waiting out a 429 `Retry-After` before trying the next chain entry.
    pub max_failover_backoff: Duration,
}

impl Default for DispatchConfig {
    fn default() -> Self {
        Self {
            default_provider: "ollama",
            max_failover_backoff: Duration::from_secs(2),
        }
    }
}

/// One provider/model attempt in the failover order.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Candidate {
    provider: String,
    model: String,
}

/// Local-runtime dispatcher: primary model first, then the executable fallback chain.
pub struct LocalDispatcher {
    registry: Arc<ConnectorRegistry>,
    depseudo: Arc<dyn Depseudonymizer>,
    cfg: DispatchConfig,
    latency: Histogram<f64>,
    failovers: Counter<u64>,
}

impl LocalDispatcher {
    /// Dispatcher over `registry`; `depseudo` restores response text (identity in P2).
    pub fn new(
        registry: Arc<ConnectorRegistry>,
        depseudo: Arc<dyn Depseudonymizer>,
        cfg: DispatchConfig,
    ) -> Self {
        let meter = global::meter("waddleai-connectors");
        Self {
            registry,
            depseudo,
            cfg,
            latency: meter
                .f64_histogram("llm_request_duration_seconds")
                .with_unit("s")
                .with_description("Upstream LLM call duration")
                .build(),
            failovers: meter
                .u64_counter("dispatch_failover_total")
                .with_unit("1")
                .with_description("Failovers to the next fallback-chain entry")
                .build(),
        }
    }

    /// Failover order: the chosen model, then the chain (the routing-vetted executable subset
    /// when routing ran). Non-local-prefixed chain entries are never attempted.
    fn candidates(&self, ctx: &PipelineContext) -> Vec<Candidate> {
        let meta = ctx.extensions.get::<RoutingMeta>();
        let hint = meta.and_then(|m| m.provider_hint.as_deref());
        let mut out = Vec::new();
        if let Some(model) = ctx.model.as_deref() {
            out.push(self.resolve(model, hint));
        }
        let chain = meta.map_or(&ctx.fallback_chain, |m| &m.executable_fallbacks);
        for m in chain {
            let (prefix, _) = split_provider_prefix(m);
            if prefix.is_none_or(|p| LOCAL_PROVIDERS.contains(&p)) {
                out.push(self.resolve(m, None));
            }
        }
        out
    }

    /// Provider + bare model for `model`. An explicit local prefix wins; the routing hint is
    /// advisory (used only if it names a registered local provider); else the default.
    fn resolve(&self, model: &str, hint: Option<&str>) -> Candidate {
        let (prefix, bare) = split_provider_prefix(model);
        let provider = prefix
            .or_else(|| hint.filter(|h| LOCAL_PROVIDERS.contains(h) && self.registry.contains(h)))
            .unwrap_or(self.cfg.default_provider);
        Candidate {
            provider: provider.to_owned(),
            model: bare.to_owned(),
        }
    }

    /// Connector request for a candidate; local calls never carry a credential.
    fn request_for(ctx: &PipelineContext, cand: &Candidate) -> ConnectorRequest {
        let mut params = serde_json::Map::new();
        if let Some(n) = ctx.body.get("max_tokens").and_then(Value::as_i64) {
            if n > 0 {
                params.insert("max_tokens".to_owned(), json!(n));
            }
        }
        ConnectorRequest {
            model: cand.model.clone(),
            messages: ctx.messages.clone(),
            params: Value::Object(params),
            credential: None,
        }
    }

    /// Records the attempt on the context before the call, so an aborted request settles
    /// against the right provider. `requested_model` is only filled when routing did not.
    fn begin_attempt(ctx: &mut PipelineContext, original: &Option<String>, cand: &Candidate) {
        if ctx.requested_model.is_none() {
            ctx.requested_model.clone_from(original);
        }
        ctx.provider = Some(cand.provider.clone());
        ctx.model = Some(cand.model.clone());
    }

    /// Waits out a 429 hint (capped) before failing over, and counts the failover.
    async fn failover_pause(&self, cand: &Candidate, err: &ConnectorError) {
        self.failovers
            .add(1, &[KeyValue::new("provider", cand.provider.clone())]);
        if let ConnectorError::Status {
            retry_after: Some(d),
            ..
        } = err
        {
            tokio::time::sleep((*d).min(self.cfg.max_failover_backoff)).await;
        }
    }

    /// Records upstream latency by provider and outcome.
    fn observe(&self, provider: &str, ok: bool, start: Instant) {
        self.latency.record(
            start.elapsed().as_secs_f64(),
            &[
                KeyValue::new("provider", provider.to_owned()),
                KeyValue::new("status", if ok { "success" } else { "error" }),
            ],
        );
    }
}

/// Maps a connector failure to the client-facing status/reason and retryability, mirroring
/// `_record_dispatch_error`: 4xx pass through unretried; 429, 5xx, timeouts and transport
/// faults are retryable (502/504 to the caller). Never carries the upstream body.
pub(crate) fn map_connector_error(e: &ConnectorError) -> DispatchError {
    match e {
        ConnectorError::Status { status, .. } if *status == 429 => DispatchError::Upstream {
            status: 429,
            reason: "provider_error_429".to_owned(),
            retryable: true,
        },
        ConnectorError::Status { status, .. } if (400..500).contains(status) => {
            DispatchError::Upstream {
                status: *status,
                reason: format!("provider_error_{status}"),
                retryable: false,
            }
        }
        ConnectorError::Status { status, .. } => DispatchError::Upstream {
            status: 502,
            reason: format!("provider_error_{status}"),
            retryable: true,
        },
        ConnectorError::Timeout => DispatchError::Upstream {
            status: 504,
            reason: "provider_error_504".to_owned(),
            retryable: true,
        },
        ConnectorError::Upstream(_) => DispatchError::Upstream {
            status: 502,
            reason: "provider_error".to_owned(),
            retryable: true,
        },
        ConnectorError::Unsupported(_) => DispatchError::Upstream {
            status: 501,
            reason: "provider_unsupported".to_owned(),
            retryable: false,
        },
    }
}

/// The 400 for a request with nothing to send.
fn no_messages() -> DispatchError {
    DispatchError::Upstream {
        status: 400,
        reason: "no_messages".to_owned(),
        retryable: false,
    }
}

/// The 503 when no candidate exists at all (no model resolved).
fn no_providers() -> DispatchError {
    DispatchError::Upstream {
        status: 503,
        reason: "no_available_providers".to_owned(),
        retryable: false,
    }
}

/// Usage for the context from a non-streaming connector reply (None when the runtime
/// reported no counts, so quota falls back to its local estimate).
fn usage_from(input: u64, output: u64) -> Option<Usage> {
    if input == 0 && output == 0 {
        return None;
    }
    let mut u = Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    };
    u.extra
        .insert("total_tokens".to_owned(), json!(input + output));
    Some(u)
}

#[async_trait]
impl Dispatcher for LocalDispatcher {
    /// Non-streaming dispatch with failover; fills response/usage/provider/model/finish_reason.
    /// On error the caller records it (see [`record_dispatch_error`]).
    async fn dispatch(&self, ctx: &mut PipelineContext) -> Result<(), DispatchError> {
        if ctx.messages.is_empty() {
            return Err(no_messages());
        }
        let original = ctx.model.clone();
        let cands = self.candidates(ctx);
        let mut last = no_providers();
        for (i, cand) in cands.iter().enumerate() {
            Self::begin_attempt(ctx, &original, cand);
            let Some(conn) = self.registry.get(&cand.provider) else {
                tracing::error!(provider = %cand.provider, "dispatch: no connector for provider");
                last = DispatchError::NoConnector(cand.provider.clone());
                continue;
            };
            let req = Self::request_for(ctx, cand);
            let start = Instant::now();
            match conn.chat(&req).await {
                Ok(resp) => {
                    self.observe(&cand.provider, true, start);
                    ctx.response_text = self.depseudo.restore_text(&ctx.pseudonym_map, &resp.text);
                    ctx.usage = usage_from(resp.input_tokens, resp.output_tokens);
                    ctx.finish_reason =
                        Some(resp.finish_reason.unwrap_or_else(|| "stop".to_owned()));
                    return Ok(());
                }
                Err(e) => {
                    self.observe(&cand.provider, false, start);
                    let mapped = map_connector_error(&e);
                    tracing::warn!(provider = %cand.provider, error = %mapped, "dispatch: upstream error");
                    if !mapped.is_retryable() {
                        return Err(mapped);
                    }
                    last = mapped;
                    if i + 1 < cands.len() {
                        self.failover_pause(cand, &e).await;
                    }
                }
            }
        }
        Err(last)
    }

    /// True streaming. Accumulates `ctx.response_text` as chunks are yielded (so a client
    /// disconnect leaves the partial text for settlement and drops the upstream request),
    /// fills usage/finish_reason on completion, and fails over only before the first byte.
    /// Errors are recorded on the context (like the Python generator) AND yielded last.
    fn stream_dispatch<'a>(
        &'a self,
        ctx: &'a mut PipelineContext,
    ) -> BoxStream<'a, Result<StreamChunk, DispatchError>> {
        Box::pin(async_stream::stream! {
            if ctx.messages.is_empty() {
                let e = no_messages();
                record_dispatch_error(ctx, &e);
                yield Err(e);
                return;
            }
            let original = ctx.model.clone();
            let cands = self.candidates(ctx);
            let mut transform = self.depseudo.restore_stream(&ctx.pseudonym_map);
            let mut last = no_providers();
            ctx.response_text.clear();
            'cands: for (i, cand) in cands.iter().enumerate() {
                Self::begin_attempt(ctx, &original, cand);
                let Some(conn) = self.registry.get(&cand.provider) else {
                    tracing::error!(provider = %cand.provider, "dispatch: no connector for provider");
                    last = DispatchError::NoConnector(cand.provider.clone());
                    continue;
                };
                let req = Self::request_for(ctx, cand);
                let start = Instant::now();
                let mut upstream = conn.chat_stream(&req);
                let mut yielded = false;
                let mut usage = None;
                let mut finish = None;
                while let Some(item) = upstream.next().await {
                    match item {
                        Ok(chunk) => {
                            if !chunk.delta.is_empty() {
                                yielded = true;
                                ctx.response_text.push_str(&chunk.delta);
                                let out = transform.push(&chunk.delta);
                                if !out.is_empty() {
                                    yield Ok(StreamChunk { delta: out, ..StreamChunk::default() });
                                }
                            }
                            if chunk.usage.is_some() {
                                usage = chunk.usage;
                            }
                            if chunk.finish_reason.is_some() {
                                finish = chunk.finish_reason;
                            }
                        }
                        Err(e) => {
                            self.observe(&cand.provider, false, start);
                            let mapped = map_connector_error(&e);
                            tracing::warn!(provider = %cand.provider, error = %mapped, "dispatch: upstream stream error");
                            if !yielded && mapped.is_retryable() && i + 1 < cands.len() {
                                last = mapped;
                                self.failover_pause(cand, &e).await;
                                continue 'cands;
                            }
                            record_dispatch_error(ctx, &mapped);
                            yield Err(mapped);
                            return;
                        }
                    }
                }
                self.observe(&cand.provider, true, start);
                let tail = transform.finish();
                if !tail.is_empty() {
                    yield Ok(StreamChunk { delta: tail, ..StreamChunk::default() });
                }
                let raw = std::mem::take(&mut ctx.response_text);
                ctx.response_text = self.depseudo.restore_text(&ctx.pseudonym_map, &raw);
                let finish = finish.unwrap_or_else(|| "stop".to_owned());
                ctx.finish_reason = Some(finish.clone());
                let usage = usage.filter(|u: &Usage| u.input_tokens > 0 || u.output_tokens > 0);
                ctx.usage = usage.clone();
                yield Ok(StreamChunk { delta: String::new(), finish_reason: Some(finish), usage });
                return;
            }
            record_dispatch_error(ctx, &last);
            yield Err(last);
        })
    }
}
