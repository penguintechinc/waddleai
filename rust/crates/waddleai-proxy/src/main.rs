//! WaddleAI Rust data-plane proxy entrypoint.
use std::sync::Arc;

use clap::Parser;
use telemetry::{HealthState, Metrics, Readiness};

/// Command-line interface for the proxy; `--version` is derived from the crate version.
#[derive(Parser)]
#[command(
    name = "waddleai-proxy",
    version,
    about = "WaddleAI Rust data-plane proxy"
)]
struct Cli {}

/// Boxed error for the process boundary; the proxy exits non-zero on startup failure.
type BoxError = Box<dyn std::error::Error + Send + Sync>;

/// Loads config, brings up telemetry, connects the DB and cache, and serves
/// the health/metrics routes until the server stops.
async fn run() -> Result<(), BoxError> {
    let cfg = config::Config::from_env()?;
    let _telemetry = telemetry::init_telemetry(&cfg.otel);
    let conn = db::connect(&cfg).await?;
    let cache = cache::connect(&cfg).await?;
    let state = HealthState {
        readiness: Readiness::new().with_db(conn).with_cache(cache),
        metrics: Arc::new(Metrics::new(&cfg.otel.service_name)?),
    };
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", cfg.http_port)).await?;
    tracing::info!(port = cfg.http_port, "proxy listening");
    telemetry::serve(listener, telemetry::router(state)).await?;
    Ok(())
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let _cli = Cli::parse();
    match run().await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "proxy failed");
            std::process::ExitCode::FAILURE
        }
    }
}
