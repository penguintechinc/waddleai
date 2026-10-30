//! `EmbeddingClient` against a local fake Ollama (no external services), plus an `#[ignore]`d check
//! against a real Ollama (`WADDLEAI_TEST_OLLAMA_URL`, model `nomic-embed-text` pulled).

use std::sync::Arc;
use std::time::Duration;

use respcache::{Embedder, EmbeddingClient};
use telemetry::Metrics;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// Serves exactly `replies.len()` HTTP/1.1 responses, one per connection, then stops.
async fn fake_ollama(
    replies: Vec<(u16, String)>,
) -> (String, tokio::task::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let h = tokio::spawn(async move {
        let mut requests = Vec::new();
        for (status, body) in replies {
            let (mut sock, _) = listener.accept().await.unwrap();
            let mut buf = vec![0u8; 16384];
            let mut got = Vec::new();
            // read until the JSON body (after the blank line) is complete
            loop {
                let n = sock.read(&mut buf).await.unwrap();
                got.extend_from_slice(&buf[..n]);
                let text = String::from_utf8_lossy(&got).to_string();
                if let Some((head, rest)) = text.split_once("\r\n\r\n") {
                    let want: usize = head
                        .lines()
                        .find_map(|l| {
                            l.to_lowercase()
                                .strip_prefix("content-length:")
                                .map(|v| v.trim().parse().unwrap())
                        })
                        .unwrap_or(0);
                    if rest.len() >= want || n == 0 {
                        break;
                    }
                }
                if n == 0 {
                    break;
                }
            }
            requests.push(String::from_utf8_lossy(&got).to_string());
            let resp = format!(
                "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            );
            sock.write_all(resp.as_bytes()).await.unwrap();
            let _ = sock.shutdown().await;
        }
        requests
    });
    (url, h)
}

fn calls(m: &Metrics, label: &str, outcome: &str) -> f64 {
    m.counter("waddleai_embedding_calls_total")
        .unwrap()
        .with_label_values(&[label, outcome])
        .get()
}

#[tokio::test]
async fn embeds_via_ollama_api_and_records_ok_metric() {
    let (url, srv) = fake_ollama(vec![(200, r#"{"embedding":[0.5,-0.25,1.0]}"#.into())]).await;
    let m = Arc::new(Metrics::new("embed-test").unwrap());
    let c = EmbeddingClient::new(&url, "nomic-embed-text", "chat_ollama", Some(m.clone())).unwrap();
    let v = c.embed("What is Rust?").await.unwrap();
    assert_eq!(v, vec![0.5, -0.25, 1.0]);
    let reqs = srv.await.unwrap();
    assert!(reqs[0].starts_with("POST /api/embeddings "), "{}", reqs[0]);
    assert!(reqs[0].contains("\"model\":\"nomic-embed-text\""));
    assert!(reqs[0].contains("\"prompt\":\"What is Rust?\""));
    assert_eq!(calls(&m, "chat_ollama", "ok"), 1.0);
    assert_eq!(calls(&m, "chat_ollama", "error"), 0.0);
}

#[tokio::test]
async fn http_error_malformed_and_empty_replies_are_errors_without_the_prompt() {
    for (status, body) in [
        (500, r#"{"error":"boom"}"#),
        (200, r#"{"nope":1}"#),
        (200, r#"{"embedding":[]}"#),
        (200, "not json"),
    ] {
        let (url, srv) = fake_ollama(vec![(status, body.into())]).await;
        let m = Arc::new(Metrics::new("embed-test").unwrap());
        let c = EmbeddingClient::new(&url, "m", "embedding_ollama", Some(m.clone())).unwrap();
        let err = c.embed("super secret prompt").await.unwrap_err();
        assert!(!err.0.contains("super secret"), "{err}");
        assert_eq!(
            calls(&m, "embedding_ollama", "error"),
            1.0,
            "{status} {body}"
        );
        srv.await.unwrap();
    }
}

#[tokio::test]
async fn unreachable_endpoint_is_an_error_not_a_hang() {
    // bind then drop to get a port nothing listens on
    let port = TcpListener::bind("127.0.0.1:0")
        .await
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let m = Arc::new(Metrics::new("embed-test").unwrap());
    let c = EmbeddingClient::new(
        &format!("http://127.0.0.1:{port}/"),
        "m",
        "chat_ollama",
        Some(m.clone()),
    )
    .unwrap();
    let r = tokio::time::timeout(Duration::from_secs(5), c.embed("x"))
        .await
        .expect("no hang");
    assert!(r.is_err());
    assert_eq!(calls(&m, "chat_ollama", "error"), 1.0);
}

#[tokio::test]
#[ignore = "needs a live Ollama with nomic-embed-text (WADDLEAI_TEST_OLLAMA_URL)"]
async fn real_ollama_returns_a_768_dim_vector() {
    let url = std::env::var("WADDLEAI_TEST_OLLAMA_URL")
        .expect("WADDLEAI_TEST_OLLAMA_URL must be set for --ignored ollama tests");
    let c = EmbeddingClient::new(
        &url,
        respcache::DEFAULT_EMBEDDING_MODEL,
        "chat_ollama",
        None,
    )
    .unwrap();
    let v = c.embed("What is Rust?").await.expect("real embedding");
    assert_eq!(v.len(), 768);
    assert!(v.iter().all(|f| f.is_finite()));
}
