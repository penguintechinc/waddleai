//! Prometheus registry mirroring the Python proxy's metric names, labels,
//! help text and histogram buckets exactly (`shared/utils/metrics.py`,
//! `shared/security/content_filter.py`, `shared/utils/token_limiter.py`).
//!
//! Dashboards and the T8 contract harness key off these names; change them
//! only together with the Python side.

use std::collections::{BTreeSet, HashMap};

use prometheus::{CounterVec, GaugeVec, HistogramOpts, HistogramVec, Opts, Registry};

use crate::error::TelemetryError;

/// `prometheus_client` default histogram buckets (differs from the Rust crate's).
const PY_DEFAULT: &[f64] = &[
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
];
const AUTH_LOOKUP: &[f64] = &[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5];
const LLM_REQ: &[f64] = &[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0];
const EMBED: &[f64] = &[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0];
const STREAM_TTFB: &[f64] = &[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0];
const HOOK_EVAL: &[f64] = &[
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
];
const AUDITOR: &[f64] = &[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0];

/// Collector kind for a [`MetricSpec`].
#[derive(Clone, Copy, Debug)]
pub enum Kind {
    /// Monotonic counter.
    Counter,
    /// Settable gauge.
    Gauge,
    /// Histogram with explicit upper bounds.
    Histogram(&'static [f64]),
}

/// One Python-parity metric definition.
#[derive(Clone, Copy, Debug)]
pub struct MetricSpec {
    /// Exposed metric name (identical to Python).
    pub name: &'static str,
    /// HELP text (identical to Python).
    pub help: &'static str,
    /// Label names, in Python order.
    pub labels: &'static [&'static str],
    /// Collector kind.
    pub kind: Kind,
}

const fn m(
    name: &'static str,
    help: &'static str,
    labels: &'static [&'static str],
    kind: Kind,
) -> MetricSpec {
    MetricSpec {
        name,
        help,
        labels,
        kind,
    }
}

/// Every metric the Python proxy exposes (excluding `waddleai_management_*`,
/// which belong to the management service only).
pub const SPECS: &[MetricSpec] = &[
    m("waddleai_requests_total", "Total number of requests", &["service", "endpoint", "method", "status_code"], Kind::Counter),
    m("waddleai_request_duration_seconds", "Request duration in seconds", &["service", "endpoint", "method"], Kind::Histogram(PY_DEFAULT)),
    m("waddleai_llm_requests_total", "Total LLM requests by provider and model", &["provider", "model", "status"], Kind::Counter),
    m("waddleai_llm_tokens_total", "Total LLM tokens processed", &["provider", "model", "token_type"], Kind::Counter),
    m("waddleai_normalized_tokens_total", "Total WaddleAI normalized tokens", &["organization", "user", "provider"], Kind::Counter),
    m("waddleai_security_events_total", "Total security events detected", &["event_type", "severity", "action"], Kind::Counter),
    m("waddleai_database_operations_total", "Total database operations", &["operation", "table", "status"], Kind::Counter),
    m("waddleai_database_operation_duration_seconds", "Database operation duration", &["operation", "table"], Kind::Histogram(PY_DEFAULT)),
    m("waddleai_active_connections", "Number of active connections", &["service", "connection_type"], Kind::Gauge),
    m("waddleai_auth_attempts_total", "Total authentication attempts", &["auth_type", "status"], Kind::Counter),
    m("waddleai_auth_lookup_duration_seconds", "API-key auth lookup latency (cache hit or DB fallback), by result", &["result"], Kind::Histogram(AUTH_LOOKUP)),
    m("waddleai_provider_health", "Provider health status (1=healthy, 0=unhealthy)", &["provider", "endpoint"], Kind::Gauge),
    m("waddleai_token_quota_usage", "Token quota usage percentage", &["organization", "user"], Kind::Gauge),
    m("waddleai_llm_request_duration_seconds", "Upstream LLM/provider call duration in seconds", &["provider", "model", "status"], Kind::Histogram(LLM_REQ)),
    m("waddleai_embedding_call_duration_seconds", "Embedding call duration in seconds, by target endpoint and outcome", &["endpoint", "outcome"], Kind::Histogram(EMBED)),
    m("waddleai_embedding_calls_total", "Embedding calls, by target endpoint and outcome", &["endpoint", "outcome"], Kind::Counter),
    m("waddleai_proxy_inflight_requests", "Current in-flight requests held by this worker's ConcurrencyLimiter", &["service"], Kind::Gauge),
    m("waddleai_proxy_concurrency_rejections_total", "Requests shed with 429 by the proxy ConcurrencyLimiter", &["endpoint"], Kind::Counter),
    m("waddleai_rate_limit_exceeded_total", "Rate limit exceeded events", &["endpoint", "limit_type"], Kind::Counter),
    m("waddleai_cache_lookups_total", "Cache lookups by layer and result (result: hit|miss|bypass|stampede_wait|stampede_fallthrough)", &["layer", "result"], Kind::Counter),
    m("waddleai_cache_lookup_duration_seconds", "Cache lookup latency by layer, including any single-flight wait", &["layer"], Kind::Histogram(PY_DEFAULT)),
    m("waddleai_cache_tokens_saved_total", "Tokens saved by cache layer", &["layer"], Kind::Counter),
    m("waddleai_cache_entries_evicted_total", "Cache entries evicted (LRU/quota)", &["layer"], Kind::Counter),
    m("waddleai_proxy_stream_ttfb_seconds", "Time to first upstream chunk for a true-SSE streaming dispatch", &["provider", "endpoint"], Kind::Histogram(STREAM_TTFB)),
    m("waddleai_proxy_stream_chunks_total", "SSE content chunks forwarded to the client", &["provider", "endpoint"], Kind::Counter),
    m("waddleai_proxy_stream_errors_total", "Streaming dispatch failures (pre-stream or mid-stream)", &["provider", "endpoint"], Kind::Counter),
    m("waddleai_proxy_usage_estimated_total", "Responses metered from a token ESTIMATE because the provider omitted usage", &["provider"], Kind::Counter),
    m("waddleai_hook_invocations_total", "Agent-hook evaluations by ecosystem/event/decision", &["ecosystem", "event", "decision"], Kind::Counter),
    m("waddleai_hook_evaluation_duration_seconds", "Server-side agent-hook evaluation latency (§18 evaluate endpoint)", &["ecosystem", "event"], Kind::Histogram(HOOK_EVAL)),
    m("waddleai_hook_timeouts_total", "Agent-hook Tier-2 evaluation timeouts", &["tier"], Kind::Counter),
    m("waddleai_hook_fail_mode_total", "Agent-hook Tier-2 fail-open vs fail-closed events", &["mode"], Kind::Counter),
    m("waddleai_hook_tool_calls_total", "Agent-hook tool-call volume by ecosystem/tool/org (efficiency + cache-hit analysis)", &["ecosystem", "tool_name", "organization"], Kind::Counter),
    m("waddleai_hook_rule_evaluations_total", "Admin hook_rules matched (regardless of whether they won), by rule id and scope", &["rule_id", "scope"], Kind::Counter),
    m("waddleai_hook_rule_decisions_total", "Admin hook_rules that actually decided the outcome, by rule id/scope/decision", &["rule_id", "scope", "decision"], Kind::Counter),
    m("waddleai_content_filter_fail_total", "ContentFilter internal failures, split by outcome", &["phase", "mode"], Kind::Counter),
    m("waddleai_security_auditor_duration_seconds", "LLM content-filter auditor invocation latency, by outcome", &["outcome"], Kind::Histogram(AUDITOR)),
    m("waddleai_token_limiter_fail_open_total", "TokenLimiter fail-open events (request allowed because the limiter itself failed)", &["phase"], Kind::Counter),
    m("waddleai_info", "WaddleAI service information", &["service", "version", "python_version"], Kind::Gauge),
];

/// Wrapped collector, so lookups return the right typed vec.
enum Collector {
    Counter(CounterVec),
    Gauge(GaugeVec),
    Histogram(HistogramVec),
}

/// Registry of all Python-parity collectors plus exposition rendering.
pub struct Metrics {
    registry: Registry,
    collectors: HashMap<&'static str, Collector>,
}

impl Metrics {
    /// Builds and registers every [`SPECS`] collector and sets the
    /// `waddleai_info` series for `service` (Python parity: `Info`).
    pub fn new(service: &str) -> Result<Self, TelemetryError> {
        let registry = Registry::new();
        let mut collectors = HashMap::new();
        for s in SPECS {
            let c = match s.kind {
                Kind::Counter => {
                    let v = CounterVec::new(Opts::new(s.name, s.help), s.labels)?;
                    registry.register(Box::new(v.clone()))?;
                    Collector::Counter(v)
                }
                Kind::Gauge => {
                    let v = GaugeVec::new(Opts::new(s.name, s.help), s.labels)?;
                    registry.register(Box::new(v.clone()))?;
                    Collector::Gauge(v)
                }
                Kind::Histogram(b) => {
                    let v = HistogramVec::new(
                        HistogramOpts::new(s.name, s.help).buckets(b.to_vec()),
                        s.labels,
                    )?;
                    registry.register(Box::new(v.clone()))?;
                    Collector::Histogram(v)
                }
            };
            collectors.insert(s.name, c);
        }
        let metrics = Self {
            registry,
            collectors,
        };
        metrics
            .gauge("waddleai_info")?
            .with_label_values(&[service, env!("CARGO_PKG_VERSION"), "rust"])
            .set(1.0);
        Ok(metrics)
    }

    /// Looks up a counter vec by exposed name.
    pub fn counter(&self, name: &str) -> Result<&CounterVec, TelemetryError> {
        match self.collectors.get(name) {
            Some(Collector::Counter(c)) => Ok(c),
            _ => Err(TelemetryError::UnknownMetric(name.to_owned())),
        }
    }

    /// Looks up a gauge vec by exposed name.
    pub fn gauge(&self, name: &str) -> Result<&GaugeVec, TelemetryError> {
        match self.collectors.get(name) {
            Some(Collector::Gauge(c)) => Ok(c),
            _ => Err(TelemetryError::UnknownMetric(name.to_owned())),
        }
    }

    /// Looks up a histogram vec by exposed name.
    pub fn histogram(&self, name: &str) -> Result<&HistogramVec, TelemetryError> {
        match self.collectors.get(name) {
            Some(Collector::Histogram(c)) => Ok(c),
            _ => Err(TelemetryError::UnknownMetric(name.to_owned())),
        }
    }

    /// Records one HTTP request (Python `record_request`): count + duration.
    /// `endpoint` is run through [`sanitize_label`] as a cardinality backstop.
    pub fn record_request(
        &self,
        service: &str,
        endpoint: &str,
        method: &str,
        status_code: u16,
        duration_secs: f64,
    ) -> Result<(), TelemetryError> {
        let endpoint = sanitize_label(endpoint);
        let status = status_code.to_string();
        self.counter("waddleai_requests_total")?
            .with_label_values(&[service, &endpoint, method, &status])
            .inc();
        self.histogram("waddleai_request_duration_seconds")?
            .with_label_values(&[service, &endpoint, method])
            .observe(duration_secs);
        Ok(())
    }

    /// Renders Prometheus text exposition (version 0.0.4). Families with no
    /// samples yet still emit `# HELP`/`# TYPE`, as `prometheus_client` does.
    pub fn render(&self) -> Result<String, TelemetryError> {
        use prometheus::Encoder;
        let families = self.registry.gather();
        let present: BTreeSet<String> = families.iter().map(|f| f.name().to_owned()).collect();
        let mut buf = Vec::new();
        prometheus::TextEncoder::new().encode(&families, &mut buf)?;
        let mut out = String::from_utf8(buf)?;
        for s in SPECS.iter().filter(|s| !present.contains(s.name)) {
            let ty = match s.kind {
                Kind::Counter => "counter",
                Kind::Gauge => "gauge",
                Kind::Histogram(_) => "histogram",
            };
            out.push_str(&format!(
                "# HELP {} {}\n# TYPE {} {}\n",
                s.name, s.help, s.name, ty
            ));
        }
        Ok(out)
    }
}

/// True when `s` (at least 36 bytes) starts with a canonical UUID.
fn uuid_at(b: &[u8]) -> bool {
    b.len() >= 36
        && b[..36].iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        })
}

/// Rewrites UUID substrings and bare 2+-digit path segments to `<id>`,
/// mirroring Python `_sanitize_label` so unbounded ids never become labels.
pub fn sanitize_label(value: &str) -> String {
    let b = value.as_bytes();
    let mut out = String::with_capacity(value.len());
    let mut i = 0;
    while i < b.len() {
        if uuid_at(&b[i..]) {
            out.push_str("<id>");
            i += 36;
        } else {
            let ch_len = value[i..].chars().next().map_or(1, char::len_utf8);
            out.push_str(&value[i..i + ch_len]);
            i += ch_len;
        }
    }
    // Numeric segments: lookbehind '/', 2+ digits, lookahead '/' or end.
    let segs: Vec<&str> = out.split('/').collect();
    segs.iter()
        .enumerate()
        .map(|(n, s)| {
            if n > 0 && s.len() >= 2 && s.bytes().all(|c| c.is_ascii_digit()) {
                "<id>"
            } else {
                s
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}
