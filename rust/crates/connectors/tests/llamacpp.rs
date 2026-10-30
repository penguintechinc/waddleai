//! llama.cpp connector protocol tests (OpenAI-compatible SSE).

mod common;

use common::cfg;
use connectors::LlamaCppConnector;
use futures_util::StreamExt;
use pipeline::{Connector, ConnectorError, ConnectorRequest};
use secrecy::SecretString;
use serde_json::{json, Value};
use wiremock::matchers::{body_partial_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn req(params: Value) -> ConnectorRequest {
    ConnectorRequest {
        model: "qwen".into(),
        messages: vec![json!({"role": "user", "content": "hi"})],
        params,
        credential: None,
    }
}

#[tokio::test]
async fn llamacpp_chat_maps_request_and_usage() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(body_partial_json(
            json!({"model": "qwen", "stream": false, "max_tokens": 9}),
        ))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "choices": [{"message": {"content": "yo"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2}
        })))
        .expect(1)
        .mount(&srv)
        .await;
    let c = LlamaCppConnector::new(cfg(&srv.uri())).unwrap();
    let r = c.chat(&req(json!({"max_tokens": 9}))).await.unwrap();
    assert_eq!(r.text, "yo");
    assert_eq!((r.input_tokens, r.output_tokens), (4, 2));
    assert_eq!(r.finish_reason.as_deref(), Some("length"));
}

#[tokio::test]
async fn llamacpp_params_cannot_override_fixed_fields_and_default_model_used() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "dflt", "stream": false})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "choices": [{"message": {"content": "ok"}}]
        })))
        .expect(1)
        .mount(&srv)
        .await;
    let mut conf = cfg(&srv.uri());
    conf.default_model = Some("dflt".into());
    let c = LlamaCppConnector::new(conf).unwrap();
    let mut r = req(json!({"model": "evil", "stream": true}));
    r.model = String::new();
    let out = c.chat(&r).await.unwrap();
    assert_eq!(out.finish_reason.as_deref(), Some("stop"));
    assert_eq!((out.input_tokens, out.output_tokens), (0, 0));
}

#[tokio::test]
async fn llamacpp_configured_bearer_sent_only_from_config() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(header("authorization", "Bearer cfg-key"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "choices": [{"message": {"content": "ok"}}]
        })))
        .expect(1)
        .mount(&srv)
        .await;
    let mut conf = cfg(&srv.uri());
    conf.bearer = Some(SecretString::from("cfg-key".to_owned()));
    let c = LlamaCppConnector::new(conf).unwrap();
    c.chat(&req(json!({}))).await.unwrap();
}

#[tokio::test]
async fn llamacpp_empty_choices_and_status_errors() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "qwen"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"choices": []})))
        .mount(&srv)
        .await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "bad"})))
        .respond_with(ResponseTemplate::new(400).set_body_string("secret"))
        .mount(&srv)
        .await;
    let c = LlamaCppConnector::new(cfg(&srv.uri())).unwrap();
    assert!(matches!(
        c.chat(&req(json!({}))).await,
        Err(ConnectorError::Upstream(_))
    ));
    let mut r = req(json!({}));
    r.model = "bad".into();
    assert_eq!(
        c.chat(&r).await.unwrap_err(),
        ConnectorError::Status {
            status: 400,
            retry_after: None
        }
    );
}

#[tokio::test]
async fn llamacpp_sse_to_chunks() {
    let srv = MockServer::start().await;
    let body = concat!(
        "data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"content\":\"Hel\"}}]}\n\n",
        ": comment line\n",
        "data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":3,\"completion_tokens\":2}}\n\n",
        "data: [DONE]\n\n"
    );
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(body_partial_json(json!({"stream": true})))
        .respond_with(ResponseTemplate::new(200).set_body_raw(body, "text/event-stream"))
        .mount(&srv)
        .await;
    let c = LlamaCppConnector::new(cfg(&srv.uri())).unwrap();
    let chunks: Vec<_> = c
        .chat_stream(&req(json!({})))
        .map(|r| r.unwrap())
        .collect()
        .await;
    assert_eq!(chunks.len(), 3);
    assert_eq!(
        (chunks[0].delta.as_str(), chunks[1].delta.as_str()),
        ("Hel", "lo")
    );
    assert_eq!(chunks[2].finish_reason.as_deref(), Some("stop"));
    let u = chunks[2].usage.as_ref().unwrap();
    assert_eq!((u.input_tokens, u.output_tokens), (3, 2));
}

#[tokio::test]
async fn llamacpp_stream_without_done_or_finish_is_error_and_http_error_surfaces() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "qwen"})))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            "data: {\"choices\":[{\"delta\":{\"content\":\"a\"}}]}\n\n",
            "text/event-stream",
        ))
        .mount(&srv)
        .await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "bad"})))
        .respond_with(ResponseTemplate::new(500))
        .mount(&srv)
        .await;
    let c = LlamaCppConnector::new(cfg(&srv.uri())).unwrap();
    let items: Vec<_> = c.chat_stream(&req(json!({}))).collect().await;
    assert_eq!(items.len(), 2);
    assert!(matches!(items[1], Err(ConnectorError::Upstream(_))));
    let mut r = req(json!({}));
    r.model = "bad".into();
    let first = c.chat_stream(&r).next().await.unwrap();
    assert_eq!(
        first,
        Err(ConnectorError::Status {
            status: 500,
            retry_after: None
        })
    );
}
