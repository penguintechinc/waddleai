//! Health + metrics routes. DB/Valkey tests need real services
//! (`TEST_DATABASE_URL`, `TEST_REDIS_URL`) and FAIL loudly when unset.
use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use telemetry::{router, HealthState, Metrics, Readiness, SPECS};
use tower::ServiceExt;

/// Env is process-global; building a Config serializes on this.
static ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn app(readiness: Readiness) -> Router {
    router(HealthState {
        readiness,
        metrics: Arc::new(Metrics::new("proxy").expect("metrics")),
    })
}

async fn get(app: &Router, path: &str) -> (StatusCode, String) {
    let resp = app
        .clone()
        .oneshot(Request::get(path).body(Body::empty()).expect("req"))
        .await
        .expect("resp");
    let status = resp.status();
    let body = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8(body.to_vec()).expect("utf8"))
}

fn cfg_for(redis_url: &str) -> config::Config {
    let _g = ENV.lock().unwrap_or_else(|e| e.into_inner());
    std::env::set_var("DATABASE_URL", "postgres://x/y");
    std::env::set_var("REDIS_URL", redis_url);
    config::Config::from_env().expect("config")
}

async fn real_db() -> db::Connection {
    let url = std::env::var("TEST_DATABASE_URL").expect("TEST_DATABASE_URL must be set");
    db::connect_with(
        &url,
        &db::DbOptions {
            pool_size: 2,
            max_retries: 1,
            retry_delay: std::time::Duration::from_millis(10),
        },
    )
    .await
    .expect("db connect")
}

async fn real_cache() -> cache::Cache {
    let url = std::env::var("TEST_REDIS_URL").expect("TEST_REDIS_URL must be set");
    cache::connect(&cfg_for(&url)).await.expect("cache connect")
}

#[tokio::test]
async fn healthz_200() {
    let a = app(Readiness::new());
    for p in ["/healthz", "/livez"] {
        let (s, b) = get(&a, p).await;
        assert_eq!(s, StatusCode::OK, "{p}");
        assert!(b.contains(r#""status":"ok""#), "{b}");
    }
}

#[tokio::test]
async fn readyz_503_when_db_down_200_when_up() {
    let conn = real_db().await;
    let cache = real_cache().await;
    let up = app(Readiness::new().with_db(conn.clone()).with_cache(cache));
    let (s, b) = get(&up, "/readyz").await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(
        b.contains(r#""db":"ok""#) && b.contains(r#""cache":"ok""#),
        "{b}"
    );

    // Same pool, closed: a genuine failing ping, not a stubbed flag.
    conn.close_by_ref().await.expect("close");
    let (s, b) = get(&up, "/readyz").await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE, "{b}");
    assert!(
        b.contains(r#""db":"down""#) && b.contains(r#""cache":"ok""#),
        "{b}"
    );
    assert!(!b.contains("error"), "must not leak error detail: {b}");
}

/// Fake Valkey: answers `+PONG` to the first connection's first PING
/// (handshake-time ping) then hangs up, so later probes fail for real.
async fn flaky_valkey() -> String {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let l = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let url = format!("redis://{}", l.local_addr().expect("addr"));
    tokio::spawn(async move {
        if let Ok((mut sock, _)) = l.accept().await {
            let mut buf = [0u8; 4096];
            while let Ok(n) = sock.read(&mut buf).await {
                if n == 0 {
                    return;
                }
                let text = String::from_utf8_lossy(&buf[..n]).to_ascii_uppercase();
                let mut reply = Vec::new();
                let mut done = false;
                for cmd in text.split("\r\n*").filter(|c| !c.trim().is_empty()) {
                    if cmd.contains("PING") {
                        reply.extend_from_slice(b"+PONG\r\n");
                        done = true;
                    } else if cmd.contains("HELLO") {
                        reply.extend_from_slice(b"-ERR unknown command\r\n");
                    } else {
                        reply.extend_from_slice(b"+OK\r\n");
                    }
                }
                let _ = sock.write_all(&reply).await;
                if done {
                    return;
                }
            }
        }
    });
    url
}

#[tokio::test]
async fn readyz_503_when_cache_down() {
    let url = flaky_valkey().await;
    let cache = cache::connect(&cfg_for(&url)).await.expect("handshake");
    let a = app(Readiness::new().with_cache(cache));
    let (s, b) = get(&a, "/readyz").await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE, "{b}");
    assert!(b.contains(r#""cache":"down""#), "{b}");
}

#[tokio::test]
async fn readyz_fails_closed_without_probes() {
    let (s, b) = get(&app(Readiness::new()), "/readyz").await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    assert!(b.contains("no readiness checks"), "{b}");
}

#[tokio::test]
async fn readyz_probe_error_timeout_and_ok() {
    let r = Readiness::new()
        .with_check("fine", || Box::pin(async { Ok(()) }))
        .with_check("err", || Box::pin(async { Err("boom".to_owned()) }))
        .with_check("slow", || {
            Box::pin(async {
                tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                Ok(())
            })
        })
        .with_check("panics", || {
            Box::pin(async {
                panic!("probe panic");
            })
        });
    let (s, b) = get(&app(r), "/readyz").await;
    assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
    for want in [
        r#""fine":"ok""#,
        r#""err":"down""#,
        r#""slow":"down""#,
        r#""panics":"down""#,
    ] {
        assert!(b.contains(want), "{want} in {b}");
    }
}

#[tokio::test]
async fn metrics_exposes_expected_names() {
    let a = app(Readiness::new());
    let (s, b) = get(&a, "/metrics").await;
    assert_eq!(s, StatusCode::OK);
    assert!(SPECS.len() >= 38, "denominator: {}", SPECS.len());
    for sp in SPECS {
        assert!(
            b.contains(&format!("# HELP {} {}", sp.name, sp.help)),
            "HELP {}",
            sp.name
        );
        assert!(
            b.contains(&format!("# TYPE {} ", sp.name)),
            "TYPE {}",
            sp.name
        );
    }
    assert!(
        b.contains(r#"waddleai_info{python_version="rust",service="proxy","#),
        "{b}"
    );
}

/// The set of `"waddleai_*"` literals in the Python proxy sources must equal SPECS exactly.
#[test]
fn python_metric_names_match_specs_exactly() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../shared");
    let mut py = std::collections::BTreeSet::new();
    for f in [
        "utils/metrics.py",
        "security/content_filter.py",
        "utils/token_limiter.py",
    ] {
        let src = std::fs::read_to_string(root.join(f)).expect("python source readable");
        for part in src.split('"').skip(1).step_by(2) {
            // `waddleai_tokens` is a usage-dict key in metrics.py, not a metric.
            let is_name = part.starts_with("waddleai_")
                && part != "waddleai_tokens"
                && part.bytes().all(|c| c.is_ascii_lowercase() || c == b'_');
            if is_name {
                py.insert(part.to_owned());
            }
        }
    }
    let rust: std::collections::BTreeSet<String> =
        SPECS.iter().map(|s| s.name.to_owned()).collect();
    assert!(py.len() >= 38, "scanned only {} python names", py.len());
    assert_eq!(py, rust);
}

#[tokio::test]
async fn record_request_and_buckets_render() {
    let m = Arc::new(Metrics::new("proxy").expect("metrics"));
    m.record_request(
        "proxy",
        "/memories/3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "GET",
        200,
        0.06,
    )
    .expect("record");
    m.record_request("proxy", "/users/12345/keys", "GET", 404, 0.001)
        .expect("record");
    let out = m.render().expect("render");
    assert!(out.contains(r#"waddleai_requests_total{endpoint="/memories/<id>",method="GET",service="proxy",status_code="200"} 1"#), "{out}");
    assert!(out.contains(r#"endpoint="/users/<id>/keys""#), "{out}");
    // Python default buckets include 0.075 (the Rust crate default does not).
    assert!(out.contains(r#"le="0.075""#), "{out}");
    assert!(
        out.contains(r#"waddleai_request_duration_seconds_bucket"#),
        "{out}"
    );
}

#[test]
fn lookups_error_on_unknown_or_wrong_kind() {
    let m = Metrics::new("proxy").expect("metrics");
    assert!(m.counter("nope").is_err());
    assert!(m.gauge("waddleai_requests_total").is_err());
    assert!(m.histogram("waddleai_requests_total").is_err());
    assert!(m.counter("waddleai_requests_total").is_ok());
    assert!(m.gauge("waddleai_info").is_ok());
    assert!(m.histogram("waddleai_request_duration_seconds").is_ok());
    let e = m.counter("nope").expect_err("err");
    assert!(e.to_string().contains("nope"));
}

#[test]
fn sanitize_label_cases() {
    use telemetry::sanitize_label as s;
    assert_eq!(s("/a/b"), "/a/b");
    assert_eq!(s("/a/7/b"), "/a/7/b");
    assert_eq!(s("/a/77/b"), "/a/<id>/b");
    assert_eq!(s("/a/77"), "/a/<id>");
    assert_eq!(s("/a/77x"), "/a/77x");
    assert_eq!(s("/é/ü"), "/é/ü");
    assert_eq!(s("/k/3FA85F64-5717-4562-B3FC-2C963F66AFA6/x"), "/k/<id>/x");
}

#[tokio::test]
async fn serve_answers_over_tcp() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let l = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = l.local_addr().expect("addr");
    let h = tokio::spawn(telemetry::serve(l, app(Readiness::new())));
    let mut s = tokio::net::TcpStream::connect(addr).await.expect("connect");
    s.write_all(b"GET /healthz HTTP/1.1\r\nhost: x\r\nconnection: close\r\n\r\n")
        .await
        .expect("write");
    let mut out = String::new();
    s.read_to_string(&mut out).await.expect("read");
    assert!(out.starts_with("HTTP/1.1 200"), "{out}");
    h.abort();
}
