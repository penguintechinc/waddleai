//! Shared test helpers: a raw chunked-HTTP server (for split-chunk, stall and disconnect
//! scenarios wiremock cannot express), context and dispatcher builders.
#![allow(dead_code)]

use std::sync::Arc;
use std::time::Duration;

use connectors::{
    ConnectorConfig, ConnectorRegistry, DispatchConfig, LlamaCppConnector, LocalDispatcher,
    OllamaConnector,
};
use pipeline::{Depseudonymizer, IdentityDepseudonymizer, PipelineContext};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;

/// A chunk to send plus the pause before sending it.
pub struct Piece {
    pub bytes: Vec<u8>,
    pub delay_ms: u64,
}

/// What the raw server does after the pieces are sent.
pub enum After {
    /// Terminate the chunked body cleanly.
    Finish,
    /// Hold the connection open and report when the client closes it.
    HoldUntilClientCloses,
}

/// Starts a one-shot raw HTTP server; returns its base URL and a receiver that fires when the
/// client closes the connection (only meaningful for `HoldUntilClientCloses`).
pub async fn raw_server(
    content_type: &'static str,
    pieces: Vec<Piece>,
    after: After,
) -> (String, oneshot::Receiver<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let (closed_tx, closed_rx) = oneshot::channel();
    tokio::spawn(async move {
        let (mut sock, _) = listener.accept().await.unwrap();
        sock.set_nodelay(true).unwrap();
        let mut buf = vec![0u8; 8192];
        let mut got = Vec::new();
        // Read the request head (the body follows; we only need to get past the headers).
        loop {
            let n = sock.read(&mut buf).await.unwrap_or(0);
            if n == 0 {
                return;
            }
            got.extend_from_slice(&buf[..n]);
            if got.windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        let head = format!(
            "HTTP/1.1 200 OK\r\ncontent-type: {content_type}\r\ntransfer-encoding: chunked\r\n\r\n"
        );
        sock.write_all(head.as_bytes()).await.unwrap();
        for p in pieces {
            tokio::time::sleep(Duration::from_millis(p.delay_ms)).await;
            let mut framed = format!("{:x}\r\n", p.bytes.len()).into_bytes();
            framed.extend_from_slice(&p.bytes);
            framed.extend_from_slice(b"\r\n");
            if sock.write_all(&framed).await.is_err() {
                let _ = closed_tx.send(());
                return;
            }
        }
        match after {
            After::Finish => {
                let _ = sock.write_all(b"0\r\n\r\n").await;
            }
            After::HoldUntilClientCloses => loop {
                let mut b = [0u8; 64];
                match sock.read(&mut b).await {
                    Ok(0) | Err(_) => {
                        let _ = closed_tx.send(());
                        return;
                    }
                    Ok(_) => {}
                }
            },
        }
    });
    (url, closed_rx)
}

/// Short-timeout config for a test server.
pub fn cfg(url: &str) -> ConnectorConfig {
    let mut c = ConnectorConfig::new(url);
    c.request_timeout = Duration::from_secs(5);
    c.idle_timeout = Duration::from_secs(5);
    c
}

/// A user-message context targeting `model`.
pub fn ctx(model: &str) -> PipelineContext {
    let mut c = PipelineContext::new(json!({}));
    c.model = Some(model.to_owned());
    c.messages = vec![json!({"role": "user", "content": "hi"})];
    c
}

/// Dispatcher with an Ollama connector at `ollama` and optionally llama.cpp at `llama`.
pub fn dispatcher(
    ollama: &str,
    llama: Option<&str>,
    depseudo: Arc<dyn Depseudonymizer>,
) -> LocalDispatcher {
    let mut reg = ConnectorRegistry::new();
    reg.register(Arc::new(OllamaConnector::new(cfg(ollama)).unwrap()));
    if let Some(l) = llama {
        reg.register(Arc::new(LlamaCppConnector::new(cfg(l)).unwrap()));
    }
    LocalDispatcher::new(Arc::new(reg), depseudo, DispatchConfig::default())
}

/// Identity-depseudonymizing dispatcher over a single Ollama URL.
pub fn simple(ollama: &str) -> LocalDispatcher {
    dispatcher(ollama, None, Arc::new(IdentityDepseudonymizer))
}

/// Accepts one connection and never sends response headers; returns the base URL.
pub async fn silent_server() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move {
        let (mut sock, _) = listener.accept().await.unwrap();
        let mut b = [0u8; 4096];
        while matches!(sock.read(&mut b).await, Ok(n) if n > 0) {}
    });
    url
}
