# Proxy Observability: Prometheus Metrics

The proxy exposes Prometheus text-format metrics at `GET /metrics` (excluded
from OIDC auth as a public path). All metrics are registered once,
process-wide, in `shared/utils/metrics.py`'s `WaddleAIMetrics` class and
recorded from `proxy/apps/proxy_server/main.py` and the pipeline stages.

**Label cardinality rule:** every label value must come from a bounded,
closed set (service name, provider name, status, a route *template*, an
operator-configured model name) -- never a raw request path, user id, or
other unbounded value. `record_request`'s `endpoint` label additionally
passes through a defensive `_sanitize_label()` guard that rewrites any
UUID/numeric-id-shaped path segment to `<id>` and logs a one-time WARN if it
ever has to (see "Endpoint label" below) -- this is a backstop, not a
substitute for passing a bounded value in the first place.

## Request metrics

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `waddleai_requests_total` | Counter | `service`, `endpoint`, `method`, `status_code` | `endpoint` is the matched route **template** (e.g. `/mem0/memories/<memory_id>`), or the literal `unmatched` when no route matched (404/405) -- never the concrete path. |
| `waddleai_request_duration_seconds` | Histogram | `service`, `endpoint`, `method` | Wall-clock duration of the full request, recorded in `after_request_metrics`. |

### Endpoint label (ops O1-a)

`after_request_metrics` (a Quart `after_request` hook) derives `endpoint`
from `request.url_rule.rule` -- the Werkzeug route template Quart already
resolved during dispatch -- falling back to the literal `unmatched` when
`request.url_rule` is `None` (no route matched, e.g. a 404). This replaced
a prior implementation that used `request.path` directly, which put the
concrete value of any parametrized route (e.g. `/mem0/memories/<memory_id>`)
straight into a Prometheus label -- unbounded cardinality, one timeseries
per distinct memory id ever requested.

`record_request()` additionally runs every `endpoint` value through
`shared.utils.metrics._sanitize_label()`, which rewrites UUID and
2+-digit numeric path segments to `<id>` and logs a WARN the first time it
has to rewrite anything in the process's lifetime. This is a defensive
backstop for a *future* caller (or an edge case in route-template
derivation) -- it is not meant to be the primary unboundedness defense.

## LLM / provider metrics

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `waddleai_llm_requests_total` | Counter | `provider`, `model`, `status` | Recorded once per completed `/v1/chat/completions` or `/v1/messages` call that reaches a terminal (non-blocked) success. |
| `waddleai_llm_tokens_total` | Counter | `provider`, `model`, `token_type` | `token_type` is `input`/`output`. |
| `waddleai_normalized_tokens_total` | Counter | `organization`, `provider` | WaddleAI-normalized token accounting. Per-user `user` label was intentionally removed (release-audit-2026-09-23, ops O1) -- raw `user_id` is unbounded; per-user attribution lives in the usage DB, not a Prometheus label. |
| `waddleai_llm_request_duration_seconds` | Histogram | `provider`, `model`, `status` | **New (ops O1-c).** Upstream LLM/provider call latency -- previously only counted, never timed. `duration` spans `ProxyPipeline.run()` (dominated by the single `DispatchStage` upstream call). Recorded on **both** success (`status="success"`) and a blocked/failed dispatch where `ctx.provider` was already set (`status="error"`) -- a pre-dispatch block (no provider ever selected) records no sample. Buckets: `0.1`-`120.0`s (LLM calls routinely take seconds, not milliseconds). `model` reuses the exact value `waddleai_llm_requests_total` already uses: the router-resolved target model, drawn from the operator-configured set of connection-link models -- not raw user input -- so this histogram doesn't add a second cardinality source. |

**Streaming note:** `record_llm_latency()` (the method backing the
histogram above) is written to be reusable by `pipeline/stages.py`'s
streaming `DispatchStage` once that path times the streamed upstream call
directly -- same histogram, same bounded labels, no separate instrument
needed.

## Embedding-call metrics (Ollama-embedding bulkhead, ops O10/O5)

The in-cluster Ollama instance serving live chat (`waddleai_llm_*` above)
also serves embedding traffic by default: the semantic-cache prompt
embedding in `shared/cache/semantic.py`, plus penguincode's doc/code
indexing, GraphRAG query embedding, and mem0 memory embedder. A bulk
embedding burst can saturate that shared instance and degrade chat
latency cluster-wide with no warning, unless the two are told apart.

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `waddleai_embedding_call_duration_seconds` | Histogram | `endpoint`, `outcome` | `endpoint` is the closed `chat_ollama`/`embedding_ollama` label (never the raw URL) -- `chat_ollama` means the call went to the same instance serving `waddleai_llm_*` traffic above; `embedding_ollama` means it went to a dedicated endpoint (see `OLLAMA_EMBEDDING_URL` below). `outcome` is `ok`/`error`. Buckets: `0.01`-`30.0`s. |
| `waddleai_embedding_calls_total` | Counter | `endpoint`, `outcome` | Same labels as above. |

Recorded by `shared.utils.embedding_manager.EmbeddingManager.embed()` for
the `ollama` backend only (the bulkhead concept doesn't apply to the
`openai`/`anthropic` backends, which have no local saturation risk).
Cross-reference against `waddleai_llm_request_duration_seconds` (chat) and
`waddleai_cache_lookup_duration_seconds` (semantic-cache lookup, which
includes this embedding call) to see whether a chat-latency regression
correlates with embedding-call volume on the SAME `chat_ollama` endpoint.

## Proxy ConcurrencyLimiter metrics (ops O10)

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `waddleai_proxy_inflight_requests` | Gauge | `service` | Current in-flight count held by **this worker process's** `ConcurrencyLimiter`. The limiter's ceiling (`PROXY_MAX_CONCURRENT_PER_WORKER`, see below) is enforced per Hypercorn worker, not cluster- or pod-wide -- with N workers the real ceiling is `limit * N`. This gauge (summed/maxed across workers in your dashboard) is how you observe the real multi-process behavior instead of just the documented per-worker number. |
| `waddleai_proxy_concurrency_rejections_total` | Counter | `endpoint` | Incremented once per request shed with a 429 by the limiter. `endpoint` is always a caller-supplied literal (e.g. `/v1/chat/completions`), never `request.path`. |

## Other existing metrics (unchanged by this change)

| Metric | Type | Labels |
|---|---|---|
| `waddleai_security_events_total` | Counter | `event_type`, `severity`, `action` |
| `waddleai_database_operations_total` | Counter | `operation`, `table`, `status` |
| `waddleai_database_operation_duration_seconds` | Histogram | `operation`, `table` |
| `waddleai_active_connections` | Gauge | `service`, `connection_type` |
| `waddleai_auth_attempts_total` | Counter | `auth_type`, `status` |
| `waddleai_provider_health` | Gauge | `provider`, `endpoint` |
| `waddleai_token_quota_usage` | Gauge | `organization`, `user` | Setter (`set_token_quota_usage`) exists but is not yet called anywhere -- quota computation itself is currently a mocked/inert stand-in in `TokenBudgetStage` pending the budget/meter work (gh-212/gh-217); wire the setter in once that lands. |
| `waddleai_rate_limit_exceeded_total` | Counter | `endpoint`, `limit_type` |
| `waddleai_cache_lookups_total` | Counter | `layer`, `result` |
| `waddleai_cache_tokens_saved_total` | Counter | `layer` |
| `waddleai_cache_entries_evicted_total` | Counter | `layer` |
| `waddleai_hook_invocations_total` | Counter | `ecosystem`, `event`, `decision` |
| `waddleai_hook_evaluation_duration_seconds` | Histogram | `ecosystem`, `event` |
| `waddleai_hook_timeouts_total` | Counter | `tier` |
| `waddleai_hook_fail_mode_total` | Counter | `mode` |
| `waddleai_hook_tool_calls_total` | Counter | `ecosystem`, `tool_name`, `organization` |
| `waddleai_hook_rule_evaluations_total` | Counter | `rule_id`, `scope` |
| `waddleai_hook_rule_decisions_total` | Counter | `rule_id`, `scope`, `decision` |
| `waddleai_info` | Info | (static) |

## Related tunables (env vars)

These aren't metrics themselves, but govern the behavior the metrics above
observe -- every one is env-tunable with a sane default, never a hardcoded
literal in the hot path (release-audit-2026-10-02, ops O4/O6/O10):

| Env var | Default | Governs |
|---|---|---|
| `PROXY_MAX_CONCURRENT_PER_WORKER` | `100` | `ConcurrencyLimiter`'s per-Hypercorn-worker-process in-flight ceiling. Falls back to the legacy `MAX_CONCURRENT_REQUESTS` if set, so an existing deployment's env doesn't silently revert to the default on upgrade. |
| `PROXY_MAX_BODY_BYTES` | `10485760` (10 MiB) | `app.config["MAX_CONTENT_LENGTH"]` -- the request body size cap. Rejected with `413` in the proxy's standard `{"error": {"message", "type"}}` envelope. LLM prompts (multi-turn history, embedded context) can legitimately be large, so the default sits well above a typical payload while still bounding worst-case per-request memory. |
| `PROXY_VALKEY_MAX_CONNECTIONS` | `50` | Max pooled connections per `redis.from_url(...)` client (the §6A memory-layer client and the shared TokenBudgetStage/CacheStage client). |
| `PROXY_VALKEY_SOCKET_TIMEOUT_SECONDS` | `5` | Per-command socket timeout on those same Valkey clients. |
| `PROXY_VALKEY_SOCKET_CONNECT_TIMEOUT_SECONDS` | `5` | Connection-establishment timeout on those same clients. |
| `PROXY_VALKEY_HEALTH_CHECK_INTERVAL_SECONDS` | `30` | How often pooled connections are health-checked, so idle ones don't go stale against Valkey. |
| `OLLAMA_EMBEDDING_URL` | unset | Ollama-embedding bulkhead (ops O10/O5): when set, `shared.utils.embedding_manager.create_embedding_manager()` (and therefore the proxy's semantic-cache/RAG embedding calls) routes to this dedicated endpoint instead of `OLLAMA_HOST`. Unset means the fallback chain below applies -- today's single-Ollama behavior, unchanged. Pair with `k8s/helm/waddleai`'s `ollamaEmbeddings.enabled` Deployment. |
| `OLLAMA_HOST` | `http://localhost:11434` | Fallback target for embedding calls when `OLLAMA_EMBEDDING_URL` is unset -- the same chat-serving Ollama host used elsewhere (`shared/vectorstore/factory.py`). |

### Body-size enforcement path

`app.config["MAX_CONTENT_LENGTH"]` alone enforces lazily -- only the first
time a route handler actually reads the body -- and `chat_completions()` /
`claude_messages()` each wrap that read in their own broad
`except Exception`, which would otherwise convert the resulting
`RequestEntityTooLarge` into a generic `500` before it ever reached the
proxy's error envelope. A `before_request` hook
(`_enforce_max_body_size`) checks the advertised `Content-Length` header
up front, before routing/dispatch, so the rejection happens early enough to
always produce the documented `413` response. A request with no
Content-Length header (e.g. true chunked transfer) isn't covered by that
early check; Quart's lazy enforcement during the body read remains the
backstop for that case.
