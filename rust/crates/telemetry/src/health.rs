//! Axum `/healthz`, `/livez`, `/readyz`, `/metrics` routes.
//!
//! `/healthz` and `/livez` are process-liveness only. `/readyz` runs real
//! dependency probes (DB `ping`, Valkey `PING`) with a bounded timeout and
//! fails closed: no probes configured, a probe error, or a timeout => 503.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::State;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;

use crate::metrics::Metrics;

const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// Boxed readiness probe future: `Ok(())` when the dependency is healthy.
pub type ProbeFuture = Pin<Box<dyn Future<Output = Result<(), String>> + Send>>;
type Probe = Arc<dyn Fn() -> ProbeFuture + Send + Sync>;

/// Set of named dependency probes backing `/readyz`.
#[derive(Clone, Default)]
pub struct Readiness {
    checks: Vec<(&'static str, Probe)>,
}

impl Readiness {
    /// Empty set (note: `/readyz` is 503 until a probe is added).
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds a custom named probe.
    pub fn with_check<F>(mut self, name: &'static str, probe: F) -> Self
    where
        F: Fn() -> ProbeFuture + Send + Sync + 'static,
    {
        self.checks.push((name, Arc::new(probe)));
        self
    }

    /// Adds the real Postgres probe: a round-trip `ping` on the pool.
    pub fn with_db(self, conn: db::Connection) -> Self {
        self.with_check("db", move || {
            let conn = conn.clone();
            Box::pin(async move { conn.ping().await.map_err(|e| e.to_string()) })
        })
    }

    /// Adds the real Valkey probe: a `PING` round-trip.
    pub fn with_cache(self, cache: cache::Cache) -> Self {
        self.with_check("cache", move || {
            let cache = cache.clone();
            Box::pin(async move { cache.ping().await.map_err(|e| e.to_string()) })
        })
    }

    /// Runs every probe concurrently; returns `(name, healthy)` per check.
    pub async fn run(&self) -> Vec<(&'static str, bool)> {
        let mut set = tokio::task::JoinSet::new();
        for (i, (name, probe)) in self.checks.iter().enumerate() {
            let name: &'static str = name;
            let fut = probe();
            set.spawn(async move {
                let ok = match tokio::time::timeout(PROBE_TIMEOUT, fut).await {
                    Ok(Ok(())) => true,
                    Ok(Err(e)) => {
                        tracing::warn!(check = name, error = %e, "readiness probe failed");
                        false
                    }
                    Err(_) => {
                        tracing::warn!(check = name, "readiness probe timed out");
                        false
                    }
                };
                (i, ok)
            });
        }
        // A probe task that panics/aborts stays `false` (fail closed).
        let mut out: Vec<(&'static str, bool)> =
            self.checks.iter().map(|(n, _)| (*n, false)).collect();
        while let Some(res) = set.join_next().await {
            match res {
                Ok((i, ok)) => out[i].1 = ok,
                Err(e) => tracing::warn!(error = %e, "readiness probe task failed"),
            }
        }
        out
    }
}

/// Shared state for the health/metrics router.
#[derive(Clone)]
pub struct HealthState {
    /// Dependency probes for `/readyz`.
    pub readiness: Readiness,
    /// Metric registry rendered by `/metrics`.
    pub metrics: Arc<Metrics>,
}

/// Builds the router exposing `/healthz /livez /readyz /metrics`.
pub fn router(state: HealthState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/livez", get(healthz))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics))
        .with_state(state)
}

/// Serves `app` on `listener` until the server errors.
pub async fn serve(listener: tokio::net::TcpListener, app: Router) -> std::io::Result<()> {
    axum::serve(listener, app).await
}

fn json(status: StatusCode, body: String) -> Response {
    (status, [(header::CONTENT_TYPE, "application/json")], body).into_response()
}

async fn healthz() -> Response {
    json(StatusCode::OK, r#"{"status":"ok"}"#.to_owned())
}

async fn readyz(State(st): State<HealthState>) -> Response {
    if st.readiness.checks.is_empty() {
        tracing::error!("readyz called with no readiness probes configured");
        return json(
            StatusCode::SERVICE_UNAVAILABLE,
            r#"{"status":"unready","reason":"no readiness checks configured"}"#.to_owned(),
        );
    }
    let results = st.readiness.run().await;
    let all_ok = results.iter().all(|(_, ok)| *ok);
    let checks = results
        .iter()
        .map(|(n, ok)| format!(r#""{n}":"{}""#, if *ok { "ok" } else { "down" }))
        .collect::<Vec<_>>()
        .join(",");
    let (code, status) = if all_ok {
        (StatusCode::OK, "ready")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "unready")
    };
    json(
        code,
        format!(r#"{{"status":"{status}","checks":{{{checks}}}}}"#),
    )
}

async fn metrics(State(st): State<HealthState>) -> Response {
    match st.metrics.render() {
        Ok(body) => (
            StatusCode::OK,
            [(
                header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            )],
            body,
        )
            .into_response(),
        Err(e) => {
            tracing::error!(error = %e, "metrics render failed");
            (StatusCode::INTERNAL_SERVER_ERROR, "metrics unavailable").into_response()
        }
    }
}
