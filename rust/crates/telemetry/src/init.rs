//! `tracing` subscriber + OTLP trace/metric export, configured only from the
//! `OTEL_*` values already loaded into [`config::OtelCfg`].
//!
//! A dead or misconfigured exporter never fails the app: every failure here
//! degrades to "no export" with a warning (critical-rules: telemetry failure
//! is never a request failure).

use std::time::Duration;

use config::OtelCfg;
use opentelemetry::global;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::KeyValue;
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::trace::{Sampler, SdkTracerProvider};
use opentelemetry_sdk::Resource;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::EnvFilter;

const EXPORT_TIMEOUT: Duration = Duration::from_secs(3);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);

/// Owns the OTLP providers; flushes (bounded) on drop.
pub struct TelemetryGuard {
    tracer: Option<SdkTracerProvider>,
    meter: Option<SdkMeterProvider>,
    /// True when a global tracing subscriber was installed by this call.
    pub subscriber_installed: bool,
}

impl TelemetryGuard {
    /// True when an OTLP trace pipeline is active.
    pub fn exporting_traces(&self) -> bool {
        self.tracer.is_some()
    }

    /// True when an OTLP metric pipeline is active.
    pub fn exporting_metrics(&self) -> bool {
        self.meter.is_some()
    }
}

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        if let Some(t) = self.tracer.take() {
            if let Err(e) = t.shutdown_with_timeout(SHUTDOWN_TIMEOUT) {
                tracing::warn!(error = %e, "otlp trace shutdown incomplete");
            }
        }
        if let Some(m) = self.meter.take() {
            if let Err(e) = m.shutdown_with_timeout(SHUTDOWN_TIMEOUT) {
                tracing::warn!(error = %e, "otlp metric shutdown incomplete");
            }
        }
    }
}

/// Maps `OTEL_TRACES_SAMPLER` to a sampler; unknown values fall back to
/// `always_on` with a warning (no ratio arg is carried by `OtelCfg`).
fn sampler(name: &str) -> Sampler {
    match name {
        "always_on" => Sampler::AlwaysOn,
        "always_off" => Sampler::AlwaysOff,
        "parentbased_always_on" => Sampler::ParentBased(Box::new(Sampler::AlwaysOn)),
        "parentbased_always_off" => Sampler::ParentBased(Box::new(Sampler::AlwaysOff)),
        other => {
            tracing::warn!(
                sampler = other,
                "unsupported OTEL_TRACES_SAMPLER; using always_on"
            );
            Sampler::AlwaysOn
        }
    }
}

/// OTLP/HTTP signal URL: base endpoint + `/v1/{signal}`.
pub fn signal_url(base: &str, signal: &str) -> String {
    format!("{}/v1/{signal}", base.trim_end_matches('/'))
}

fn resource(cfg: &OtelCfg) -> Resource {
    Resource::builder()
        .with_service_name(cfg.service_name.clone())
        .with_attribute(KeyValue::new(
            "deployment.environment.name",
            cfg.environment.clone(),
        ))
        .build()
}

fn build_tracer(cfg: &OtelCfg, base: &str) -> Result<SdkTracerProvider, String> {
    let exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_http()
        .with_endpoint(signal_url(base, "traces"))
        .with_timeout(EXPORT_TIMEOUT)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(SdkTracerProvider::builder()
        .with_sampler(sampler(&cfg.traces_sampler))
        .with_resource(resource(cfg))
        .with_batch_exporter(exporter)
        .build())
}

fn build_meter(cfg: &OtelCfg, base: &str) -> Result<SdkMeterProvider, String> {
    let exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_http()
        .with_endpoint(signal_url(base, "metrics"))
        .with_timeout(EXPORT_TIMEOUT)
        .build()
        .map_err(|e| e.to_string())?;
    Ok(SdkMeterProvider::builder()
        .with_resource(resource(cfg))
        .with_reader(PeriodicReader::builder(exporter).build())
        .build())
}

/// Installs the `tracing` subscriber (env-filtered fmt + OTel layer when an
/// endpoint is configured) and global OTel providers. Never fails: exporter
/// build errors or an already-installed subscriber are logged and skipped.
///
/// The OTLP destination comes solely from `cfg` (loaded from `OTEL_*`); with
/// no endpoint no exporter is created and spans/metrics are no-ops.
pub fn init_telemetry(cfg: &OtelCfg) -> TelemetryGuard {
    let mut tracer = None;
    let mut meter = None;
    let mut build_errors = Vec::new();
    if let Some(base) = cfg.endpoint.as_deref() {
        match build_tracer(cfg, base) {
            Ok(t) => tracer = Some(t),
            Err(e) => build_errors.push(("traces", e)),
        }
        match build_meter(cfg, base) {
            Ok(m) => meter = Some(m),
            Err(e) => build_errors.push(("metrics", e)),
        }
    }

    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let otel_layer = tracer.as_ref().map(|t| {
        tracing_opentelemetry::OpenTelemetryLayer::new(t.tracer(cfg.service_name.clone()))
    });
    let subscriber_installed = tracing_subscriber::registry()
        .with(filter)
        .with(tracing_subscriber::fmt::layer())
        .with(otel_layer)
        .try_init()
        .is_ok();
    if let Some(t) = &tracer {
        global::set_tracer_provider(t.clone());
    }
    if let Some(m) = &meter {
        global::set_meter_provider(m.clone());
    }
    for (signal, e) in build_errors {
        tracing::warn!(signal, error = %e, "otlp exporter disabled; continuing without export");
    }
    if !subscriber_installed {
        tracing::debug!("tracing subscriber already installed; reusing");
    }
    TelemetryGuard {
        tracer,
        meter,
        subscriber_installed,
    }
}
