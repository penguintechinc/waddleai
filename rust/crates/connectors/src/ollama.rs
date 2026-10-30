//! Ollama connector: `/api/chat`, NDJSON streaming (port of `OllamaConnector` in
//! `shared/utils/llm_connectors.py`).

use async_trait::async_trait;
use futures_core::stream::BoxStream;
use futures_util::StreamExt;
use pipeline::{
    Connector, ConnectorError, ConnectorRequest, ConnectorResponse, StreamChunk, Usage,
};
use serde_json::{json, Value};

use crate::http::{build_client, count, post_json, reply_error, response_lines, ConnectorConfig};

/// Provider name registered for this connector.
pub const PROVIDER: &str = "ollama";

/// Local Ollama runtime. Injects no credentials: `ConnectorRequest::credential` is ignored.
pub struct OllamaConnector {
    client: reqwest::Client,
    cfg: ConnectorConfig,
}

impl OllamaConnector {
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
}

/// Builds the `/api/chat` payload; `temperature` defaults to 0.7 and `num_predict` to -1 as in
/// the Python connector, and `think` is only sent when explicitly set.
fn payload(req: &ConnectorRequest, stream: bool) -> Value {
    let temperature = req
        .params
        .get("temperature")
        .filter(|v| v.is_number())
        .cloned()
        .unwrap_or_else(|| json!(0.7));
    let num_predict = req
        .params
        .get("max_tokens")
        .filter(|v| v.is_i64())
        .cloned()
        .unwrap_or_else(|| json!(-1));
    let mut p = json!({
        "model": req.model,
        "messages": req.messages,
        "stream": stream,
        "options": {"temperature": temperature, "num_predict": num_predict},
    });
    if let Some(think) = req.params.get("think").filter(|v| !v.is_null()) {
        p["think"] = think.clone();
    }
    p
}

/// Ollama's usage counters (`prompt_eval_count` -> input, `eval_count` -> output).
fn usage_of(v: &Value) -> Usage {
    let input = count(v, "prompt_eval_count");
    let output = count(v, "eval_count");
    let mut u = Usage {
        input_tokens: input,
        output_tokens: output,
        ..Usage::default()
    };
    u.extra
        .insert("total_tokens".to_owned(), json!(input + output));
    u
}

#[async_trait]
impl Connector for OllamaConnector {
    fn provider(&self) -> &'static str {
        PROVIDER
    }

    /// Non-streaming `/api/chat`; token counts come from Ollama's own counters.
    async fn chat(&self, req: &ConnectorRequest) -> Result<ConnectorResponse, ConnectorError> {
        let resp = post_json(
            &self.client,
            &self.cfg,
            "/api/chat",
            &payload(req, false),
            Some(self.cfg.request_timeout),
        )
        .await?;
        let v: Value = resp.json().await.map_err(|e| reply_error(&e, "ollama"))?;
        let text = v
            .pointer("/message/content")
            .and_then(Value::as_str)
            .ok_or_else(|| ConnectorError::Upstream("malformed ollama reply".to_owned()))?
            .to_owned();
        let u = usage_of(&v);
        Ok(ConnectorResponse {
            text,
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
            finish_reason: Some(
                v.get("done_reason")
                    .and_then(Value::as_str)
                    .unwrap_or("stop")
                    .to_owned(),
            ),
        })
    }

    /// Streaming `/api/chat` (NDJSON). Emits one chunk per content delta and a final chunk with
    /// the finish reason and usage; a stream that ends without `done:true` is an error.
    fn chat_stream(
        &self,
        req: &ConnectorRequest,
    ) -> BoxStream<'static, Result<StreamChunk, ConnectorError>> {
        let client = self.client.clone();
        let cfg = self.cfg.clone();
        let body = payload(req, true);
        Box::pin(async_stream::stream! {
            let resp = match post_json(&client, &cfg, "/api/chat", &body, None).await {
                Ok(r) => r,
                Err(e) => {
                    yield Err(e);
                    return;
                }
            };
            let mut lines = response_lines(resp, cfg.idle_timeout);
            while let Some(line) = lines.next().await {
                let line = match line {
                    Ok(l) => l,
                    Err(e) => {
                        yield Err(e);
                        return;
                    }
                };
                let Ok(v) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if v.get("error").is_some() {
                    yield Err(ConnectorError::Upstream("ollama stream error".to_owned()));
                    return;
                }
                if let Some(c) = v.pointer("/message/content").and_then(Value::as_str) {
                    if !c.is_empty() {
                        yield Ok(StreamChunk::delta(c.to_owned()));
                    }
                }
                if v.get("done").and_then(Value::as_bool) == Some(true) {
                    yield Ok(StreamChunk::finished(
                        Some(v.get("done_reason").and_then(Value::as_str).unwrap_or("stop").to_owned(),),
                        Some(usage_of(&v)),
                    ));
                    return;
                }
            }
            yield Err(ConnectorError::Upstream("ollama stream ended early".to_owned()));
        })
    }
}
