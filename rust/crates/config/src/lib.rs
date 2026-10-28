//! Typed environment configuration for the WaddleAI Rust data plane.
//!
//! Reads the same env var names/defaults as the Python proxy; secrets are
//! redacted from `Debug` output.

use std::fmt;

/// Errors produced while loading configuration from the environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConfigError {
    /// A required variable was unset or empty.
    Missing(&'static str),
    /// A variable was set but could not be parsed.
    Invalid {
        /// Variable name.
        var: &'static str,
        /// Human-readable reason (never contains the value).
        reason: String,
    },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing(v) => write!(f, "missing required env var {v}"),
            Self::Invalid { var, reason } => write!(f, "invalid env var {var}: {reason}"),
        }
    }
}

impl std::error::Error for ConfigError {}

/// OpenTelemetry settings (`OTEL_*`); endpoint is optional (no exporter if unset).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OtelCfg {
    /// `OTEL_EXPORTER_OTLP_ENDPOINT`.
    pub endpoint: Option<String>,
    /// `OTEL_SERVICE_NAME`, default `waddleai`.
    pub service_name: String,
    /// `OTEL_DEPLOYMENT_ENVIRONMENT`, default `development`.
    pub environment: String,
    /// `OTEL_TRACES_SAMPLER`, default `always_on`.
    pub traces_sampler: String,
}

/// PostHog feature-flag settings; the key is a secret.
#[derive(Clone, PartialEq, Eq)]
pub struct FlagCfg {
    /// `POSTHOG_KEY` (secret); `None` disables flag fetching.
    pub key: Option<String>,
    /// `POSTHOG_HOST`, default `https://license.penguintech.io`.
    pub host: String,
}

/// License server settings; the key is a secret.
#[derive(Clone, PartialEq, Eq)]
pub struct LicenseCfg {
    /// `LICENSE_KEY` (secret), empty when unset.
    pub key: String,
    /// `LICENSE_SERVER_URL`, default `https://license.penguintech.io`.
    pub server_url: String,
}

/// Full data-plane configuration.
#[derive(Clone, PartialEq, Eq)]
pub struct Config {
    /// `DATABASE_URL` (required, may embed credentials).
    pub database_url: String,
    /// `REDIS_URL`, default `redis://localhost:6379/0` (may embed credentials).
    pub redis_url: String,
    /// `HTTP_PORT`, default 8080.
    pub http_port: u16,
    /// `GRPC_PORT`, default 50051.
    pub grpc_port: u16,
    /// OpenTelemetry settings.
    pub otel: OtelCfg,
    /// PostHog settings.
    pub posthog: FlagCfg,
    /// License settings.
    pub license: LicenseCfg,
}

const DEFAULT_LICENSE_URL: &str = "https://license.penguintech.io";

fn redact(set: bool) -> &'static str {
    if set {
        "<redacted>"
    } else {
        "<unset>"
    }
}

impl fmt::Debug for FlagCfg {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("FlagCfg")
            .field("key", &redact(self.key.is_some()))
            .field("host", &self.host)
            .finish()
    }
}

impl fmt::Debug for LicenseCfg {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LicenseCfg")
            .field("key", &redact(!self.key.is_empty()))
            .field("server_url", &self.server_url)
            .finish()
    }
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("database_url", &redact(true))
            .field("redis_url", &redact(true))
            .field("http_port", &self.http_port)
            .field("grpc_port", &self.grpc_port)
            .field("otel", &self.otel)
            .field("posthog", &self.posthog)
            .field("license", &self.license)
            .finish()
    }
}

fn get(var: &str) -> Option<String> {
    std::env::var(var).ok().filter(|v| !v.is_empty())
}

fn or_default(var: &str, default: &str) -> String {
    get(var).unwrap_or_else(|| default.to_owned())
}

fn port(var: &'static str, default: u16) -> Result<u16, ConfigError> {
    match get(var) {
        None => Ok(default),
        Some(v) => v.parse::<u16>().map_err(|e| ConfigError::Invalid {
            var,
            reason: e.to_string(),
        }),
    }
}

impl Config {
    /// Loads configuration from process environment variables.
    ///
    /// Fails with [`ConfigError::Missing`] if `DATABASE_URL` is unset, or
    /// [`ConfigError::Invalid`] if a port does not parse.
    pub fn from_env() -> Result<Self, ConfigError> {
        let database_url = get("DATABASE_URL").ok_or(ConfigError::Missing("DATABASE_URL"))?;
        Ok(Self {
            database_url,
            redis_url: or_default("REDIS_URL", "redis://localhost:6379/0"),
            http_port: port("HTTP_PORT", 8080)?,
            grpc_port: port("GRPC_PORT", 50051)?,
            otel: OtelCfg {
                endpoint: get("OTEL_EXPORTER_OTLP_ENDPOINT"),
                service_name: or_default("OTEL_SERVICE_NAME", "waddleai"),
                environment: or_default("OTEL_DEPLOYMENT_ENVIRONMENT", "development"),
                traces_sampler: or_default("OTEL_TRACES_SAMPLER", "always_on"),
            },
            posthog: FlagCfg {
                key: get("POSTHOG_KEY"),
                host: or_default("POSTHOG_HOST", DEFAULT_LICENSE_URL),
            },
            license: LicenseCfg {
                key: get("LICENSE_KEY").unwrap_or_default(),
                server_url: or_default("LICENSE_SERVER_URL", DEFAULT_LICENSE_URL),
            },
        })
    }
}
