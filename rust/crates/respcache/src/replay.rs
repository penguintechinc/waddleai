//! Synthetic-SSE replay of cached responses (port of `shared/cache/replay.py`).
//!
//! A streaming request that hits the cache must be indistinguishable from a streaming miss: same
//! framing, content and final usage accounting, with no sleeps between chunks. The cached
//! (non-streaming) client-facing body is decomposed into the requesting endpoint's own SSE event
//! sequence. The event sequence and framing bytes equal Python's; object key order inside a JSON
//! payload is serde_json's (sorted), which SSE clients do not depend on.

use std::time::{SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use futures_core::stream::BoxStream;
use serde_json::{json, Map, Value};

use crate::exact::{py_truthy, CachedResponse};

/// Characters per synthetic content delta (arbitrary but deterministic, as in Python).
const CHUNK_CHARS: usize = 24;

fn chunk_text(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    chars
        .chunks(CHUNK_CHARS)
        .map(|c| c.iter().collect())
        .collect()
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

fn into_stream(frames: Vec<Bytes>) -> BoxStream<'static, Result<Bytes, std::io::Error>> {
    Box::pin(futures_util::stream::iter(frames.into_iter().map(Ok)))
}

fn data_frame(v: &Value) -> Bytes {
    let mut out = b"data: ".to_vec();
    out.extend_from_slice(v.to_string().as_bytes());
    out.extend_from_slice(b"\n\n");
    Bytes::from(out)
}

fn event_frame(event: &str, data: &Value) -> Bytes {
    let mut out = format!("event: {event}\ndata: ").into_bytes();
    out.extend_from_slice(data.to_string().as_bytes());
    out.extend_from_slice(b"\n\n");
    Bytes::from(out)
}

/// Replays a cached OpenAI `chat.completion` as SSE: one role-only chunk, N content-delta chunks,
/// one final chunk carrying `finish_reason` and (if present) `usage`, then `data: [DONE]`.
pub fn replay_openai_sse(
    cached: &CachedResponse,
) -> BoxStream<'static, Result<Bytes, std::io::Error>> {
    let body = &cached.response;
    let choice = body
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|c| c.first())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let content = choice
        .pointer("/message/content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let finish_reason = choice.get("finish_reason").cloned().unwrap_or(Value::Null);
    let id = body
        .get("id")
        .filter(|v| py_truthy(v))
        .cloned()
        .unwrap_or_else(|| json!(format!("chatcmpl-{}", now_secs())));
    let created = body
        .get("created")
        .cloned()
        .unwrap_or_else(|| json!(now_secs()));
    let model = body.get("model").cloned().unwrap_or_else(|| json!(""));
    let usage = body.get("usage").filter(|u| !u.is_null()).cloned();

    let frame = |delta: Value, finish: Value, include_usage: bool| -> Bytes {
        let mut chunk = Map::new();
        chunk.insert("id".into(), id.clone());
        chunk.insert("object".into(), json!("chat.completion.chunk"));
        chunk.insert("created".into(), created.clone());
        chunk.insert("model".into(), model.clone());
        chunk.insert(
            "choices".into(),
            json!([{"index": 0, "delta": delta, "finish_reason": finish}]),
        );
        if include_usage {
            if let Some(u) = &usage {
                chunk.insert("usage".into(), u.clone());
            }
        }
        data_frame(&Value::Object(chunk))
    };

    let mut frames = vec![frame(json!({"role": "assistant"}), Value::Null, false)];
    for piece in chunk_text(&content) {
        frames.push(frame(json!({"content": piece}), Value::Null, false));
    }
    frames.push(frame(json!({}), finish_reason, true));
    frames.push(Bytes::from_static(b"data: [DONE]\n\n"));
    into_stream(frames)
}

/// Replays a cached Anthropic Messages response as the
/// `message_start -> content_block_start -> content_block_delta* -> content_block_stop ->
/// message_delta -> message_stop` sequence, including `input_json_delta` framing for `tool_use`
/// blocks, in the order the blocks appear.
pub fn replay_anthropic_sse(
    cached: &CachedResponse,
) -> BoxStream<'static, Result<Bytes, std::io::Error>> {
    let body = &cached.response;
    let id = body
        .get("id")
        .filter(|v| py_truthy(v))
        .cloned()
        .unwrap_or_else(|| json!(format!("msg_{}", now_secs() * 1000)));
    let model = body.get("model").cloned().unwrap_or_else(|| json!(""));
    let stop_reason = body.get("stop_reason").cloned().unwrap_or(Value::Null);
    let stop_sequence = body.get("stop_sequence").cloned().unwrap_or(Value::Null);
    let usage = body
        .get("usage")
        .filter(|u| py_truthy(u))
        .cloned()
        .unwrap_or_else(|| json!({}));
    let blocks = body
        .get("content")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    let mut frames = vec![event_frame(
        "message_start",
        &json!({"type": "message_start", "message": {
            "id": id, "type": "message", "role": "assistant", "content": [],
            "model": model, "stop_reason": null, "stop_sequence": null,
            "usage": {"input_tokens": usage.get("input_tokens").cloned().unwrap_or(json!(0)),
                      "output_tokens": 0}}}),
    )];

    for (index, block) in blocks.iter().enumerate() {
        if block.get("type").and_then(Value::as_str) == Some("tool_use") {
            frames.push(event_frame(
                "content_block_start",
                &json!({"type": "content_block_start", "index": index, "content_block": {
                    "type": "tool_use", "id": block.get("id").cloned().unwrap_or(Value::Null),
                    "name": block.get("name").cloned().unwrap_or(Value::Null), "input": {}}}),
            ));
            let input = block
                .get("input")
                .filter(|v| py_truthy(v))
                .cloned()
                .unwrap_or_else(|| json!({}));
            for piece in chunk_text(&input.to_string()) {
                frames.push(event_frame(
                    "content_block_delta",
                    &json!({"type": "content_block_delta", "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": piece}}),
                ));
            }
        } else {
            // "text" and any unrecognized type replay as text, so an odd block shape degrades to
            // its text rather than being silently dropped.
            let text = block.get("text").and_then(Value::as_str).unwrap_or("");
            frames.push(event_frame(
                "content_block_start",
                &json!({"type": "content_block_start", "index": index,
                        "content_block": {"type": "text", "text": ""}}),
            ));
            for piece in chunk_text(text) {
                frames.push(event_frame(
                    "content_block_delta",
                    &json!({"type": "content_block_delta", "index": index,
                            "delta": {"type": "text_delta", "text": piece}}),
                ));
            }
        }
        frames.push(event_frame(
            "content_block_stop",
            &json!({"type": "content_block_stop", "index": index}),
        ));
    }

    frames.push(event_frame(
        "message_delta",
        &json!({"type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
                "usage": usage}),
    ));
    frames.push(event_frame(
        "message_stop",
        &json!({"type": "message_stop"}),
    ));
    into_stream(frames)
}

#[cfg(test)]
mod tests {
    use futures_util::StreamExt;

    use super::*;

    async fn collect(s: BoxStream<'static, Result<Bytes, std::io::Error>>) -> Vec<String> {
        s.map(|b| String::from_utf8(b.unwrap().to_vec()).unwrap())
            .collect()
            .await
    }

    fn cached(v: Value) -> CachedResponse {
        CachedResponse {
            usage: v.get("usage").cloned().unwrap_or(json!({})),
            response: v,
            stored_at: 0.0,
        }
    }

    #[test]
    fn chunking_is_by_characters_not_bytes() {
        assert!(chunk_text("").is_empty());
        let s = "é".repeat(25);
        let c = chunk_text(&s);
        assert_eq!(c.len(), 2);
        assert_eq!(c[0].chars().count(), 24);
        assert_eq!(c[1].chars().count(), 1);
    }

    #[tokio::test]
    async fn openai_empty_content_and_missing_fields_still_frame_correctly() {
        let f = collect(replay_openai_sse(&cached(json!({})))).await;
        // role chunk, final chunk, [DONE]; no content chunks; id/created synthesized
        assert_eq!(f.len(), 3);
        assert!(f[0].contains("\"role\":\"assistant\"") && f[0].contains("chatcmpl-"));
        assert!(f[1].contains("\"finish_reason\":null") && !f[1].contains("usage"));
        assert_eq!(f[2], "data: [DONE]\n\n");
    }

    #[tokio::test]
    async fn openai_usage_only_on_the_final_chunk() {
        let f = collect(replay_openai_sse(&cached(json!({
            "id": "x", "created": 1, "model": "m",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3}
        }))))
        .await;
        assert_eq!(f.len(), 4);
        assert!(f[..2].iter().all(|s| !s.contains("usage")));
        assert!(f[2].contains("\"usage\":{\"total_tokens\":3}") && f[2].contains("\"stop\""));
    }

    #[tokio::test]
    async fn anthropic_unknown_block_degrades_to_text_and_empty_input_is_object() {
        let f = collect(replay_anthropic_sse(&cached(json!({
            "content": [
                {"type": "thinking", "text": "hmm"},
                {"type": "tool_use", "id": "t", "name": "n", "input": {}},
                {"type": "text"}
            ]
        }))))
        .await;
        let joined = f.join("");
        assert!(joined.contains("\"text_delta\"") && joined.contains("hmm"));
        assert!(joined.contains("\"partial_json\":\"{}\""));
        assert!(joined.contains("msg_"), "id synthesized");
        assert!(f.last().unwrap().starts_with("event: message_stop"));
        // blocks 0..=2 each get exactly one stop event
        assert_eq!(joined.matches("event: content_block_stop").count(), 3);
    }
}
