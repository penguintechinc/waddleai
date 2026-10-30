//! `ResponseCache` facade: exact, then semantic, then upstream-miss (port of
//! `shared/cache/response_cache.py`).
//!
//! `lookup` tries the exact layer, then the semantic layer if enabled and exact missed, and
//! returns either a hit or a miss carrying a write-back handle. The write-back is deliberately NOT
//! invoked here: the caller commits it only after the whole pipeline, including output filtering,
//! completed without `ctx.blocked`, so a blocked or filtered response never enters any layer
//! (cache-poisoning defense, spec 3.6). Keys derive from `ctx.messages`, which (because the cache
//! stage runs after `security_in`) is already post-input-filter content.
//!
//! Entries are scoped by wire format: `{model}::{response_format}` is the key's model class, so an
//! OpenAI and an Anthropic request for "the same" prompt never share an entry.

use std::sync::{Arc, Mutex, PoisonError};
use std::time::Instant;

use cache::{derive_exact_key, ExactKeyParts};
use pipeline::{
    CacheStatus, FlagResolver, PipelineContext, ResponseFormat, WriteBack, WriteBackSink,
};
use serde_json::Value;

use crate::affinity::AffinityStore;
use crate::config::ConfigSource;
use crate::exact::{
    has_unkeyed_shaping, is_cacheable_response, is_exact_eligible, py_float, py_truthy,
    CachedResponse, ExactStore, PutLimits,
};
use crate::metrics::CacheMetrics;
use crate::semantic::{is_semantic_eligible, CtxFlags, SemanticCache};
use crate::singleflight::{
    guard_miss, FlightGuard, InProcessSingleFlight, LeaseStore, MissGuardInput, SingleFlightConfig,
    DISABLE_SINGLEFLIGHT_FLAG,
};

/// Default per-org exact-cache quota: 10 MiB (`CACHE_ORG_QUOTA_KB` overrides).
const DEFAULT_ORG_QUOTA_KB: u64 = 10 * 1024;

/// What the cache stage should do next.
#[derive(Debug, Clone, Default)]
pub struct LookupResult {
    /// Which layer hit, or `Miss`.
    pub status: CacheStatus,
    /// The cached response, on a hit.
    pub cached: Option<CachedResponse>,
    /// On a miss: the handle the caller commits once the response is known safe to cache.
    pub write_back: Option<WriteBack>,
}

impl LookupResult {
    fn miss() -> Self {
        Self::default()
    }
}

/// Orchestrates the exact, semantic and affinity layers for one request.
pub struct ResponseCache {
    exact: Arc<dyn ExactStore>,
    exact_flight: InProcessSingleFlight<CachedResponse>,
    semantic: Option<Arc<SemanticCache>>,
    affinity: Option<Arc<dyn AffinityStore>>,
    config: Arc<dyn ConfigSource>,
    flags: Arc<dyn FlagResolver>,
    lease: Option<Arc<dyn LeaseStore>>,
    metrics: CacheMetrics,
    org_quota_kb: u64,
    flight_cfg: SingleFlightConfig,
}

/// The `Sync` slice of a request that a lookup needs.
struct LookupRequest<'a> {
    org_id: Option<i64>,
    vkey_id: Option<i64>,
    body: &'a Value,
    messages: &'a [Value],
    model: &'a str,
    format: ResponseFormat,
}

/// Python `_org_id`: a falsy org (None or 0) means "no caching" — org 0 is the unattributed
/// fall-through bucket and sharing cache entries across it would leak between unrelated callers.
fn org_of(ctx: &PipelineContext) -> Option<i64> {
    ctx.user
        .as_ref()
        .map(|u| i64::from(u.org_id))
        .filter(|id| *id != 0)
}

fn format_name(f: ResponseFormat) -> &'static str {
    match f {
        ResponseFormat::Openai => "openai",
        ResponseFormat::Anthropic => "anthropic",
    }
}

/// Python `float(body.get("temperature") or 0.0)`.
fn key_temperature(body: &Value) -> f64 {
    body.get("temperature")
        .filter(|v| py_truthy(v))
        .and_then(py_float)
        .unwrap_or(0.0)
}

impl ResponseCache {
    /// Exact-only cache; add layers with the `with_*` builders. Tunables come from the
    /// environment (`CACHE_STAMPEDE_*`, `CACHE_TTL_JITTER_FRACTION`, `CACHE_ORG_QUOTA_KB`).
    pub fn new(
        exact: Arc<dyn ExactStore>,
        config: Arc<dyn ConfigSource>,
        flags: Arc<dyn FlagResolver>,
        metrics: Arc<telemetry::Metrics>,
    ) -> Self {
        let org_quota_kb = std::env::var("CACHE_ORG_QUOTA_KB")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|v| *v > 0)
            .unwrap_or(DEFAULT_ORG_QUOTA_KB);
        Self {
            exact,
            exact_flight: InProcessSingleFlight::new(),
            semantic: None,
            affinity: None,
            config,
            flags,
            lease: None,
            metrics: CacheMetrics::new(metrics),
            org_quota_kb,
            flight_cfg: SingleFlightConfig::from_env(),
        }
    }

    /// Adds the semantic layer.
    pub fn with_semantic(mut self, semantic: Arc<SemanticCache>) -> Self {
        self.semantic = Some(semantic);
        self
    }

    /// Adds session affinity (preferred-backend hints on a miss).
    pub fn with_affinity(mut self, affinity: Arc<dyn AffinityStore>) -> Self {
        self.affinity = Some(affinity);
        self
    }

    /// Adds the cross-process stampede lease.
    pub fn with_lease(mut self, lease: Arc<dyn LeaseStore>) -> Self {
        self.lease = Some(lease);
        self
    }

    /// Overrides the per-org exact-cache quota (KiB).
    pub fn with_org_quota_kb(mut self, kb: u64) -> Self {
        self.org_quota_kb = kb;
        self
    }

    /// Overrides the stampede/jitter tunables.
    pub fn with_flight_config(mut self, cfg: SingleFlightConfig) -> Self {
        self.flight_cfg = cfg;
        self
    }

    /// The metrics recorder (the stage records hit/miss through it).
    pub fn metrics(&self) -> &CacheMetrics {
        &self.metrics
    }

    /// Tries exact then semantic; returns a hit or a miss with a write-back. Wrapped with the
    /// `response` layer duration histogram, which includes any single-flight wait (real latency
    /// the request experiences).
    ///
    /// Not an `async fn` on purpose: `PipelineContext` is not `Sync`, so the returned future must
    /// borrow only the `Sync` request fields (`body`, `messages`, `model`), keeping it `Send`.
    pub fn lookup<'a>(
        &'a self,
        ctx: &'a PipelineContext,
    ) -> impl std::future::Future<Output = LookupResult> + Send + 'a {
        let req = LookupRequest {
            org_id: org_of(ctx),
            vkey_id: ctx.user.as_ref().and_then(|u| u.api_key_id).map(i64::from),
            body: &ctx.body,
            messages: &ctx.messages,
            model: ctx.model.as_deref().unwrap_or(""),
            format: ctx.response_format,
        };
        async move {
            let start = Instant::now();
            let out = self.lookup_inner(req).await;
            self.metrics
                .lookup_duration("response", start.elapsed().as_secs_f64());
            out
        }
    }

    async fn lookup_inner(&self, req: LookupRequest<'_>) -> LookupResult {
        let Some(org_id) = req.org_id else {
            return LookupResult::miss();
        };
        let vkey_id = req.vkey_id;
        let body = req.body;
        if has_unkeyed_shaping(body) {
            // Neither read nor write: the key cannot distinguish this request (see exact.rs).
            tracing::debug!("response cache skipped: request carries fields the key omits");
            return LookupResult::miss();
        }
        let messages = req.messages;
        let model_class = format!("{}::{}", req.model, format_name(req.format));
        let cfg = match self.config.resolve(org_id, vkey_id).await {
            Ok(c) => c,
            Err(e) => {
                tracing::error!(error = %e, "cache config unavailable; caching skipped for this request");
                self.metrics.lookup("response", "bypass");
                return LookupResult::miss();
            }
        };
        let singleflight_enabled = !self
            .flags
            .enabled(DISABLE_SINGLEFLIGHT_FLAG, Some(&org_id.to_string()))
            .await;
        let ttl = cfg.ttl_seconds;

        let mut committer = Committer {
            exact: None,
            semantic: None,
        };
        let mut wb_key: Option<String> = None;

        if cfg.exact_enabled && is_exact_eligible(body.get("temperature"), messages) {
            let parts = ExactKeyParts {
                org_id,
                model_class: &model_class,
                messages,
                tools: body.get("tools"),
                temperature: key_temperature(body),
                top_p: body.get("top_p"),
                max_tokens: body.get("max_tokens"),
            };
            let key = derive_exact_key(&parts);
            match self.exact.get(org_id, &key).await {
                Ok(Some(cached)) => {
                    return LookupResult {
                        status: CacheStatus::Exact,
                        cached: Some(cached),
                        write_back: None,
                    };
                }
                Ok(None) => {
                    let lease_key = cache::keys::exact_lease(org_id, &key);
                    let exact = self.exact.clone();
                    let (fetch_org, fetch_key) = (org_id, key.clone());
                    let guard = guard_miss(
                        MissGuardInput {
                            cache_key: &lease_key,
                            in_process: &self.exact_flight,
                            lease: self.lease.as_ref(),
                            layer: "exact",
                            enabled: singleflight_enabled,
                            cfg: &self.flight_cfg,
                            metrics: &self.metrics,
                        },
                        || {
                            let (exact, k) = (exact.clone(), fetch_key.clone());
                            async move { exact.get(fetch_org, &k).await.ok().flatten() }
                        },
                    )
                    .await;
                    if !guard.is_leader {
                        if let Some(cached) = guard.cached {
                            return LookupResult {
                                status: CacheStatus::Exact,
                                cached: Some(cached),
                                write_back: None,
                            };
                        }
                    }
                    committer.exact = Some(ExactCommit {
                        store: self.exact.clone(),
                        org_id,
                        key: key.clone(),
                        limits: PutLimits {
                            ttl_secs: ttl,
                            max_entry_kb: cfg.max_entry_kb,
                            org_quota_kb: self.org_quota_kb,
                        },
                        guard: Mutex::new(Some(guard.flight)),
                    });
                    wb_key = Some(key);
                }
                Err(e) => {
                    tracing::warn!(error = %e, "exact cache unavailable; skipping the exact layer");
                    self.metrics.lookup("exact", "bypass");
                }
            }
        }

        if let (Some(semantic), true) = (self.semantic.as_ref(), cfg.semantic_enabled) {
            let ctx_flags = CtxFlags {
                is_single_turn: messages.len() <= 1,
                has_tools_schema: body.get("tools").is_some_and(py_truthy),
                has_memory_injection: body
                    .get("messages")
                    .and_then(Value::as_array)
                    .map_or(!messages.is_empty(), |m| m != messages),
                temperature: body.get("temperature").cloned(),
            };
            if is_semantic_eligible(body, messages, &ctx_flags) {
                let last_user_text = messages
                    .iter()
                    .rev()
                    .find(|m| m.get("role").and_then(Value::as_str) == Some("user"))
                    .and_then(|m| m.get("content"))
                    .and_then(Value::as_str);
                if let Some(text) = last_user_text {
                    let prior = &messages[..messages.len().saturating_sub(1)];
                    let context_hash = derive_exact_key(&ExactKeyParts {
                        org_id,
                        model_class: &model_class,
                        messages: prior,
                        tools: None,
                        temperature: 0.0,
                        top_p: None,
                        max_tokens: None,
                    });
                    let mut flight = FlightGuard::none();
                    // Embed once up front (deduped + budget-bounded) so stampede polling can
                    // re-score against the same vector without re-embedding per poll.
                    if let Some(embedding) = semantic.embed(text).await {
                        if let Some(cached) = semantic
                            .score_candidates(
                                org_id,
                                &model_class,
                                &context_hash,
                                &embedding,
                                cfg.semantic_threshold,
                            )
                            .await
                        {
                            return LookupResult {
                                status: CacheStatus::Semantic,
                                cached: Some(cached),
                                write_back: None,
                            };
                        }
                        // Python keys this flight on (org, model_class, context_hash) only, which
                        // excludes the query: two different concurrent questions would collapse and
                        // a follower would be handed the leader's answer. Add the normalized-query
                        // digest so only identical queries share a flight/lease.
                        let lease_key = format!(
                            "{}:{}",
                            cache::keys::semantic_lease(org_id, &model_class, &context_hash),
                            crate::semantic::embed_dedup_key(text)
                                .trim_start_matches("semantic:embed:")
                        );
                        let guard = guard_miss(
                            MissGuardInput {
                                cache_key: &lease_key,
                                in_process: &semantic.flight,
                                lease: self.lease.as_ref(),
                                layer: "semantic",
                                enabled: singleflight_enabled,
                                cfg: &self.flight_cfg,
                                metrics: &self.metrics,
                            },
                            || {
                                semantic.score_candidates(
                                    org_id,
                                    &model_class,
                                    &context_hash,
                                    &embedding,
                                    cfg.semantic_threshold,
                                )
                            },
                        )
                        .await;
                        if !guard.is_leader {
                            if let Some(cached) = guard.cached {
                                return LookupResult {
                                    status: CacheStatus::Semantic,
                                    cached: Some(cached),
                                    write_back: None,
                                };
                            }
                        }
                        flight = guard.flight;
                    }
                    committer.semantic = Some(SemanticCommit {
                        cache: semantic.clone(),
                        org_id,
                        model_class: model_class.clone(),
                        last_user_msg: text.to_owned(),
                        context_hash: context_hash.clone(),
                        ttl_secs: ttl,
                        guard: Mutex::new(Some(flight)),
                    });
                    wb_key.get_or_insert(context_hash);
                }
            }
        }

        let write_back = wb_key.map(|key| WriteBack {
            key,
            ttl_secs: Some(ttl),
            sink: Some(Arc::new(committer)),
        });
        LookupResult {
            status: CacheStatus::Miss,
            cached: None,
            write_back,
        }
    }

    /// Best-effort miss annotation: session affinity becomes `ctx.preferred_backend`. The hint is
    /// advisory; routing decides whether to honor it. (The Anthropic upstream prompt-cache
    /// orchestrator of `upstream.py` is not part of this port.)
    pub async fn annotate_miss(&self, ctx: &mut PipelineContext) {
        let Some(org_id) = org_of(ctx) else {
            return;
        };
        let Some(affinity) = &self.affinity else {
            return;
        };
        let session = ctx
            .body
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        if let Some(session) = session {
            if let Some(preferred) = affinity.lookup(org_id, session).await {
                ctx.preferred_backend = Some(preferred);
            }
        }
    }
}

struct ExactCommit {
    store: Arc<dyn ExactStore>,
    org_id: i64,
    key: String,
    limits: PutLimits,
    guard: Mutex<Option<FlightGuard>>,
}

struct SemanticCommit {
    cache: Arc<SemanticCache>,
    org_id: i64,
    model_class: String,
    last_user_msg: String,
    context_hash: String,
    ttl_secs: u64,
    guard: Mutex<Option<FlightGuard>>,
}

/// Commits one miss's response into every layer that was eligible, then settles each layer's
/// single-flight obligations (followers get the value, or none for an uncacheable response).
struct Committer {
    exact: Option<ExactCommit>,
    semantic: Option<SemanticCommit>,
}

fn take_guard(slot: &Mutex<Option<FlightGuard>>) -> Option<FlightGuard> {
    slot.lock().unwrap_or_else(PoisonError::into_inner).take()
}

#[async_trait::async_trait]
impl WriteBackSink for Committer {
    async fn commit(&self, response: &Value, usage: &Value) {
        let value = CachedResponse {
            response: response.clone(),
            usage: usage.clone(),
            stored_at: 0.0,
        };
        // regression: cache-poisoning -- never cache a failed/empty completion; the flight is
        // still settled below so followers fall through to compute their own.
        let cacheable = is_cacheable_response(response, usage);
        if let Some(ex) = &self.exact {
            if cacheable {
                if let Err(e) = ex.store.put(ex.org_id, &ex.key, &value, ex.limits).await {
                    tracing::warn!(error = %e, "exact write-back failed");
                }
            } else {
                tracing::info!("exact write-back skipped: failed/empty completion not cached");
            }
            if let Some(g) = take_guard(&ex.guard) {
                g.finish(cacheable.then(|| value.clone())).await;
            }
        }
        if let Some(sem) = &self.semantic {
            if cacheable {
                sem.cache
                    .put(
                        sem.org_id,
                        &sem.model_class,
                        &sem.last_user_msg,
                        &sem.context_hash,
                        &value,
                        sem.ttl_secs,
                    )
                    .await;
            } else {
                tracing::info!("semantic write-back skipped: failed/empty completion not cached");
            }
            if let Some(g) = take_guard(&sem.guard) {
                g.finish(cacheable.then(|| value.clone())).await;
            }
        }
    }
}
