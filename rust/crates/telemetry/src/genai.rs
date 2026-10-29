//! OpenTelemetry GenAI semantic-convention instruments (`gen_ai.*`) for
//! telemetry that has no Python-proxy Prometheus equivalent. Names follow the
//! OTel GenAI semconv (`gen_ai.client.token.usage`,
//! `gen_ai.client.operation.duration`).

use opentelemetry::metrics::Histogram;
use opentelemetry::{global, KeyValue};

/// `gen_ai.client.token.usage` instrument name.
pub const TOKEN_USAGE: &str = "gen_ai.client.token.usage";
/// `gen_ai.client.operation.duration` instrument name.
pub const OPERATION_DURATION: &str = "gen_ai.client.operation.duration";
/// Attribute: operation name (`chat`, `embeddings`, ...).
pub const ATTR_OPERATION_NAME: &str = "gen_ai.operation.name";
/// Attribute: provider name.
pub const ATTR_PROVIDER_NAME: &str = "gen_ai.provider.name";
/// Attribute: requested model.
pub const ATTR_REQUEST_MODEL: &str = "gen_ai.request.model";
/// Attribute: token type (`input` | `output`).
pub const ATTR_TOKEN_TYPE: &str = "gen_ai.token.type";

/// GenAI client histograms bound to the global meter provider.
pub struct GenAiMetrics {
    token_usage: Histogram<u64>,
    operation_duration: Histogram<f64>,
}

impl Default for GenAiMetrics {
    fn default() -> Self {
        Self::new()
    }
}

impl GenAiMetrics {
    /// Creates the instruments on the global meter (no-op when no exporter).
    pub fn new() -> Self {
        let meter = global::meter("waddleai.genai");
        Self {
            token_usage: meter
                .u64_histogram(TOKEN_USAGE)
                .with_unit("{token}")
                .with_description("Number of input and output tokens used")
                .build(),
            operation_duration: meter
                .f64_histogram(OPERATION_DURATION)
                .with_unit("s")
                .with_description("GenAI operation duration")
                .build(),
        }
    }

    fn attrs(operation: &str, provider: &str, model: &str) -> Vec<KeyValue> {
        vec![
            KeyValue::new(ATTR_OPERATION_NAME, operation.to_owned()),
            KeyValue::new(ATTR_PROVIDER_NAME, provider.to_owned()),
            KeyValue::new(ATTR_REQUEST_MODEL, model.to_owned()),
        ]
    }

    /// Records tokens used; `token_type` is `input` or `output`.
    pub fn record_tokens(
        &self,
        operation: &str,
        provider: &str,
        model: &str,
        token_type: &str,
        tokens: u64,
    ) {
        let mut a = Self::attrs(operation, provider, model);
        a.push(KeyValue::new(ATTR_TOKEN_TYPE, token_type.to_owned()));
        self.token_usage.record(tokens, &a);
    }

    /// Records an operation duration in seconds.
    pub fn record_duration(&self, operation: &str, provider: &str, model: &str, secs: f64) {
        self.operation_duration
            .record(secs, &Self::attrs(operation, provider, model));
    }
}
