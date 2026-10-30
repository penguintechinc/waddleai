//! Ollama connector protocol tests against wiremock and raw chunked servers.

mod common;

use common::{cfg, raw_server, After, Piece};
use connectors::OllamaConnector;
use futures_util::StreamExt;
use pipeline::{Connector, ConnectorError, ConnectorRequest, Credential, StreamChunk};
use secrecy::SecretString;
use serde_json::{json, Value};
use wiremock::matchers::{body_partial_json, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn req(params: Value) -> ConnectorRequest {
    ConnectorRequest {
        model: "gemma4:e4b".into(),
        messages: vec![json!({"role": "user", "content": "hi"})],
        params,
        credential: None,
    }
}

#[tokio::test]
async fn ollama_chat_maps_request_and_usage() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/chat"))
        .and(body_partial_json(json!({
            "model": "gemma4:e4b", "stream": false,
            "options": {"temperature": 0.7, "num_predict": 50},
            "messages": [{"role": "user", "content": "hi"}]
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "message": {"role": "assistant", "content": "hello"},
            "done": true, "done_reason": "length",
            "prompt_eval_count": 11, "eval_count": 7
        })))
        .expect(1)
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let r = c.chat(&req(json!({"max_tokens": 50}))).await.unwrap();
    assert_eq!(r.text, "hello");
    assert_eq!((r.input_tokens, r.output_tokens), (11, 7));
    assert_eq!(r.finish_reason.as_deref(), Some("length"));
}

#[tokio::test]
async fn ollama_think_and_temperature_params_forwarded() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/chat"))
        .and(body_partial_json(
            json!({"think": false, "options": {"temperature": 0.1, "num_predict": -1}}),
        ))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"message": {"content": "x"}})),
        )
        .expect(1)
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let r = c
        .chat(&req(json!({"think": false, "temperature": 0.1})))
        .await
        .unwrap();
    assert_eq!(r.finish_reason.as_deref(), Some("stop"));
    assert_eq!((r.input_tokens, r.output_tokens), (0, 0));
}

#[tokio::test]
async fn ollama_ignores_request_credential_and_sends_no_auth() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"message": {"content": "x"}})),
        )
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let mut r = req(json!({}));
    r.credential = Some(Credential(SecretString::from(
        "sk-should-never-be-sent".to_owned(),
    )));
    c.chat(&r).await.unwrap();
    let got = srv.received_requests().await.unwrap();
    assert_eq!(got.len(), 1);
    assert!(got[0].headers.get("authorization").is_none());
}

#[tokio::test]
async fn ollama_status_errors_carry_no_body() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(429)
                .insert_header("retry-after", "3")
                .set_body_string("secret-upstream-body"),
        )
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let e = c.chat(&req(json!({}))).await.unwrap_err();
    assert_eq!(
        e,
        ConnectorError::Status {
            status: 429,
            retry_after: Some(std::time::Duration::from_secs(3))
        }
    );
    assert!(!format!("{e:?}{e}").contains("secret-upstream-body"));
}

#[tokio::test]
async fn ollama_malformed_reply_is_upstream_error() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"nope": 1})))
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    assert!(matches!(
        c.chat(&req(json!({}))).await,
        Err(ConnectorError::Upstream(_))
    ));
}

#[tokio::test]
async fn ollama_unreachable_is_generic_upstream_error() {
    let c = OllamaConnector::new(cfg("http://127.0.0.1:1")).unwrap();
    let e = c.chat(&req(json!({}))).await.unwrap_err();
    assert_eq!(e, ConnectorError::Upstream("upstream unreachable".into()));
}

#[tokio::test]
async fn ollama_stream_ndjson_to_chunks_with_final_usage() {
    let srv = MockServer::start().await;
    let body = concat!(
        "{\"message\":{\"content\":\"Hel\"},\"done\":false}\n",
        "not json, skipped\n",
        "{\"message\":{\"content\":\"lo\"},\"done\":false}\n",
        "{\"message\":{\"content\":\"\"},\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":5,\"eval_count\":2}\n"
    );
    Mock::given(method("POST"))
        .and(path("/api/chat"))
        .and(body_partial_json(json!({"stream": true})))
        .respond_with(ResponseTemplate::new(200).set_body_raw(body, "application/x-ndjson"))
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let chunks: Vec<StreamChunk> = c
        .chat_stream(&req(json!({})))
        .map(|r| r.unwrap())
        .collect()
        .await;
    assert_eq!(chunks.len(), 3);
    assert_eq!(chunks[0].delta, "Hel");
    assert_eq!(chunks[1].delta, "lo");
    let last = &chunks[2];
    assert_eq!(last.finish_reason.as_deref(), Some("stop"));
    let u = last.usage.as_ref().unwrap();
    assert_eq!((u.input_tokens, u.output_tokens), (5, 2));
}

#[tokio::test]
async fn ollama_stream_http_error_and_early_end_and_inline_error() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "bad"})))
        .respond_with(ResponseTemplate::new(503))
        .mount(&srv)
        .await;
    let c = OllamaConnector::new(cfg(&srv.uri())).unwrap();
    let mut r = req(json!({}));
    r.model = "bad".into();
    let first = c.chat_stream(&r).next().await.unwrap();
    assert_eq!(
        first,
        Err(ConnectorError::Status {
            status: 503,
            retry_after: None
        })
    );

    // Stream that ends without done:true is an error, not a silent success.
    let srv2 = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            "{\"message\":{\"content\":\"a\"}}\n",
            "application/x-ndjson",
        ))
        .mount(&srv2)
        .await;
    let c2 = OllamaConnector::new(cfg(&srv2.uri())).unwrap();
    let items: Vec<_> = c2.chat_stream(&req(json!({}))).collect().await;
    assert_eq!(items.len(), 2);
    assert!(items[0].is_ok());
    assert!(matches!(items[1], Err(ConnectorError::Upstream(_))));

    // An inline {"error":..} frame fails the stream without echoing its text.
    let srv3 = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            "{\"error\":\"secret-model-detail\"}\n",
            "application/x-ndjson",
        ))
        .mount(&srv3)
        .await;
    let c3 = OllamaConnector::new(cfg(&srv3.uri())).unwrap();
    let e = c3
        .chat_stream(&req(json!({})))
        .next()
        .await
        .unwrap()
        .unwrap_err();
    assert!(!format!("{e}").contains("secret-model-detail"));
}

#[tokio::test]
async fn utf8_split_across_chunks_reassembled() {
    // "héllo 🐧" with the 2-byte é and the 4-byte penguin each split across network chunks,
    // and a JSON line itself split mid-way.
    let line1 = "{\"message\":{\"content\":\"héllo 🐧\"},\"done\":false}\n"
        .as_bytes()
        .to_vec();
    let e_pos = line1.iter().position(|b| *b == 0xC3).unwrap() + 1;
    let p_pos = line1.iter().position(|b| *b == 0xF0).unwrap() + 2;
    let line2 =
        b"{\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":1,\"eval_count\":1}\n"
            .to_vec();
    let pieces = vec![
        Piece {
            bytes: line1[..e_pos].to_vec(),
            delay_ms: 0,
        },
        Piece {
            bytes: line1[e_pos..p_pos].to_vec(),
            delay_ms: 30,
        },
        Piece {
            bytes: line1[p_pos..].to_vec(),
            delay_ms: 30,
        },
        Piece {
            bytes: line2,
            delay_ms: 30,
        },
    ];
    let (url, _closed) = raw_server("application/x-ndjson", pieces, After::Finish).await;
    let c = OllamaConnector::new(cfg(&url)).unwrap();
    let text: String = c
        .chat_stream(&req(json!({})))
        .map(|r| r.unwrap().delta)
        .collect::<Vec<_>>()
        .await
        .concat();
    assert_eq!(text, "héllo 🐧");
}

#[tokio::test]
async fn stream_idle_timeout_between_frames_is_timeout_error() {
    let line = b"{\"message\":{\"content\":\"a\"},\"done\":false}\n".to_vec();
    let (url, _closed) = raw_server(
        "application/x-ndjson",
        vec![Piece {
            bytes: line,
            delay_ms: 0,
        }],
        After::HoldUntilClientCloses,
    )
    .await;
    let mut c = cfg(&url);
    c.idle_timeout = std::time::Duration::from_millis(200);
    let c = OllamaConnector::new(c).unwrap();
    let items: Vec<_> = c.chat_stream(&req(json!({}))).collect().await;
    assert_eq!(items.len(), 2);
    assert_eq!(items[0].as_ref().unwrap().delta, "a");
    assert_eq!(items[1], Err(ConnectorError::Timeout));
}

/// Live Ollama (RTX box) — run via `make test-gpu OLLAMA_HOST=...`; ignored in plain CI.
#[tokio::test]
#[ignore = "needs a live Ollama with gemma4:e4b; run via make test-gpu"]
async fn live_ollama_chat_and_stream() {
    let host = std::env::var("OLLAMA_HOST").unwrap_or_else(|_| "http://127.0.0.1:11434".into());
    let host = if host.starts_with("http") {
        host
    } else {
        format!("http://{host}")
    };
    let model = std::env::var("OLLAMA_TEST_MODEL").unwrap_or_else(|_| "gemma4:e4b".into());
    let c = OllamaConnector::new(cfg(&host)).unwrap();
    let mut r = req(json!({"max_tokens": 64, "think": false}));
    r.model = model;
    let resp = c.chat(&r).await.unwrap();
    assert!(
        !resp.text.is_empty() && resp.output_tokens > 0,
        "live chat returned nothing"
    );
    let chunks: Vec<_> = c.chat_stream(&r).map(|x| x.unwrap()).collect().await;
    assert!(chunks.iter().any(|c| !c.delta.is_empty()));
    assert!(chunks.last().unwrap().usage.is_some());
}
