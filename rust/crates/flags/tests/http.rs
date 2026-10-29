//! HTTP sources against a tiny in-process server (no external network).

use std::sync::{Arc, Mutex};
use std::time::Duration;

use config::{FlagCfg, LicenseCfg};
use flags::{
    FlagClient, FlagLookup, FlagSettings, FlagSource, HttpFlagSource, HttpLicenseSource,
    LicenseSource, Scope, SourceError, Tier,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// Serves `(status, body)` for every request, recording raw requests.
async fn serve(status: u16, body: &'static str) -> (String, Arc<Mutex<Vec<String>>>) {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let s2 = seen.clone();
    tokio::spawn(async move {
        loop {
            let Ok((mut sock, _)) = l.accept().await else {
                return;
            };
            let s3 = s2.clone();
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut chunk = [0u8; 4096];
                loop {
                    let n = sock.read(&mut chunk).await.unwrap_or(0);
                    if n == 0 {
                        return;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                    let text = String::from_utf8_lossy(&buf).to_string();
                    if let Some(i) = text.find("\r\n\r\n") {
                        let len = text[..i]
                            .to_ascii_lowercase()
                            .lines()
                            .find_map(|l| l.strip_prefix("content-length: ").map(str::to_owned))
                            .and_then(|v| v.trim().parse::<usize>().ok())
                            .unwrap_or(0);
                        if text.len() >= i + 4 + len {
                            s3.lock().unwrap().push(text);
                            break;
                        }
                    }
                }
                let resp = format!(
                    "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = sock.write_all(resp.as_bytes()).await;
            });
        }
    });
    (url, seen)
}

/// A server that accepts and never answers.
async fn blackhole() -> String {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", l.local_addr().unwrap());
    tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((s, _)) = l.accept().await {
            held.push(s);
        }
    });
    url
}

const T: Duration = Duration::from_millis(500);

fn flag_src(url: &str) -> HttpFlagSource {
    HttpFlagSource::new(reqwest::Client::new(), url, "phc_SECRET".into(), T)
}

fn lic_src(url: &str) -> HttpLicenseSource {
    HttpLicenseSource::new(
        reqwest::Client::new(),
        url,
        "PENG-SECRET".into(),
        "waddleai".into(),
        T,
    )
}

#[tokio::test]
async fn posthog_parses_bool_variant_and_unknown() {
    let (url, seen) = serve(
        200,
        r#"{"featureFlags":{"a":true,"b":false,"c":"variant-x","d":1,"n":null}}"#,
    )
    .await;
    let s = flag_src(&format!("{url}/"));
    assert_eq!(s.fetch("a", "u1").await, Ok(FlagLookup::Value(true)));
    assert_eq!(s.fetch("b", "u1").await, Ok(FlagLookup::Value(false)));
    assert_eq!(s.fetch("c", "u1").await, Ok(FlagLookup::Value(true)));
    assert_eq!(s.fetch("d", "u1").await, Ok(FlagLookup::Null));
    assert_eq!(s.fetch("n", "u1").await, Ok(FlagLookup::Null));
    assert_eq!(s.fetch("zzz", "u1").await, Ok(FlagLookup::Absent));
    let req = seen.lock().unwrap()[0].clone();
    assert!(req.starts_with("POST /flags/?v=2 "), "{req}");
    assert!(req.contains(r#""distinct_id":"u1""#));
}

#[tokio::test]
async fn posthog_errors_never_leak_key() {
    let (url, _) = serve(500, "{}").await;
    let e = flag_src(&url).fetch("a", "u").await.unwrap_err();
    assert!(matches!(e, SourceError::Unreachable(_)));
    assert!(!format!("{e} {e:?}").contains("SECRET"));
    let (url, _) = serve(200, r#"{"nope":1}"#).await;
    assert!(matches!(
        flag_src(&url).fetch("a", "u").await,
        Err(SourceError::Malformed(_))
    ));
    let (url, _) = serve(200, "not json").await;
    assert!(matches!(
        flag_src(&url).fetch("a", "u").await,
        Err(SourceError::Malformed(_))
    ));
    // Connection refused (port 1) is Unreachable.
    assert!(matches!(
        flag_src("http://127.0.0.1:1").fetch("a", "u").await,
        Err(SourceError::Unreachable(_))
    ));
    assert!(!format!("{:?}", flag_src("http://x")).contains("SECRET"));
}

#[tokio::test]
async fn posthog_request_timeout_is_bounded() {
    let url = blackhole().await;
    let s = HttpFlagSource::new(
        reqwest::Client::new(),
        &url,
        "k".into(),
        Duration::from_millis(100),
    );
    let r = tokio::time::timeout(Duration::from_secs(5), s.fetch("a", "u"))
        .await
        .unwrap();
    assert!(matches!(r, Err(SourceError::Unreachable(_))));
}

#[tokio::test]
async fn license_feature_and_auth_header() {
    let (url, seen) = serve(200, r#"{"features":[{"entitled":true}]}"#).await;
    let s = lic_src(&url);
    assert_eq!(
        s.feature("pii_ner_detection", &Scope::new("t")).await,
        Ok(true)
    );
    let req = seen.lock().unwrap()[0].clone();
    assert!(req.starts_with("POST /api/v2/features "), "{req}");
    assert!(req
        .to_ascii_lowercase()
        .contains("authorization: bearer peng-secret"));
    assert!(req.contains(r#""feature":"pii_ner_detection""#));
    let (url, _) = serve(200, r#"{"features":[]}"#).await;
    assert_eq!(
        lic_src(&url).feature("x", &Scope::new("t")).await,
        Ok(false)
    );
    let (url, _) = serve(200, r#"{}"#).await;
    assert!(matches!(
        lic_src(&url).feature("x", &Scope::new("t")).await,
        Err(SourceError::Malformed(_))
    ));
    let (url, _) = serve(503, "{}").await;
    let e = lic_src(&url)
        .feature("x", &Scope::new("t"))
        .await
        .unwrap_err();
    assert!(!format!("{e}").contains("SECRET"));
    assert!(!format!("{:?}", lic_src("http://x")).contains("SECRET"));
}

#[tokio::test]
async fn license_tier_parsing() {
    let sc = Scope::new("t");
    let (url, seen) = serve(200, r#"{"valid":true,"tier":"Enterprise"}"#).await;
    assert_eq!(lic_src(&url).tier(&sc).await, Ok(Tier::Enterprise));
    assert!(seen.lock().unwrap()[0].starts_with("POST /api/v2/validate "));
    let (url, _) = serve(200, r#"{"valid":false,"tier":"enterprise"}"#).await;
    assert_eq!(lic_src(&url).tier(&sc).await, Ok(Tier::Free));
    let (url, _) = serve(200, r#"{"valid":true}"#).await;
    assert!(matches!(
        lic_src(&url).tier(&sc).await,
        Err(SourceError::Malformed(_))
    ));
    let (url, _) = serve(200, r#"{"valid":true,"tier":"gold"}"#).await;
    assert!(matches!(
        lic_src(&url).tier(&sc).await,
        Err(SourceError::Malformed(_))
    ));
}

#[tokio::test]
async fn from_config_end_to_end_and_unconfigured() {
    let (url, _) = serve(200, r#"{"featureFlags":{"waddleai.on":true}}"#).await;
    let (lurl, _) = serve(
        200,
        r#"{"features":[{"entitled":true}],"valid":true,"tier":"professional"}"#,
    )
    .await;
    let c = FlagClient::from_config(
        &FlagCfg {
            key: Some("k".into()),
            host: url,
        },
        &LicenseCfg {
            key: "l".into(),
            server_url: lurl,
        },
        FlagSettings::default(),
    )
    .unwrap();
    assert!(c.enabled("waddleai.on", "u").await);
    assert!(c.has_feature("f", &Scope::new("t")).await);
    assert_eq!(c.get_tier(&Scope::new("t")).await, Tier::Professional);

    let off = FlagClient::from_config(
        &FlagCfg {
            key: None,
            host: "http://127.0.0.1:1".into(),
        },
        &LicenseCfg {
            key: String::new(),
            server_url: "http://127.0.0.1:1".into(),
        },
        FlagSettings::default(),
    )
    .unwrap();
    assert!(!off.enabled("waddleai.on", "u").await);
    assert!(!off.has_feature("f", &Scope::new("t")).await);
}

#[tokio::test]
async fn client_bounds_a_hung_server() {
    let url = blackhole().await;
    let s = FlagSettings {
        flag_timeout: Duration::from_millis(100),
        ..FlagSettings::default()
    };
    let c = FlagClient::from_config(
        &FlagCfg {
            key: Some("k".into()),
            host: url,
        },
        &LicenseCfg {
            key: String::new(),
            server_url: String::new(),
        },
        s,
    )
    .unwrap();
    let r = tokio::time::timeout(Duration::from_secs(5), c.enabled("waddleai.x", "u")).await;
    assert_eq!(r, Ok(false));
    // Teams kill-switch stays ON-mechanism when server hangs.
    assert!(c.teams_enabled(&Scope::new("t")).await);
}

#[test]
fn settings_from_env_and_scope() {
    std::env::set_var("FEATURE_FLAG_TTL_SECONDS", "7");
    std::env::set_var("FEATURE_FLAG_TIMEOUT_SECONDS", "garbage");
    std::env::set_var("FEATURE_FLAG_CACHE_MAX_ENTRIES", "5");
    std::env::set_var("LICENSE_MAX_STALE_SECONDS", "60");
    std::env::set_var("PRODUCT_NAME", "wp");
    let s = FlagSettings::from_env();
    assert_eq!(s.flag_ttl, Duration::from_secs(7));
    assert_eq!(s.flag_timeout, Duration::from_secs(3));
    assert_eq!(s.cache_max_entries, 5);
    assert_eq!(s.max_stale, Duration::from_secs(60));
    assert_eq!(s.product, "wp");
    assert_eq!(Scope::new("t").distinct_id(), "t");
    assert_eq!(Scope::new("t").with_team("x").distinct_id(), "x");
}
