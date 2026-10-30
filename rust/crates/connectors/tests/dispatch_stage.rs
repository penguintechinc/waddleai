//! DispatchStage + LocalDispatcher behaviour: failover, error mapping, fail-loud gates,
//! depseudonymization, streaming accumulation and disconnect.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use common::{ctx, dispatcher, raw_server, simple, After, Piece};
use connectors::{DispatchStage, LocalDispatcher};
use futures_util::StreamExt;
use pipeline::{
    AbortSnapshot, AuthMode, ChunkTransform, Depseudonymizer, DispatchError, Dispatcher,
    IdentityDepseudonymizer, PseudonymMap, Stage, StageError,
};
use routing::RoutingMeta;
use secrecy::SecretString;
use serde_json::json;
use wiremock::matchers::{body_partial_json, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

fn ok_body(text: &str) -> serde_json::Value {
    json!({"message": {"content": text}, "done": true, "done_reason": "stop",
           "prompt_eval_count": 6, "eval_count": 4})
}

async fn mock_model(srv: &MockServer, model: &str, resp: ResponseTemplate) {
    Mock::given(method("POST"))
        .and(path("/api/chat"))
        .and(body_partial_json(json!({"model": model})))
        .respond_with(resp)
        .mount(srv)
        .await;
}

fn stage_over(d: LocalDispatcher) -> DispatchStage {
    DispatchStage::new(Arc::new(d))
}

fn meta(executable: bool) -> RoutingMeta {
    RoutingMeta {
        executable,
        commercial_models: Vec::new(),
        executable_fallbacks: Vec::new(),
        provider_hint: None,
        unclassified_default: false,
    }
}

#[tokio::test]
async fn dispatch_sets_provider_model_finish_reason() {
    let srv = MockServer::start().await;
    mock_model(
        &srv,
        "gemma4:e4b",
        ResponseTemplate::new(200).set_body_json(ok_body("hello")),
    )
    .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("gemma4:e4b");
    c.body = json!({"max_tokens": 32});
    assert_eq!(stage.name(), "dispatch");
    stage.call(&mut c).await.unwrap();
    assert!(!c.blocked);
    assert_eq!(c.provider.as_deref(), Some("ollama"));
    assert_eq!(c.model.as_deref(), Some("gemma4:e4b"));
    assert_eq!(c.requested_model.as_deref(), Some("gemma4:e4b"));
    assert_eq!(c.finish_reason.as_deref(), Some("stop"));
    assert_eq!(c.response_text, "hello");
    let u = c.usage.unwrap();
    assert_eq!((u.input_tokens, u.output_tokens), (6, 4));
    let body: serde_json::Value =
        serde_json::from_slice(&srv.received_requests().await.unwrap()[0].body).unwrap();
    assert_eq!(body["options"]["num_predict"], 32);
}

#[tokio::test]
async fn provider_prefix_selects_llamacpp_and_routing_hint_is_advisory() {
    let ollama = MockServer::start().await;
    let llama = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .and(body_partial_json(json!({"model": "qwen"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "choices": [{"message": {"content": "from-llama"}}]
        })))
        .expect(2)
        .mount(&llama)
        .await;
    let stage = stage_over(dispatcher(
        &ollama.uri(),
        Some(&llama.uri()),
        Arc::new(IdentityDepseudonymizer),
    ));
    let mut c = ctx("llamacpp:qwen");
    stage.call(&mut c).await.unwrap();
    assert_eq!(
        (c.provider.as_deref(), c.model.as_deref()),
        (Some("llamacpp"), Some("qwen"))
    );
    assert_eq!(c.response_text, "from-llama");

    // A bare model with an advisory hint naming a registered local provider uses that provider.
    let mut c2 = ctx("qwen");
    let mut m = meta(true);
    m.provider_hint = Some("llamacpp".into());
    c2.extensions.insert(m);
    stage.call(&mut c2).await.unwrap();
    assert_eq!(c2.provider.as_deref(), Some("llamacpp"));

    // A commercial hint on a bare local model is ignored (advisory only), default provider used.
    let mut c3 = ctx("gemma");
    let mut m = meta(true);
    m.provider_hint = Some("openai".into());
    c3.extensions.insert(m);
    mock_model(
        &ollama,
        "gemma",
        ResponseTemplate::new(200).set_body_json(ok_body("x")),
    )
    .await;
    stage.call(&mut c3).await.unwrap();
    assert_eq!(c3.provider.as_deref(), Some("ollama"));
}

#[tokio::test]
async fn upstream_5xx_retryable_fails_over_to_next_chain_entry() {
    let srv = MockServer::start().await;
    mock_model(&srv, "bad", ResponseTemplate::new(500)).await;
    mock_model(
        &srv,
        "good",
        ResponseTemplate::new(200).set_body_json(ok_body("rescued")),
    )
    .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("bad");
    c.fallback_chain = vec!["good".into()];
    stage.call(&mut c).await.unwrap();
    assert!(!c.blocked, "failover must succeed: {:?}", c.block_reason);
    assert_eq!(c.model.as_deref(), Some("good"));
    assert_eq!(c.requested_model.as_deref(), Some("bad"));
    assert_eq!(c.response_text, "rescued");
    assert_eq!(srv.received_requests().await.unwrap().len(), 2);
}

#[tokio::test]
async fn upstream_429_with_retry_after_fails_over_and_exhaustion_maps_status() {
    let srv = MockServer::start().await;
    mock_model(
        &srv,
        "limited",
        ResponseTemplate::new(429).insert_header("retry-after", "0"),
    )
    .await;
    mock_model(
        &srv,
        "good",
        ResponseTemplate::new(200).set_body_json(ok_body("ok")),
    )
    .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("limited");
    c.fallback_chain = vec!["good".into()];
    stage.call(&mut c).await.unwrap();
    assert!(!c.blocked);

    // All entries 429 -> the last mapped error (429) is returned.
    let mut c2 = ctx("limited");
    c2.fallback_chain = vec!["limited".into()];
    stage.call(&mut c2).await.unwrap();
    assert_eq!((c2.blocked, c2.status_code), (true, 429));
    assert_eq!(c2.block_reason.as_deref(), Some("provider_error_429"));
}

#[tokio::test]
async fn upstream_4xx_not_retried() {
    let srv = MockServer::start().await;
    mock_model(&srv, "bad", ResponseTemplate::new(404)).await;
    Mock::given(method("POST"))
        .and(body_partial_json(json!({"model": "good"})))
        .respond_with(ResponseTemplate::new(200).set_body_json(ok_body("no")))
        .expect(0)
        .mount(&srv)
        .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("bad");
    c.fallback_chain = vec!["good".into()];
    stage.call(&mut c).await.unwrap();
    assert!(c.blocked);
    assert_eq!(c.status_code, 404);
    assert_eq!(c.block_reason.as_deref(), Some("provider_error_404"));
    assert_eq!(srv.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn timeout_and_transport_errors_map_to_504_and_502() {
    let stage = stage_over(simple("http://127.0.0.1:1"));
    let mut c = ctx("m");
    stage.call(&mut c).await.unwrap();
    assert_eq!((c.blocked, c.status_code), (true, 502));
    assert_eq!(c.block_reason.as_deref(), Some("provider_error"));

    // A server that never answers trips the per-request timeout -> 504.
    let (url, _closed) = raw_server("application/json", vec![], After::HoldUntilClientCloses).await;
    let mut conf = common::cfg(&url);
    conf.request_timeout = std::time::Duration::from_millis(150);
    let mut reg = connectors::ConnectorRegistry::new();
    reg.register(Arc::new(connectors::OllamaConnector::new(conf).unwrap()));
    let d = LocalDispatcher::new(
        Arc::new(reg),
        Arc::new(IdentityDepseudonymizer),
        Default::default(),
    );
    let mut c2 = ctx("m");
    stage_over(d).call(&mut c2).await.unwrap();
    assert_eq!((c2.blocked, c2.status_code), (true, 504));
    assert_eq!(c2.block_reason.as_deref(), Some("provider_error_504"));
}

#[tokio::test]
async fn commercial_provider_unsupported_loud() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&srv)
        .await;
    let stage = stage_over(simple(&srv.uri()));

    let mut prefixed = ctx("openai:gpt-4");
    assert_eq!(
        stage.call(&mut prefixed).await,
        Err(StageError::Unsupported("provider: P3"))
    );

    let mut routed = ctx("gpt-4");
    let mut m = meta(true);
    m.commercial_models = vec!["gpt-4".into()];
    routed.extensions.insert(m);
    assert_eq!(
        stage.call(&mut routed).await,
        Err(StageError::Unsupported("provider: P3"))
    );
    assert!(!routed.blocked, "fail loud via Err, not a silent block");
}

#[tokio::test]
async fn non_executable_routing_result_is_unsupported_even_with_local_hint() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&srv)
        .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("gemma");
    let mut m = meta(false);
    m.provider_hint = Some("ollama".into());
    c.extensions.insert(m);
    assert_eq!(
        stage.call(&mut c).await,
        Err(StageError::Unsupported("provider: P3"))
    );
}

#[tokio::test]
async fn passthrough_mode_unsupported_loud() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&srv)
        .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("gemma4:e4b");
    c.auth_mode = AuthMode::Passthrough;
    c.passthrough_credential = Some(SecretString::from("sk-caller-key".to_owned()));
    assert_eq!(
        stage.call(&mut c).await,
        Err(StageError::Unsupported("passthrough dispatch: P3"))
    );
    assert!(stage.stream(&mut c).is_err());
}

#[tokio::test]
async fn dispatch_early_returns_on_cache_hit_and_blocked() {
    let srv = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200))
        .expect(0)
        .mount(&srv)
        .await;
    let stage = stage_over(simple(&srv.uri()));

    let mut hit = ctx("gemma");
    hit.cache_hit = true;
    hit.response_text = "cached".into();
    // Even a would-be-unsupported pick is skipped on a hit.
    hit.auth_mode = AuthMode::Passthrough;
    stage.call(&mut hit).await.unwrap();
    assert_eq!(hit.response_text, "cached");
    assert!(hit.provider.is_none());
    assert!(stage.stream(&mut hit).unwrap().next().await.is_none());

    let mut blocked = ctx("gemma");
    blocked.blocked = true;
    blocked.status_code = 403;
    stage.call(&mut blocked).await.unwrap();
    assert!(blocked.provider.is_none());
    assert_eq!(blocked.status_code, 403);
    assert!(stage.stream(&mut blocked).unwrap().next().await.is_none());
}

#[tokio::test]
async fn no_messages_no_model_and_missing_connector_are_mapped() {
    let stage = stage_over(simple("http://127.0.0.1:1"));
    let mut empty = ctx("m");
    empty.messages.clear();
    stage.call(&mut empty).await.unwrap();
    assert_eq!((empty.blocked, empty.status_code), (true, 400));
    assert_eq!(empty.block_reason.as_deref(), Some("no_messages"));

    let mut nomodel = ctx("m");
    nomodel.model = None;
    stage.call(&mut nomodel).await.unwrap();
    assert_eq!(
        (nomodel.status_code, nomodel.block_reason.as_deref()),
        (503, Some("no_available_providers"))
    );

    // llamacpp prefix with only ollama registered -> no connector (500).
    let mut nc = ctx("llamacpp:qwen");
    stage.call(&mut nc).await.unwrap();
    assert_eq!(
        (nc.status_code, nc.block_reason.as_deref()),
        (500, Some("no_connector"))
    );

    // Same on the streaming path, and for an empty streaming request.
    let mut ncs = ctx("llamacpp:qwen");
    ncs.stream = true;
    stage.call(&mut ncs).await.unwrap();
    assert_eq!(
        (ncs.status_code, ncs.block_reason.as_deref()),
        (500, Some("no_connector"))
    );
    let mut es = ctx("m");
    es.messages.clear();
    es.stream = true;
    stage.call(&mut es).await.unwrap();
    assert_eq!(es.block_reason.as_deref(), Some("no_messages"));
}

/// Replaces `<P1>` with `Alice`, counts calls; proves the hook is wired and applied.
struct Spy {
    texts: AtomicUsize,
    streams: AtomicUsize,
}

struct SpyTransform;

impl ChunkTransform for SpyTransform {
    fn push(&mut self, chunk: &str) -> String {
        chunk.replace("<P1>", "Alice")
    }
    fn finish(&mut self) -> String {
        "!".to_owned()
    }
}

impl Depseudonymizer for Spy {
    fn restore_text(&self, _m: &PseudonymMap, s: &str) -> String {
        self.texts.fetch_add(1, Ordering::SeqCst);
        s.replace("<P1>", "Alice")
    }
    fn restore_stream(&self, _m: &PseudonymMap) -> Box<dyn ChunkTransform> {
        self.streams.fetch_add(1, Ordering::SeqCst);
        Box::new(SpyTransform)
    }
}

#[tokio::test]
async fn depseudonymize_hook_called_identity_in_p2() {
    let srv = MockServer::start().await;
    mock_model(
        &srv,
        "m",
        ResponseTemplate::new(200).set_body_json(ok_body("hi <P1>")),
    )
    .await;

    // P2 impl is the identity: text is untouched.
    let mut c = ctx("m");
    stage_over(simple(&srv.uri())).call(&mut c).await.unwrap();
    assert_eq!(c.response_text, "hi <P1>");

    // The hook is invoked on the response and its output is what lands on the context.
    let spy = Arc::new(Spy {
        texts: AtomicUsize::new(0),
        streams: AtomicUsize::new(0),
    });
    let stage = stage_over(dispatcher(&srv.uri(), None, spy.clone()));
    let mut c2 = ctx("m");
    stage.call(&mut c2).await.unwrap();
    assert_eq!(spy.texts.load(Ordering::SeqCst), 1);
    assert_eq!(c2.response_text, "hi Alice");
}

fn ndjson_pieces(lines: &[&str]) -> Vec<Piece> {
    lines
        .iter()
        .map(|l| Piece {
            bytes: format!("{l}\n").into_bytes(),
            delay_ms: 10,
        })
        .collect()
}

#[tokio::test]
async fn stream_dispatch_accumulates_response_text_and_usage_for_security_out_and_meter() {
    let srv = MockServer::start().await;
    let body = concat!(
        "{\"message\":{\"content\":\"Hel\"},\"done\":false}\n",
        "{\"message\":{\"content\":\"lo\"},\"done\":false}\n",
        "{\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":5,\"eval_count\":2}\n"
    );
    mock_model(
        &srv,
        "m",
        ResponseTemplate::new(200).set_body_raw(body, "application/x-ndjson"),
    )
    .await;
    let d = simple(&srv.uri());
    let mut c = ctx("m");
    c.stream = true;
    let chunks: Vec<_> = d
        .stream_dispatch(&mut c)
        .map(|r| r.unwrap())
        .collect()
        .await;
    let deltas: Vec<_> = chunks.iter().map(|c| c.delta.as_str()).collect();
    assert_eq!(deltas, ["Hel", "lo", ""]);
    assert_eq!(
        chunks.last().unwrap().finish_reason.as_deref(),
        Some("stop")
    );
    assert_eq!(c.response_text, "Hello");
    let u = c.usage.clone().unwrap();
    assert_eq!((u.input_tokens, u.output_tokens), (5, 2));
    assert_eq!(c.finish_reason.as_deref(), Some("stop"));
    assert_eq!(c.provider.as_deref(), Some("ollama"));
    assert!(!c.blocked);

    // Through the stage (ctx.stream): consumed silently, ctx in the same final state.
    let stage = stage_over(simple(&srv.uri()));
    let mut c2 = ctx("m");
    c2.stream = true;
    stage.call(&mut c2).await.unwrap();
    assert_eq!(c2.response_text, "Hello");
    assert_eq!(c2.usage.unwrap().output_tokens, 2);
}

#[tokio::test]
async fn stream_applies_depseudonymizer_per_chunk_and_on_the_final_text() {
    let srv = MockServer::start().await;
    let body = concat!(
        "{\"message\":{\"content\":\"hi <P1>\"},\"done\":false}\n",
        "{\"done\":true,\"done_reason\":\"stop\",\"prompt_eval_count\":1,\"eval_count\":1}\n"
    );
    mock_model(
        &srv,
        "m",
        ResponseTemplate::new(200).set_body_raw(body, "application/x-ndjson"),
    )
    .await;
    let spy = Arc::new(Spy {
        texts: AtomicUsize::new(0),
        streams: AtomicUsize::new(0),
    });
    let d = dispatcher(&srv.uri(), None, spy.clone());
    let mut c = ctx("m");
    c.stream = true;
    let deltas: Vec<String> = d
        .stream_dispatch(&mut c)
        .map(|r| r.unwrap().delta)
        .collect()
        .await;
    assert_eq!(deltas, ["hi Alice", "!", ""]);
    assert_eq!(c.response_text, "hi Alice");
    assert_eq!(spy.streams.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn stream_fails_over_before_first_chunk_but_not_after() {
    let srv = MockServer::start().await;
    mock_model(&srv, "bad", ResponseTemplate::new(503)).await;
    let good = concat!(
        "{\"message\":{\"content\":\"ok\"},\"done\":false}\n",
        "{\"done\":true,\"done_reason\":\"stop\"}\n"
    );
    mock_model(
        &srv,
        "good",
        ResponseTemplate::new(200).set_body_raw(good, "application/x-ndjson"),
    )
    .await;
    let d = simple(&srv.uri());
    let mut c = ctx("bad");
    c.stream = true;
    c.fallback_chain = vec!["good".into()];
    let items: Vec<_> = d.stream_dispatch(&mut c).collect().await;
    assert!(items.iter().all(Result::is_ok));
    assert_eq!(
        (c.response_text.as_str(), c.model.as_deref()),
        ("ok", Some("good"))
    );
    assert!(
        c.usage.is_none(),
        "no counts reported -> None so quota estimates locally"
    );

    // Mid-stream failure after a chunk was already sent is NOT failed over.
    let (url, _c) = raw_server(
        "application/x-ndjson",
        ndjson_pieces(&["{\"message\":{\"content\":\"par\"},\"done\":false}"]),
        After::Finish, // ends without done:true -> upstream error after the first chunk
    )
    .await;
    let d2 = simple(&url);
    let mut c2 = ctx("m");
    c2.stream = true;
    c2.fallback_chain = vec!["m2".into()];
    let items: Vec<_> = d2.stream_dispatch(&mut c2).collect().await;
    assert_eq!(items.len(), 2);
    assert!(matches!(
        &items[1],
        Err(DispatchError::Upstream {
            status: 502,
            retryable: true,
            ..
        })
    ));
    assert!(c2.blocked);
    assert_eq!(c2.response_text, "par");
}

#[tokio::test]
async fn stream_client_disconnect_cancels_upstream_and_settles_partial_usage() {
    let (url, closed) = raw_server(
        "application/x-ndjson",
        ndjson_pieces(&[
            "{\"message\":{\"content\":\"one \"},\"done\":false}",
            "{\"message\":{\"content\":\"two \"},\"done\":false}",
        ]),
        After::HoldUntilClientCloses,
    )
    .await;
    let d = simple(&url);
    let mut c = ctx("m");
    c.stream = true;
    {
        let mut s = d.stream_dispatch(&mut c);
        let first = s.next().await.unwrap().unwrap();
        assert_eq!(first.delta, "one ");
        // Client disconnects here: the consumer drops the stream mid-flight.
    }
    tokio::time::timeout(std::time::Duration::from_secs(5), closed)
        .await
        .expect("upstream connection was not cancelled after the consumer dropped the stream")
        .unwrap();
    assert_eq!(c.response_text, "one ", "partial text kept for settlement");
    assert!(c.usage.is_none());
    let snap = AbortSnapshot::from_ctx(&c);
    assert!(
        snap.consumed(),
        "partial output must be charged (settle_aborted), not refunded"
    );
    assert_eq!(snap.provider.as_deref(), Some("ollama"));
}

#[tokio::test]
async fn dispatch_does_not_inject_credentials_for_local() {
    let srv = MockServer::start().await;
    mock_model(
        &srv,
        "m",
        ResponseTemplate::new(200).set_body_json(ok_body("x")),
    )
    .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("m");
    // A passthrough key present on the context must never be forwarded in P2.
    c.passthrough_credential = Some(SecretString::from("sk-leak-me-not".to_owned()));
    stage.call(&mut c).await.unwrap();
    let reqs: Vec<Request> = srv.received_requests().await.unwrap();
    assert_eq!(reqs.len(), 1);
    assert!(reqs[0].headers.get("authorization").is_none());
    assert!(reqs[0].headers.get("x-api-key").is_none());
    assert!(!String::from_utf8_lossy(&reqs[0].body).contains("sk-leak-me-not"));
}

#[tokio::test]
async fn stage_stream_entry_yields_dispatcher_chunks() {
    let srv = MockServer::start().await;
    let body = "{\"message\":{\"content\":\"a\"},\"done\":false}\n{\"done\":true,\"done_reason\":\"stop\"}\n";
    mock_model(
        &srv,
        "m",
        ResponseTemplate::new(200).set_body_raw(body, "application/x-ndjson"),
    )
    .await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("m");
    c.stream = true;
    let n = stage.stream(&mut c).unwrap().count().await;
    assert_eq!(n, 2);
    assert_eq!(c.response_text, "a");
}

#[tokio::test]
async fn chain_skips_non_local_prefixed_entries() {
    let srv = MockServer::start().await;
    mock_model(&srv, "bad", ResponseTemplate::new(500)).await;
    let stage = stage_over(simple(&srv.uri()));
    let mut c = ctx("bad");
    c.fallback_chain = vec!["openai:gpt-4".into()];
    stage.call(&mut c).await.unwrap();
    // The commercial entry is never attempted: only the primary is hit, exhaustion -> 502.
    assert_eq!((c.blocked, c.status_code), (true, 502));
    assert_eq!(srv.received_requests().await.unwrap().len(), 1);
}
