//! `CacheStage`: response cache lookup (spec 6), after `security_in`, before routing/dispatch.
//!
//! Flag-gated on `waddleai.response_cache` (default OFF: the driver skips the stage entirely, so
//! flag-off makes zero Valkey/Postgres calls). A hit populates the context so quota and dispatch
//! skip (`provider = "cache"`, `cache_hit`); a miss stashes the write-back for the caller to commit
//! after `security_out` passes.

use std::sync::Arc;

use pipeline::{CacheStatus, PipelineContext, ResponseFormat, Stage, StageError, Usage};
use serde_json::{Map, Value};

use crate::replay::{replay_anthropic_sse, replay_openai_sse};
use crate::response_cache::ResponseCache;

/// PostHog flag gating the response cache (default OFF).
pub const RESPONSE_CACHE_FLAG: &str = "waddleai.response_cache";

/// Cache lookup stage.
pub struct CacheStage {
    cache: Arc<ResponseCache>,
}

impl CacheStage {
    /// Stage over a configured [`ResponseCache`].
    pub fn new(cache: Arc<ResponseCache>) -> Self {
        Self { cache }
    }
}

/// `(text, finish_reason)` from a cached full response body, by wire format.
fn extract_cached_text(response: &Value, format: ResponseFormat) -> (String, Option<String>) {
    match format {
        ResponseFormat::Anthropic => {
            let text = response
                .pointer("/content/0/text")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let stop = response
                .get("stop_reason")
                .and_then(Value::as_str)
                .map(str::to_owned);
            (text, stop)
        }
        ResponseFormat::Openai => {
            let text = response
                .pointer("/choices/0/message/content")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let finish = response
                .pointer("/choices/0/finish_reason")
                .and_then(Value::as_str)
                .map(str::to_owned);
            (text, finish)
        }
    }
}

fn count(usage: &Value, key: &str) -> u64 {
    usage.get(key).and_then(Value::as_u64).unwrap_or(0)
}

/// Best-effort total token count from either wire format's usage block.
fn cached_total_tokens(usage: &Value) -> u64 {
    if usage.get("total_tokens").is_some() {
        return count(usage, "total_tokens");
    }
    count(usage, "input_tokens") + count(usage, "output_tokens")
}

/// The context `Usage` for a cached usage block: input/output normalized across both wire
/// formats, every other key kept in `extra`.
fn usage_of(usage: &Value) -> Usage {
    let pick = |a: &str, b: &str| {
        usage
            .get(a)
            .and_then(Value::as_u64)
            .or_else(|| usage.get(b).and_then(Value::as_u64))
            .unwrap_or(0)
    };
    let extra: Map<String, Value> = usage
        .as_object()
        .map(|o| {
            o.iter()
                .filter(|(k, _)| !matches!(k.as_str(), "input_tokens" | "output_tokens"))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect()
        })
        .unwrap_or_default();
    Usage {
        input_tokens: pick("input_tokens", "prompt_tokens"),
        output_tokens: pick("output_tokens", "completion_tokens"),
        extra,
    }
}

#[async_trait::async_trait]
impl Stage for CacheStage {
    fn name(&self) -> &'static str {
        "cache"
    }

    fn flag(&self) -> Option<&'static str> {
        Some(RESPONSE_CACHE_FLAG)
    }

    /// Looks up the cache and populates the context from a hit; on a miss stashes the write-back
    /// and the affinity hint. Cache faults never fail the request (see `ResponseCache`).
    async fn call(&self, ctx: &mut PipelineContext) -> Result<(), StageError> {
        let result = self.cache.lookup(ctx).await;
        let metrics = self.cache.metrics();

        if let (CacheStatus::Exact | CacheStatus::Semantic, Some(cached)) =
            (result.status, result.cached.as_ref())
        {
            let layer = if result.status == CacheStatus::Exact {
                "exact"
            } else {
                "semantic"
            };
            metrics.lookup(layer, "hit");
            let (text, finish) = extract_cached_text(&cached.response, ctx.response_format);
            let usage_block = cached.response.get("usage").cloned().unwrap_or(Value::Null);
            ctx.response_text = text;
            ctx.finish_reason = finish;
            ctx.usage = Some(usage_of(&usage_block));
            ctx.provider = Some("cache".to_owned());
            ctx.cache_hit = true;
            ctx.cache_status = result.status;
            ctx.tokens_saved = cached_total_tokens(&usage_block);
            metrics.tokens_saved(layer, ctx.tokens_saved);
            if ctx.stream {
                ctx.stream_iter = Some(match ctx.response_format {
                    ResponseFormat::Anthropic => replay_anthropic_sse(cached),
                    ResponseFormat::Openai => replay_openai_sse(cached),
                });
            }
            tracing::debug!(layer, "cache hit");
            return Ok(());
        }

        metrics.lookup("exact", "miss");
        ctx.cache_status = CacheStatus::Miss;
        ctx.cache_write_back = result.write_back;
        self.cache.annotate_miss(ctx).await;
        Ok(())
    }
}

/// Commits the stashed write-back after the pipeline finished. Call this only once the whole
/// pipeline, including `security_out`, ran: a blocked, errored or cache-served request is never
/// written. Resolves to whether a write was committed. Not an `async fn` so the future stays
/// `Send` (it holds the write-back handle, not the non-`Sync` context).
pub fn commit_write_back<'a>(
    ctx: &PipelineContext,
    response: &'a Value,
    usage: &'a Value,
) -> impl std::future::Future<Output = bool> + Send + 'a {
    let eligible = !(ctx.blocked || ctx.status_code >= 400 || ctx.cache_hit);
    let wb = ctx
        .cache_write_back
        .clone()
        .filter(|w| eligible && w.sink.is_some());
    async move {
        match wb {
            Some(wb) => {
                wb.commit(response, usage).await;
                true
            }
            None => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn usage_normalizes_both_wire_formats() {
        let o = usage_of(&json!({"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}));
        assert_eq!((o.input_tokens, o.output_tokens), (5, 9));
        assert_eq!(o.extra.get("total_tokens"), Some(&json!(14)));
        let a = usage_of(&json!({"input_tokens": 11, "output_tokens": 22}));
        assert_eq!((a.input_tokens, a.output_tokens), (11, 22));
        assert!(a.extra.is_empty());
        let none = usage_of(&Value::Null);
        assert_eq!((none.input_tokens, none.output_tokens), (0, 0));
    }

    #[test]
    fn total_tokens_prefers_total_then_sums() {
        assert_eq!(
            cached_total_tokens(&json!({"total_tokens": 14, "input_tokens": 1})),
            14
        );
        assert_eq!(
            cached_total_tokens(&json!({"input_tokens": 11, "output_tokens": 22})),
            33
        );
        assert_eq!(cached_total_tokens(&json!({"total_tokens": null})), 0);
        assert_eq!(cached_total_tokens(&Value::Null), 0);
    }

    #[test]
    fn cached_text_extraction_by_format() {
        let o = json!({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]});
        assert_eq!(
            extract_cached_text(&o, ResponseFormat::Openai),
            ("hi".to_owned(), Some("stop".to_owned()))
        );
        let a = json!({"content": [{"type": "text", "text": "yo"}], "stop_reason": "end_turn"});
        assert_eq!(
            extract_cached_text(&a, ResponseFormat::Anthropic),
            ("yo".to_owned(), Some("end_turn".to_owned()))
        );
        assert_eq!(
            extract_cached_text(&json!({}), ResponseFormat::Openai),
            (String::new(), None)
        );
        assert_eq!(
            extract_cached_text(&json!({}), ResponseFormat::Anthropic),
            (String::new(), None)
        );
    }
}
