//! Tests for `Config::from_env`; env is process-global so tests serialize on a mutex.
use config::{Config, ConfigError};
use std::sync::Mutex;

static ENV: Mutex<()> = Mutex::new(());

const VARS: [&str; 12] = [
    "DATABASE_URL",
    "REDIS_URL",
    "HTTP_PORT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "OTEL_DEPLOYMENT_ENVIRONMENT",
    "OTEL_TRACES_SAMPLER",
    "POSTHOG_KEY",
    "POSTHOG_HOST",
    "LICENSE_KEY",
    "LICENSE_SERVER_URL",
    "GRPC_PORT",
];

fn clear() {
    for v in VARS {
        std::env::remove_var(v);
    }
}

#[test]
fn from_env_reads_all_required() {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    clear();
    std::env::set_var("DATABASE_URL", "postgres://u:pw@db/waddle");
    std::env::set_var("REDIS_URL", "redis://cache:6379/2");
    std::env::set_var("HTTP_PORT", "9000");
    std::env::set_var("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4317");
    std::env::set_var("OTEL_SERVICE_NAME", "svc");
    std::env::set_var("POSTHOG_KEY", "phc_secret");
    std::env::set_var("POSTHOG_HOST", "https://ph.example");
    std::env::set_var("LICENSE_KEY", "lic_secret");
    std::env::set_var("LICENSE_SERVER_URL", "https://lic.example");
    let c = Config::from_env().unwrap();
    assert_eq!(c.database_url, "postgres://u:pw@db/waddle");
    assert_eq!(c.redis_url, "redis://cache:6379/2");
    assert_eq!(c.http_port, 9000);
    assert_eq!(c.otel.endpoint.as_deref(), Some("http://otel:4317"));
    assert_eq!(c.otel.service_name, "svc");
    assert_eq!(c.posthog.key.as_deref(), Some("phc_secret"));
    assert_eq!(c.posthog.host, "https://ph.example");
    assert_eq!(c.license.key, "lic_secret");
    assert_eq!(c.license.server_url, "https://lic.example");
    let dbg = format!("{c:?}");
    for s in ["pw@db", "phc_secret", "lic_secret"] {
        assert!(!dbg.contains(s), "Debug leaked {s}");
    }
    clear();
}

#[test]
fn from_env_missing_required_errors() {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    clear();
    assert_eq!(
        Config::from_env().unwrap_err(),
        ConfigError::Missing("DATABASE_URL")
    );
    clear();
}

#[test]
fn http_port_defaults_8080() {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    clear();
    std::env::set_var("DATABASE_URL", "postgres://x");
    let c = Config::from_env().unwrap();
    assert_eq!(c.http_port, 8080);
    assert_eq!(c.grpc_port, 50051);
    assert_eq!(c.redis_url, "redis://localhost:6379/0");
    assert_eq!(c.license.server_url, "https://license.penguintech.io");
    assert_eq!(c.otel.service_name, "waddleai");
    assert_eq!(c.otel.environment, "development");
    assert_eq!(c.otel.traces_sampler, "always_on");
    clear();
}

#[test]
fn invalid_port_errors() {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    clear();
    std::env::set_var("DATABASE_URL", "postgres://x");
    std::env::set_var("HTTP_PORT", "nope");
    assert!(matches!(
        Config::from_env(),
        Err(ConfigError::Invalid {
            var: "HTTP_PORT",
            ..
        })
    ));
    clear();
}
