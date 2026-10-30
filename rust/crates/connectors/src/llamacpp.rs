//! llama.cpp (llama-server) connector over its OpenAI-compatible API (port of
//! `LlamaCppConnector` in `shared/utils/llm_connectors.py`).

use async_trait::async_trait;
use futures_core::stream::BoxStream;
use futures_util::StreamExt;
use pipeline::{
    Connector, ConnectorError, ConnectorRequest, ConnectorResponse, StreamChunk, Usage,
};
use serde_json::{json, Value};

use crate::http::{build_client, count, post_json, reply_error, response_lines, ConnectorConfig};

/// Provider name registered for this connector.
pub const PROVIDER: &str = "llamacpp";

/// Local llama-server. Injects no per-request credentials; an optional static bearer comes
/// from its own config (llama-server `--api-key`), never from `ConnectorRequest::credential`.
pub struct LlamaCppConnector {
    client: reqwest::Client,
    cfg: ConnectorConfig,
}

impl LlamaCppConnector {
    /// Connector with its own pooled client.
    pub fn new(cfg: ConnectorConfig) -> Result<Self, reqwest::Error> {
        Ok(Self {
            client: build_client(&cfg)?,
            cfg,
        })
    }

    /// Connector sharing an existing pooled client.
    pub fn with_client(client: reqwest::Client, cfg: ConnectorConfig) -> Self {
        Self { client, cfg }
    }

    /// `/v1/chat/completions` payload: caller params first, then the fixed fields so params can
    /// never override `model`/`messages`/`stream`.
    fn payload(&self, req: &ConnectorRequest, stream: bool) -> Value {
        let mut p = req.params.as_object().cloned().unwrap_or_default();
        let model = if req.model.is_empty() {
            self.cfg.default_model.clone().unwrap_or_default()
        } else {
            req.model.clone()
        };
        p.insert("model".to_owned(), json!(model));
        p.insert("messages".to_owned(), json!(req.messages));
        p.insert("stream".to_owned(), json!(stream));
        if stream {
            p.insert("stream_options".to_owned(), json!({"include_usage": true}));
        }
        Value::Object(p)
    }
}

/// OpenAI-style usage object -> [`Usage`].
fn usage_of(u: &Value) -> Usage {
    let input = count(u, "prompt_tokens");
    let output = count(u, "completion_tokens");
    let mut out = Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    };
    out.extra
        .insert("total_tokens".to_owned(), json!(input + output));
    out
}

#[async_trait]
impl Connector for LlamaCppConnector {
    fn provider(&self) -> &'static str {
        PROVIDER
    }

    /// Non-streaming chat completion; empty `choices` is an upstream fault.
    async fn chat(&self, req: &ConnectorRequest) -> Result<ConnectorResponse, ConnectorError> {
        let resp = post_json(
            &self.client,
            &self.cfg,
            "/v1/chat/completions",
            &self.payload(req, false),
            Some(self.cfg.request_timeout),
        )
        .await?;
        let v: Value = resp
            .json()
            .await
            .map_err(|e| reply_error(&e, "llama-server"))?;
        let choice = v
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|c| c.first())
            .ok_or_else(|| {
                ConnectorError::Upstream("llama-server returned empty choices".to_owned())
            })?;
        let text = choice
            .pointer("/message/content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let u = v.get("usage").map(usage_of).unwrap_or_default();
        Ok(ConnectorResponse {
            text,
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
            finish_reason: Some(
                choice
                    .get("finish_reason")
                    .and_then(Value::as_str)
                    .unwrap_or("stop")
                    .to_owned(),
            ),
        })
    }

    /// Streaming chat completion over SSE (`data: {...}` / `data: [DONE]`). A stream that ends
    /// with neither `[DONE]` nor a finish reason is an error.
    fn chat_stream(
        &self,
        req: &ConnectorRequest,
    ) -> BoxStream<'static, Result<StreamChunk, ConnectorError>> {
        let client = self.client.clone();
        let cfg = self.cfg.clone();
        let body = self.payload(req, true);
        Box::pin(async_stream::stream! {
            let resp = match post_json(&client, &cfg, "/v1/chat/completions", &body, None).await {
                Ok(r) => r,
                Err(e) => {
                    yield Err(e);
                    return;
                }
            };
            let mut lines = response_lines(resp, cfg.idle_timeout);
            let mut finish: Option<String> = None;
            let mut usage: Option<Usage> = None;
            let mut complete = false;
            while let Some(line) = lines.next().await {
                let line = match line {
                    Ok(l) => l,
                    Err(e) => {
                        yield Err(e);
                        return;
                    }
                };
                let Some(data) = line.strip_prefix("data:") else {
                    continue;
                };
                let data = data.trim();
                if data == "[DONE]" {
                    complete = true;
                    break;
                }
                let Ok(v) = serde_json::from_str::<Value>(data) else {
                    continue;
                };
                if let Some(u) = v.get("usage").filter(|u| u.is_object()) {
                    usage = Some(usage_of(u));
                }
                let Some(choice) = v.get("choices").and_then(Value::as_array).and_then(|c| c.first()) else {
                    continue;
                };
                if let Some(c) = choice.pointer("/delta/content").and_then(Value::as_str) {
                    if !c.is_empty() {
                        yield Ok(StreamChunk { delta: c.to_owned(), ..StreamChunk::default() });
                    }
                }
                if let Some(f) = choice.get("finish_reason").and_then(Value::as_str) {
                    finish = Some(f.to_owned());
                }
            }
            if !complete && finish.is_none() {
                yield Err(ConnectorError::Upstream("llama-server stream ended early".to_owned()));
                return;
            }
            yield Ok(StreamChunk {
                delta: String::new(),
                finish_reason: Some(finish.unwrap_or_else(|| "stop".to_owned())),
                usage,
            });
        })
    }
}
