//! WaddleAI telemetry base: `tracing` + OTLP init, the Python-parity
//! Prometheus registry, GenAI semconv instruments, and health/metrics routes.

pub mod error;
pub mod genai;
pub mod health;
pub mod init;
pub mod metrics;

pub use error::TelemetryError;
pub use genai::GenAiMetrics;
pub use health::{router, serve, HealthState, ProbeFuture, Readiness};
pub use init::{init_telemetry, TelemetryGuard};
pub use metrics::{sanitize_label, Kind, MetricSpec, Metrics, SPECS};
