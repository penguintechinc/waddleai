//! Error type for the telemetry crate.

/// Errors from building the metrics registry or rendering exposition text.
#[derive(Debug, thiserror::Error)]
pub enum TelemetryError {
    /// A Prometheus collector could not be built/registered/encoded.
    #[error("prometheus: {0}")]
    Prometheus(#[from] prometheus::Error),
    /// The exposition text was not valid UTF-8.
    #[error("metrics exposition was not utf-8")]
    Utf8(#[from] std::string::FromUtf8Error),
    /// A metric name that is not part of the Python-parity spec was requested.
    #[error("unknown metric {0}")]
    UnknownMetric(String),
}
