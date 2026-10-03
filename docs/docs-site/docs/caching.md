# Response Cache: Architecture & Stampede Protection

WaddleAI's proxy response cache (spec §6) has three cooperating layers,
tried cheapest-first on every request:

```
request --> [exact cache] --> [semantic cache] --> [upstream provider]
              (Valkey,            (pgvector,            (LLM dispatch --
               SHA-256 key)        restricted,            not cached here;
                                   embedding-gated)        see upstream.py)
```

- **Exact cache** (`shared/cache/exact.py`) -- Valkey-backed, keyed by a
  SHA-256 hash of the canonical request shape. Deterministic requests only
  (`temperature == 0`, no executed tool-call result in history).
- **Semantic cache** (`shared/cache/semantic.py`) -- pgvector-backed,
  restricted to single-turn, tool-free, informational-looking requests.
  Requires an embedding of the latest user message; default OFF
  (`cache_configs.semantic_enabled`).
- **Upstream prompt-cache orchestration** (`shared/cache/upstream.py`) --
  on a miss, lets the *provider's own* prompt cache (Anthropic
  `cache_control`, OpenAI `cached_tokens`, Gemini `CachedContent`) do its
  job instead of being defeated by the proxy.

The facade, `shared.cache.response_cache.ResponseCache`, is what the
proxy's `CacheStage` actually calls; the layer modules above are
independently testable.

## Cache-Stampede (Thundering-Herd) Protection

A burst of identical requests all missing the cache at once must never all
dispatch upstream (or all call the embedder) simultaneously -- that's worse
than having no cache at all. `shared/cache/singleflight.py` provides the
shared mechanism every layer uses on a miss:

1. **In-process single-flight** -- within one worker process, the first of
   N identical concurrent misses becomes the *leader*; the other N-1
   *followers* await the leader's result directly (no Valkey round trip).
2. **Cross-process Valkey lease** (`SET key:lock NX PX <ttl>`) -- across
   worker processes/pods, only the lease holder computes the value.
   Followers poll (jittered backoff) for the leader's write, bounded by a
   wait timeout; if the lease expires or the wait times out, the follower
   falls through and computes its own value. **The cache never blocks a
   request indefinitely, and a Valkey outage degrades to in-process-only
   coordination, never a failure.**
3. **Jittered write-side TTLs** -- every cache write's TTL is jittered
   (± a configurable fraction) so a burst of writes made around the same
   time doesn't expire in lockstep and re-trigger a synchronized stampede
   later.
4. **Embedding-call dedup + latency budget** (semantic cache only) --
   concurrent requests for the same normalized prompt share one embedding
   call; if the embedder exceeds its latency budget or reports
   unhealthy/circuit-open, the semantic cache is bypassed for that request
   (counted, never blocked) rather than serializing behind a saturated
   embedder. The exact cache is always checked first, so most hits never
   need an embedding at all.

### Environment Variables

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_STAMPEDE_LEASE_TTL_SECONDS` | `30` | Valkey lease lifetime (PX); bounds how long a crashed/slow leader can block followers. |
| `CACHE_STAMPEDE_WAIT_TIMEOUT_SECONDS` | `3` | How long a follower waits for the leader's value before falling through to compute its own. |
| `CACHE_STAMPEDE_POLL_INTERVAL_SECONDS` | `0.1` | Base jittered-poll interval while a follower waits on the Valkey path. |
| `CACHE_TTL_JITTER_FRACTION` | `0.1` | ± fraction applied to every cache write's TTL. |
| `SEMANTIC_CACHE_EMBED_TIMEOUT_MS` | `300` | Embedding-call latency budget before the semantic cache bypasses for that request. |
| `CACHE_ORG_QUOTA_KB` | `10240` | Per-org exact-cache byte quota (unrelated to stampede protection, listed for completeness). |

### Feature Flag (Kill Switch)

`waddleai.disable-cache-singleflight` -- an **opt-out** flag: unseen or OFF
means the stampede-protection mechanism is **ON** (the default/safe state);
turning it ON reverts to the legacy behavior where every miss dispatches
independently. Resolved per request via the same `features` helper the
proxy pipeline already uses (async `resolve()` where available, else a
sync `is_feature_enabled()` moved off the event loop) -- see
`shared.cache.singleflight.resolve_singleflight_enabled`.

### Metrics

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `waddleai_cache_lookups_total` | Counter | `layer` (`exact`\|`semantic`\|`response`), `result` (`hit`\|`miss`\|`bypass`\|`stampede_wait`\|`stampede_fallthrough`) | `hit`/`miss` recorded by the proxy's `CacheStage`; `bypass`/`stampede_wait`/`stampede_fallthrough` recorded by `shared.cache.singleflight` and the embed-budget helper. |
| `waddleai_cache_lookup_duration_seconds` | Histogram | `layer` (`exact`\|`semantic`\|`response`) | Includes any single-flight wait time a follower spends -- that wait is real request latency. |
| `waddleai_cache_tokens_saved_total` | Counter | `layer` | Tokens saved by a cache hit (pre-existing, spec §6.4). |
| `waddleai_cache_entries_evicted_total` | Counter | `layer` | LRU/quota evictions (pre-existing, exact-cache only today). |

### Scoped Keys (Security Boundary)

Every stampede-lease key is namespaced by `org_id` (and, for the semantic
layer, `model_class` + `context_hash`) in addition to the underlying
cache-entry key already being org-scoped -- two different orgs can never
share a lease, a wait, or a leader/follower relationship, even if their
request shapes happen to hash identically. See
`shared.cache.response_cache._exact_lease_key` /
`_semantic_lease_key`, and the isolation tests in
`tests/unit/cache/test_singleflight.py` and
`tests/unit/cache/test_response_cache_facade.py`.

## See Also

- `docs/docs-site/docs/architecture.md` -- overall system architecture
- `shared/cache/__init__.py` -- module docstring with the full layer list
- `shared/cache/singleflight.py` -- the stampede-protection mechanism itself
