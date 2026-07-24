# Response Cache: Exact / Semantic / Upstream Passthrough — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/response-cache` (off `release/v0.2.X`). **Depends on:** `feature/aiproxy-migration` (the stage-class `ProxyPipeline` in `proxy/apps/proxy_server/pipeline/`, `shared/licensing/features.py::features.enabled(...)`, streaming connectors, `MeteringBuffer`, migrations 007–008). Merge back into `release/v0.2.X` without a PR when complete.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §6 (with §3.2 stage-4 placement, §3.6 poisoning defense/org boundary, §13.1 migration 009a, §14.2 standing gates, §14.5 flag `waddleai.response_cache`). Authoritative.

---

**Goal:** Pipeline stage 4 — `shared/cache/` with the `response_cache.py` facade — three layers, cheapest lookup first: (1) **exact Valkey cache** (SHA-256 key, `temperature == 0` eligibility, default ON, streaming hits replayed as synthetic SSE, org-scoped keys as an absolute boundary); (2) **restricted semantic pgvector cache** (default OFF, single-turn/no-tools/temp-0/informational only, 0.95 cosine threshold, HNSW); (3) **upstream prompt-cache orchestration on miss** (Anthropic `cache_control` auto-inject default ON with per-org toggle, OpenAI `cached_tokens` surfaced, Gemini CachedContent lifecycle, Ollama/llama.cpp KV reuse via a Valkey session-affinity map). Plus: additive-only `usage.waddleai` cache fields, `token_usage.cache_status`/`tokens_saved` accounting, the `cache_configs` table (migration 009a), a management CRUD + cache-stats dashboard surface, and the §6.5 acceptance suite where **org isolation is a security test**.

**Architecture:** `CacheStage` slots into the existing `ProxyPipeline` after `SecurityInStage`, before dispatch (§3.2 order: auth → token/budget → security-in → **cache** → dispatch → security-out → meter; routing arrives on a later branch and sits after cache). On hit, the stage populates `ctx.response` from cache and short-circuits dispatch — but never metering. On miss, it annotates the outgoing request (Anthropic breakpoints, Gemini cached-content ref, Ollama affinity hint) and registers a **write-back callback that only fires after `SecurityOutStage` passes** — entries are keyed to post-filter content and blocked responses are never cached (poisoning defense, §3.6). Everything is behind PostHog flag `waddleai.response_cache` (default OFF, fail-safe OFF via `features.enabled`); finer per-org/per-key toggles (`exact_enabled` default true, `semantic_enabled` default false, `anthropic_cache_control` default true) resolve through `cache_configs`. Valkey holds nothing durable — its loss degrades hit rate, never correctness (§3.4). No CPU-heavy work on the event loop: embeddings are async network I/O (Ollama `nomic-embed-text`, 768-dim, matching `shared/utils/embedding_manager.py`).

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), Valkey 8 (redis-py asyncio client; fakeredis in unit tests), Postgres 16 + pgvector (HNSW), orjson, tiktoken (prefix token counts), pytest + pytest-asyncio, recorded provider fixtures (no live API calls in CI).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/alembic/versions/009a_response_cache.py` | Migration 009a `response_cache`: `cache_configs`, `response_cache_entries` (pgvector+HNSW), `token_usage.cache_status`/`tokens_saved` |
| Modify | `services/management/app/models_sqlalchemy.py` | `CacheConfig`, `ResponseCacheEntry` ORM models; `TokenUsage` new columns |
| Create | `tests/unit/management/test_migration_009a.py` | Round-trip + downgrade on seeded snapshot |
| Create | `shared/cache/__init__.py` | Package exports (`ResponseCache`, `CacheStageResult`, factories) |
| Create | `shared/cache/keys.py` | Eligibility predicates + SHA-256 exact-key derivation |
| Create | `tests/unit/cache/test_eligibility_keys.py` | Determinism-eligibility matrix + key stability/sensitivity tests |
| Create | `shared/cache/exact.py` | `ExactCache` — Valkey get/put, TTL, `max_entry_kb`, per-org quota LRU |
| Create | `tests/unit/cache/test_exact_cache.py` | Exact-layer unit tests (fakeredis) |
| Create | `tests/unit/cache/test_exact_cache_isolation.py` | **Security test**: org A can never read org B's entries |
| Create | `shared/cache/replay.py` | Synthetic-SSE replay of cached responses (OpenAI + Anthropic framing) |
| Create | `tests/unit/cache/test_streaming_replay.py` | Byte-equivalence replay tests, both endpoint formats |
| Create | `shared/cache/config.py` | `cache_configs` resolution (key > org > default) with Valkey-cached hot path + invalidation |
| Create | `services/management/app/api/v1/cache_configs.py` | Management CRUD `/api/v1/cache-configs` (control plane, §3.3) |
| Create | `tests/unit/cache/test_cache_config.py`, `tests/unit/management/test_cache_config_api.py` | Resolution precedence + CRUD/invalidation tests |
| Create | `shared/cache/semantic.py` | `SemanticCache` — pgvector lookup/write, restricted eligibility, 0.95 threshold |
| Create | `tests/unit/cache/test_semantic_cache.py` | Should-hit/should-miss corpus, threshold regression, restriction matrix |
| Create | `tests/unit/cache/test_semantic_cache_isolation.py` | **Security test**: org-scoped pgvector queries |
| Create | `shared/cache/upstream.py` | Anthropic `cache_control` injection + prefix tracking; OpenAI `cached_tokens` surfacing; Gemini CachedContent lifecycle |
| Create | `tests/unit/cache/test_upstream_anthropic.py` | Injection rules, passthrough, recorded-response verification |
| Create | `tests/unit/cache/test_upstream_openai_gemini.py` | `cached_tokens` surfacing + CachedContent create/TTL/delete |
| Create | `shared/cache/affinity.py` | Ollama/llama.cpp KV session-affinity map in Valkey |
| Create | `tests/unit/cache/test_session_affinity.py` | Affinity map round-trip, TTL, org scoping |
| Modify | `shared/utils/llm_connectors.py` | Surface provider cache usage fields; accept `cached_content`/affinity hints |
| Create | `shared/cache/response_cache.py` | `ResponseCache` facade — exact → semantic → upstream orchestration (the file the spec names) |
| Modify | `proxy/apps/proxy_server/pipeline/stages.py` | `CacheStage` (stage 4) + post-security-out write-back hook |
| Modify | `proxy/apps/proxy_server/pipeline/__init__.py` | Insert `CacheStage` into pipeline order |
| Modify | `proxy/apps/proxy_server/main.py` | Construct `ResponseCache` at startup; `usage.waddleai` cache fields in both endpoint translations |
| Create | `tests/unit/proxy/test_cache_stage.py` | Stage wiring, short-circuit, flag-off skip, write-after-security-out |
| Modify | `shared/utils/metering.py` | Record `cache_status`/`tokens_saved` per event; aggregate into `token_usage` |
| Modify | `shared/utils/metrics.py` | Prometheus cache counters (hits/misses per layer, tokens saved) |
| Modify | `services/management/app/api/v1/usage.py` | `/api/v1/usage/cache-stats` — hit rates + $ saved per org/key |
| Create | `tests/unit/management/test_cache_stats_api.py` | Dashboard endpoint tests |
| Create | `tests/integration/test_response_cache_acceptance.py` | §6.5 acceptance suite incl. flag-off zero-behavior-change proof |

---

### Task 1: Migration 009a (§6 slice) + ORM models

Down-revision `008_model_registry`. Creates `cache_configs(id, scope_type, scope_ref, exact_enabled, semantic_enabled, semantic_threshold, ttl_seconds, max_entry_kb, anthropic_cache_control, created_at, updated_at)` and `response_cache_entries(id, org_id, scope_key, model_class, prompt_embedding vector(768), context_hash, response jsonb, hit_count, created_at, expires_at)` with an **HNSW** index on `prompt_embedding` (vector_cosine_ops) and a btree on `(org_id, model_class, expires_at)`; adds `token_usage.cache_status` (String(16), nullable) and `token_usage.tokens_saved` (Integer, default 0). Migration goes **first** (unlike the aiproxy plan's migrations-last order) because the semantic layer and config resolution in every later task run against these tables.

> **Ledger note:** §13.1 splits the old shared slot into **separate chained revisions**: this branch owns `009a_response_cache` (§6 tables only, `down_revision = "008_model_registry"`); the sibling `feature/proxy-memory-layers` branch owns `009b_proxy_memory` (§6A tables, same parent). **Whichever branch merges into `release/v0.2.X` second re-points its migration's `down_revision` at the other's revision id** (one-line edit + re-run the round-trip test) so `alembic heads` stays single-headed — matching the coordination note in the proxy-memory plan.

**Files:** Create `services/management/alembic/versions/009a_response_cache.py`, `tests/unit/management/test_migration_009a.py`. Modify `services/management/app/models_sqlalchemy.py`.

- [ ] **Step 1: Write failing round-trip test** — `tests/unit/management/test_migration_009a.py`, same harness as `test_migration_007/008`: on a seeded snapshot, `upgrade` → `cache_configs` and `response_cache_entries` exist with the exact §6.4/§6.2 column sets; `token_usage` has `cache_status` + `tokens_saved`; on Postgres the HNSW index exists (SQLite path: vector column degrades to JSON-serialized text per the existing `MemoryEmbedding` pattern — assert table + columns only, mark index assertion `postgres_only`); `downgrade` → both tables and both columns gone, 008 schema restored. Run → fails (no 009a).

- [ ] **Step 2: Implement migration 009a** — `op.create_table` for both tables; dialect-guarded `Vector(768)` (pgvector) vs `Text` fallback; `op.execute("CREATE INDEX ... USING hnsw (prompt_embedding vector_cosine_ops)")` inside a Postgres-only guard; `op.add_column` × 2 on `token_usage`. Complete `downgrade()`. Seed one global default row in `cache_configs` (`scope_type='global', scope_ref=NULL, exact_enabled=true, semantic_enabled=false, semantic_threshold=0.95, ttl_seconds=86400, max_entry_kb=256, anthropic_cache_control=true`).

- [ ] **Step 3: Add ORM models** — `CacheConfig` and `ResponseCacheEntry` classes in `models_sqlalchemy.py` (follow the `MemoryEmbedding` pgvector-column pattern); `TokenUsage.cache_status = Column(String(16), nullable=True)`, `TokenUsage.tokens_saved = Column(Integer, default=0)`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_009a.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` shows a single head `009a_...`.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/alembic/versions/009a_response_cache.py tests/unit/management/test_migration_009a.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 009a — cache_configs + response_cache_entries (pgvector/HNSW) + token_usage cache cols" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Eligibility predicates + exact-key derivation (`keys.py`)

The determinism-eligibility matrix (§6.1/§6.5) and the SHA-256 exact key over `(org_id, model class, normalized messages array, tools schema, temperature, top_p, max_tokens)`. The cache sits **before** routing (§3.2), so the model-class component is the client-requested `model` string — deterministic per request; when §7 lands, its resolved route replaces it behind the same function.

**Files:** Create `shared/cache/__init__.py`, `shared/cache/keys.py`, `tests/unit/cache/__init__.py`, `tests/unit/cache/test_eligibility_keys.py`.

- [ ] **Step 1: Write failing tests** — determinism-eligibility matrix: (a) `temperature == 0` + plain user/system messages → eligible; (b) `temperature` absent or `> 0` → ineligible; (c) any message with `role == "tool"` or a `tool_calls`/`tool_result` block → ineligible (tool-call results in messages, §6.1); (d) requests with `tools` **schema** present but no tool results remain exact-eligible (schema is part of the key); (e) streaming flag does not affect eligibility. Key tests: identical logical requests with different dict key order / whitespace-insignificant JSON produce the **same** key (canonical serialization); changing any of org_id, model, message content, tools schema, `top_p`, `max_tokens` produces a **different** key; two different orgs never share a key.

- [ ] **Step 2: Run tests, verify they fail** — `python3 -m pytest tests/unit/cache/test_eligibility_keys.py -v --no-cov` → `ModuleNotFoundError: shared.cache`.

- [ ] **Step 3: Implement** —
  ```python
  @dataclass(slots=True)
  class ExactKeyParts:
      org_id: int
      model_class: str
      messages: list
      tools: Optional[list]
      temperature: float
      top_p: Optional[float]
      max_tokens: Optional[int]

  def is_exact_eligible(body: dict) -> bool: ...        # temp==0, no tool-call results
  def derive_exact_key(parts: ExactKeyParts) -> str: ...  # sha256 over orjson.dumps(..., OPT_SORT_KEYS)
  ```
  Canonicalize with `orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)`; hex digest becomes the Valkey key suffix.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/ tests/unit/cache/
  git commit -m "feat(cache): eligibility matrix + SHA-256 exact-key derivation" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Exact Valkey cache (`exact.py`) + org-isolation security test

Org-scoped Valkey entries: `waddleai:cache:exact:{org_id}:{sha256}` — org_id is baked into both the hash input (Task 2) **and** the key namespace (defense in depth). TTL 24h default from resolved config; `max_entry_kb` write bound; per-org memory quota with LRU eviction via a per-org ZSET (`waddleai:cache:idx:{org_id}`, score = last-access epoch) plus a byte counter (`waddleai:cache:bytes:{org_id}`).

**Files:** Create `shared/cache/exact.py`, `tests/unit/cache/test_exact_cache.py`, `tests/unit/cache/test_exact_cache_isolation.py`.

- [ ] **Step 1: Write failing tests** — `test_exact_cache.py` (fakeredis.aioredis): (a) `put` then `get` round-trips the full response JSON + usage; (b) `get` on a missing key → `None`; (c) TTL honored (fakeredis time travel / `pexpire` assertion at configured `ttl_seconds`); (d) an entry larger than `max_entry_kb` is **not** written (put returns `False`, no key created); (e) writes past the per-org quota evict the least-recently-accessed entries for **that org only** until under quota (assert evicted key gone, other org's entries untouched, byte counter consistent); (f) `get` refreshes the ZSET access score.
  `test_exact_cache_isolation.py` (**security test — mark `@pytest.mark.security`**): identical logical request bodies cached for org A and org B create two distinct keys; org B's `get` never returns org A's entry; direct probe: constructing org A's key from org B's context is impossible without org A's id (assert key namespace prefix); flushing org A's namespace leaves org B intact.

- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'ExactCache'`.

- [ ] **Step 3: Implement `ExactCache`** —
  ```python
  @dataclass(slots=True)
  class CachedResponse:
      response: dict          # full provider-shaped response JSON
      usage: dict             # original usage block (source of tokens_saved)
      stored_at: float

  class ExactCache:
      def __init__(self, valkey) -> None: ...
      async def get(self, org_id: int, key: str) -> Optional[CachedResponse]: ...
      async def put(self, org_id: int, key: str, value: CachedResponse,
                    ttl_seconds: int, max_entry_kb: int, org_quota_kb: int) -> bool: ...
  ```
  orjson payloads; pipeline (`MULTI`) for put+ZADD+INCRBY atomicity; eviction loop pops lowest-score members, `DEL`s and `DECRBY`s. Org quota default from env `CACHE_ORG_QUOTA_KB` (config plumbing arrives Task 5).

- [ ] **Step 4: Run tests, verify pass** — including the security-marked module; full suite tail `python3 -m pytest tests/unit/cache/ -v --no-cov 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/exact.py tests/unit/cache/test_exact_cache.py tests/unit/cache/test_exact_cache_isolation.py
  git commit -m "feat(cache): exact Valkey layer with TTL/size/org-quota LRU + org-isolation security tests" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Streaming synthetic-SSE replay (`replay.py`)

Streaming hits must be indistinguishable from a miss (§6.1): a cached full response is decomposed into synthetic SSE chunks in the **requesting endpoint's** wire format — OpenAI `chat.completion.chunk` frames ending in `data: [DONE]`, or the Anthropic event sequence (`message_start` → `content_block_start` → `content_block_delta`* → `content_block_stop` → `message_delta` → `message_stop`).

**Files:** Create `shared/cache/replay.py`, `tests/unit/cache/test_streaming_replay.py`.

- [ ] **Step 1: Write failing tests** — (a) **byte-equivalence**: for a cached OpenAI-shaped response, concatenating the `delta.content` of all replayed chunks reproduces the cached `choices[0].message.content` exactly, first chunk carries `role`, last carries `finish_reason`, stream terminates with `data: [DONE]\n\n`; (b) same property for Anthropic framing (assembled `content_block_delta.text` == cached `content[0].text`; `message_delta` carries `stop_reason` and usage); (c) replayed usage equals the cached usage (client sees identical token accounting); (d) tool_use content blocks in a cached Anthropic response replay as `input_json_delta` events; (e) chunk `id`/`created` fields are consistent across all frames of one replay.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `replay_openai_sse(cached: CachedResponse) -> AsyncIterator[bytes]` and `replay_anthropic_sse(cached: CachedResponse) -> AsyncIterator[bytes]`; deterministic chunking (split content on a fixed size), zero sleeps (replay at full speed — latency is the point).

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/replay.py tests/unit/cache/test_streaming_replay.py
  git commit -m "feat(cache): synthetic-SSE replay of cached responses (OpenAI + Anthropic framing)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: Cache-config resolution (`config.py`) + management CRUD

`cache_configs` resolution with precedence **virtual-key > org > global default**, Valkey-cached hot path (`waddleai:cache:cfg:{scope_type}:{scope_ref}`, short TTL) invalidated on Management writes — proxy reads config from Postgres with Valkey caching, no Management→Proxy RPC (§3.3). Management gains CRUD at `/api/v1/cache-configs`.

**Files:** Create `shared/cache/config.py`, `services/management/app/api/v1/cache_configs.py`, `tests/unit/cache/test_cache_config.py`, `tests/unit/management/test_cache_config_api.py`. Modify `services/management/app/api/v1/__init__.py` (register blueprint).

- [ ] **Step 1: Write failing tests** — `test_cache_config.py`: (a) no rows beyond the seeded global default → resolved config is the §6 defaults (`exact_enabled=True, semantic_enabled=False, semantic_threshold=0.95, ttl_seconds=86400, anthropic_cache_control=True`); (b) an org row overrides global; a key row overrides org (field-level merge: unset key fields fall through to org, then global); (c) resolution is served from Valkey on the second call (assert one DB read across two resolves); (d) `invalidate(scope_type, scope_ref)` busts the cached entry (next resolve re-reads DB). `test_cache_config_api.py`: CRUD happy paths + validation (threshold ∈ [0.5, 1.0], ttl > 0), scope uniqueness conflict → 409, writes call invalidation, org-scoped callers cannot write another org's row (403), admin-scope required for global row.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** —
  ```python
  @dataclass(slots=True)
  class ResolvedCacheConfig:
      exact_enabled: bool
      semantic_enabled: bool
      semantic_threshold: float
      ttl_seconds: int
      max_entry_kb: int
      anthropic_cache_control: bool

  class CacheConfigResolver:
      def __init__(self, db, valkey) -> None: ...
      async def resolve(self, org_id: int, vkey_id: Optional[int]) -> ResolvedCacheConfig: ...
      async def invalidate(self, scope_type: str, scope_ref: Optional[str]) -> None: ...
  ```
  penguin-dal reads (runtime rule — no SQLAlchemy queries); management blueprint async per house Quart standards, invalidation via the shared Valkey client.

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/config.py services/management/app/api/v1/cache_configs.py services/management/app/api/v1/__init__.py tests/unit/cache/test_cache_config.py tests/unit/management/test_cache_config_api.py
  git commit -m "feat(cache): cache_configs resolution (key>org>global) + management CRUD with Valkey invalidation" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Semantic pgvector cache (`semantic.py`) — restricted, default OFF

`response_cache_entries` lookup: embed the **last user message** (768-dim, via the existing async embedding path backed by `nomic-embed-text`) + a rolling SHA-256 `context_hash` of prior turns; a hit requires `org_id` match, `model_class` match, `context_hash` match, cosine similarity ≥ threshold (0.95 default, per-org tunable), and `expires_at` in the future. Eligibility is **strictly narrower** than exact: single-turn or last-turn-only, no `tools` schema at all, no memory injection, `temperature == 0`, and classified informational/Q&A.

> **§7 dependency note:** "router-classified informational" — the §7 routing engine/classifier doesn't exist on this branch. `SemanticEligibility` takes an injected `classify_intent: Callable[[str], str]` with a conservative heuristic default (question-shaped, no imperative code-gen markers → `informational`; everything else ineligible). The §7 branch swaps in its real classifier behind the same callable. The layer is default OFF regardless, so the interim heuristic gates nothing in production.

**Files:** Create `shared/cache/semantic.py`, `tests/unit/cache/test_semantic_cache.py`, `tests/unit/cache/test_semantic_cache_isolation.py`.

- [ ] **Step 1: Write failing tests** — `test_semantic_cache.py` (stub embedder returning fixed vectors; stub DAL, plus a `postgres_only`-marked real-pgvector path): **restriction matrix** — each of {multi-turn beyond last-turn-only, tools present, memory-injected ctx, temp>0, non-informational classification} independently makes the request ineligible; **should-hit corpus** — paraphrase pairs (fixture file `tests/unit/cache/fixtures/semantic_corpus.json`, labeled `should_hit`/`should_miss`) with stub similarities above/below threshold: every `should_hit` pair hits at 0.95, every `should_miss` pair misses (**threshold regression guard** — this corpus is the tripwire for future threshold changes); threshold is read from resolved config (org override to 0.98 flips a borderline pair to miss); `context_hash` mismatch → miss even at similarity 1.0; write path inserts with embedding + expiry and increments `hit_count` on hit; expired entries never match. `test_semantic_cache_isolation.py` (**security test — `@pytest.mark.security`**): entries written for org A are invisible to org B's lookup even for identical prompts/embeddings (assert the query always carries `org_id = :caller_org` — inspect the DAL query or use two-org seeded data); no query path exists that omits the org filter.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement `SemanticCache`** —
  ```python
  class SemanticCache:
      def __init__(self, db, embedder, classify_intent) -> None: ...
      async def lookup(self, org_id: int, model_class: str, last_user_msg: str,
                       context_hash: str, threshold: float) -> Optional[CachedResponse]: ...
      async def put(self, org_id: int, model_class: str, last_user_msg: str,
                    context_hash: str, response: CachedResponse, ttl_seconds: int) -> None: ...

  def is_semantic_eligible(body: dict, ctx_flags: "CtxFlags", classify_intent) -> bool: ...
  ```
  Cosine distance via pgvector `<=>` (Postgres) with a brute-force Python fallback for SQLite tests; embedding call is async network I/O (never on-loop CPU, §3.5).

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/semantic.py tests/unit/cache/test_semantic_cache.py tests/unit/cache/test_semantic_cache_isolation.py tests/unit/cache/fixtures/
  git commit -m "feat(cache): restricted semantic pgvector cache (0.95 threshold, should-hit/should-miss corpus, org-scoped)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Upstream — Anthropic `cache_control` auto-injection (`upstream.py`)

The biggest token win (§6.3). Track stable-prefix hashes per virtual key in Valkey (`waddleai:cache:prefix:{vkey_id}:{prefix_sha}`, counter with 1h TTL); when a prefix (system blocks + tools schema + leading stable messages) exceeds **1024 tokens** and has been observed **≥2×**, auto-inject `cache_control: {"type": "ephemeral"}` on the last block of the qualifying prefix (respecting Anthropic's 4-breakpoint maximum). **Default ON**, per-org toggle via `cache_configs.anthropic_cache_control`. Client-supplied `cache_control` anywhere in the request → auto-injection fully disabled for that request, payload passes through untouched.

**Files:** Create `shared/cache/upstream.py`, `tests/unit/cache/test_upstream_anthropic.py`, fixture `tests/unit/cache/fixtures/anthropic_cached_response.json` (recorded response carrying `cache_creation_input_tokens`/`cache_read_input_tokens`).

- [ ] **Step 1: Write failing tests** — (a) first observation of a >1024-token prefix (tiktoken-estimated) injects nothing but increments the Valkey counter; second identical prefix → `cache_control` breakpoint injected on the correct block; (b) prefixes ≤1024 tokens are never injected regardless of count; (c) client-supplied `cache_control` on any block → request forwarded **byte-identical** (no injection, no counter side effects on their blocks); (d) `anthropic_cache_control=False` in resolved config → no tracking, no injection; (e) never more than 4 breakpoints; (f) **recorded-response verification**: feeding the recorded fixture through the usage extractor surfaces `cached_tokens = cache_read_input_tokens` into the `usage.waddleai` structure (proves the wiring reads what Anthropic actually reports — live re-record is a nightly/manual step, not CI).

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `AnthropicPromptCacheOrchestrator` with `annotate_request(body, vkey_id, cfg) -> body` and `extract_cache_usage(provider_usage) -> tuple[int, int]`; prefix = contiguous stable leading segment (system + tools + messages up to the first non-repeating turn), hashed with SHA-256; token estimate via tiktoken (approximation documented — Anthropic's own tokenizer differs, 1024 is a floor heuristic). Wire `AnthropicConnector` to pass annotated bodies through untouched and return the cache usage fields in its normalized `usage`.

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/upstream.py shared/utils/llm_connectors.py tests/unit/cache/test_upstream_anthropic.py tests/unit/cache/fixtures/anthropic_cached_response.json
  git commit -m "feat(cache): Anthropic cache_control auto-injection with prefix tracking + untouched client passthrough" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: Upstream — OpenAI `cached_tokens` surfacing + Gemini CachedContent

OpenAI caches automatically upstream — surface `usage.prompt_tokens_details.cached_tokens` into `usage.waddleai`. Gemini: explicitly create `CachedContent` for repeated large prefixes above threshold; the cache module owns the lifecycle (create, TTL, delete on expiry/eviction), and dispatch passes the cached-content name on subsequent calls.

**Files:** Modify `shared/cache/upstream.py`, `shared/utils/llm_connectors.py`. Create `tests/unit/cache/test_upstream_openai_gemini.py`.

- [ ] **Step 1: Write failing tests** — OpenAI: a mocked completion response containing `prompt_tokens_details: {cached_tokens: N}` yields `cached_tokens == N` from the extractor; absent details → 0 (no KeyError). Gemini (mocked `google-genai` client): (a) same >threshold prefix observed ≥2× → `caches.create` called once with the prefix content and configured TTL, mapping stored in Valkey (`waddleai:cache:gemini:{vkey_id}:{prefix_sha}` → cached-content name); (b) subsequent matching request dispatches with `cached_content=<name>` and does **not** re-create; (c) lifecycle: `expire`/`delete` invoked when the Valkey mapping TTL lapses or config disables the feature; (d) usage extractor surfaces Gemini's `cached_content_token_count`.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `extract_openai_cached_tokens(usage) -> int`; `GeminiCachedContentManager` (create/lookup/delete, Valkey-mapped, reuses Task 7's prefix tracker); `GeminiConnector.chat_completion`/`stream_chat_completion` accept an optional `cached_content` kwarg.

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/upstream.py shared/utils/llm_connectors.py tests/unit/cache/test_upstream_openai_gemini.py
  git commit -m "feat(cache): OpenAI cached_tokens surfacing + Gemini CachedContent lifecycle management" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Upstream — Ollama/llama.cpp KV session-affinity map (`affinity.py`)

KV-cache reuse for the local fleet: same conversation/prefix hash → same fleet pod, via a Valkey affinity map (`waddleai:affinity:{org_id}:{conv_or_prefix_sha}` → backend/pod identifier, sliding TTL). This branch owns the map + the router hint; the §7 dispatch honors it fully (today's `request_router` receives a `preferred_backend` hint and prefers it when the backend is healthy).

**Files:** Create `shared/cache/affinity.py`, `tests/unit/cache/test_session_affinity.py`. Modify `shared/utils/request_router.py` (accept + prefer `preferred_backend` hint when healthy).

- [ ] **Step 1: Write failing tests** — (a) `record(org_id, session_hash, backend_id)` then `lookup(...)` returns `backend_id`; sliding TTL refreshed on lookup; (b) lookup after TTL expiry → `None`; (c) affinity keys are org-namespaced (org B lookup of org A's session hash → `None`); (d) router: given a `preferred_backend` hint pointing at a healthy Ollama backend, selection returns that backend; hint pointing at a circuit-broken backend is ignored (normal selection proceeds — affinity must never pin to a dead pod); (e) hint only applies to `ollama`/`llamacpp` provider types.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — `SessionAffinityMap` (fakeredis-tested, `SETEX`/`GETEX` sliding TTL, default 30 min); session hash = conversation id header if present else the Task 7 prefix hash; small router patch threading the hint through `_select_provider`.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/ -k router --no-cov --tb=short 2>&1 | tail -5` (no router regressions).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/affinity.py shared/utils/request_router.py tests/unit/cache/test_session_affinity.py
  git commit -m "feat(cache): Ollama/llama.cpp KV session-affinity map in Valkey + router preferred-backend hint" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: `ResponseCache` facade + `CacheStage` pipeline wiring + `usage.waddleai`

The file the spec names — `shared/cache/response_cache.py` — orchestrates exact → semantic → upstream, and `CacheStage` slots it in as pipeline stage 4 (after `SecurityInStage`, before `DispatchStage`). **Poisoning defense (§3.6):** the write-back closure captures the **post-security-filter** messages (what `SecurityInStage` left in `ctx`) for key derivation, and is executed only after `SecurityOutStage` passes — a blocked or modified-out response is never cached. Response `usage` gains the additive-only `waddleai` object: `{cache: "exact"|"semantic"|"upstream"|"miss", cached_tokens, tokens_saved}`. Metering records `cache_status`/`tokens_saved` (hits still meter — with `tokens_saved` = the original response's total tokens; upstream `tokens_saved` = provider-reported `cached_tokens`, an approximation documented in the module docstring).

**Files:** Create `shared/cache/response_cache.py`, `tests/unit/proxy/test_cache_stage.py`. Modify `proxy/apps/proxy_server/pipeline/stages.py`, `proxy/apps/proxy_server/pipeline/__init__.py`, `proxy/apps/proxy_server/main.py`, `shared/utils/metering.py`.

- [ ] **Step 1: Write failing tests** — `tests/unit/proxy/test_cache_stage.py`: (a) flag `waddleai.response_cache` OFF → stage logged `skipped`, request flows to dispatch unmodified, **zero Valkey/pg calls** (assert on spies); (b) exact-eligible request, warm cache → `ctx.response` set, `DispatchStage` short-circuited (stage-log shows it never ran), `usage.waddleai.cache == "exact"`, `tokens_saved` = cached usage total; (c) streaming variant of (b) → `ctx.stream_iter` is the synthetic replay (Task 4); (d) miss → dispatch runs; write-back fires **after** `SecurityOutStage` success and the entry appears in the exact layer keyed on post-filter messages; (e) `SecurityOutStage` blocks the response → **no cache write** (poisoning-defense regression guard); (f) semantic layer consulted only when `semantic_enabled` and exact missed; (g) on miss with Anthropic target + toggle ON, the dispatched body carries the Task 7 annotation; Ollama target sets the affinity hint; (h) ineligible request (temp>0) → stage runs but performs no lookup, `usage.waddleai.cache == "miss"`; (i) metering event carries `cache_status`/`tokens_saved`; (j) both endpoints emit the identical `usage.waddleai` shape (additive-only — existing usage fields untouched).

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** —
  ```python
  @dataclass(slots=True)
  class CacheLookupResult:
      status: str                      # exact | semantic | miss
      cached: Optional[CachedResponse]
      write_back: Optional[Callable[[dict, dict], Awaitable[None]]]

  class ResponseCache:
      def __init__(self, exact, semantic, upstream, affinity, resolver, features) -> None: ...
      async def lookup(self, ctx) -> CacheLookupResult: ...
      async def annotate_miss(self, ctx) -> None: ...     # Anthropic breakpoints, Gemini ref, affinity hint

  def create_response_cache(db, valkey, embedder, features) -> ResponseCache: ...
  ```
  `CacheStage` (flag `response_cache`) in `stages.py`; `MeterStage` extended to consume `ctx.cache_status`/`ctx.tokens_saved` and flush into the new `token_usage` columns; endpoint translation in `main.py` appends `usage["waddleai"]` in both OpenAI and Anthropic response shapes; `ProxyPipeline` construction gains the stage in position 4.

- [ ] **Step 4: Run tests, verify pass** — plus `make test-contract 2>&1 | tail -20` (snapshots stay green: `usage.waddleai` is additive-only per §14.2, flag is OFF in the contract environment so responses are byte-identical).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/cache/response_cache.py proxy/apps/proxy_server/pipeline/ proxy/apps/proxy_server/main.py shared/utils/metering.py tests/unit/proxy/test_cache_stage.py
  git commit -m "feat(proxy): CacheStage (pipeline stage 4) — exact/semantic/upstream orchestration + usage.waddleai" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Dashboard metrics — Prometheus counters + `/api/v1/usage/cache-stats`

Hit rates and $ saved per org/key (§6.4). Prometheus: `waddleai_cache_lookups_total{layer, result}`, `waddleai_cache_tokens_saved_total{layer}`, `waddleai_cache_entries_evicted_total`. Management: `GET /api/v1/usage/cache-stats?org_id=&virtual_key_id=&window=` aggregating `token_usage.cache_status`/`tokens_saved` (and cost model → $ saved) — this endpoint is the data source for the WebUI dashboard panel (no frontend exists in-repo; the API is the deliverable).

**Files:** Modify `shared/utils/metrics.py`, `services/management/app/api/v1/usage.py`. Create `tests/unit/management/test_cache_stats_api.py`.

- [ ] **Step 1: Write failing tests** — metrics: counters increment on hit/miss/eviction paths (drive via `ResponseCache` with fakeredis, scrape the registry). API: seeded `token_usage` rows with mixed `cache_status` values → response reports per-layer hit counts, hit rate, `tokens_saved` totals, and `$ saved` (tokens_saved × the token_manager cost model); filters by org and virtual key; org-scoped callers see only their org (403 on cross-org query — reuse the existing usage-endpoint auth pattern); empty window → zeroes, not errors.

- [ ] **Step 2: Run tests, verify they fail.**

- [ ] **Step 3: Implement** — counter definitions in `get_proxy_metrics()`; increments inside `ResponseCache`/`ExactCache`; async aggregate endpoint via penguin-dal grouped queries.

- [ ] **Step 4: Run tests, verify pass**; full suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/utils/metrics.py services/management/app/api/v1/usage.py tests/unit/management/test_cache_stats_api.py
  git commit -m "feat(cache): Prometheus cache metrics + /api/v1/usage/cache-stats dashboard endpoint" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: §6.5 acceptance suite + flag-off proof + gates

Every §6.5 acceptance item as an explicit test, in one integration module run against the live-ish proxy fixture (contract-test harness style: temp SQLite/pg, fakeredis or real Valkey, stubbed upstream connectors).

**Files:** Create `tests/integration/test_response_cache_acceptance.py`.

- [ ] **Step 1: Determinism-eligibility matrix E2E** — the Task 2 matrix exercised through the full pipeline: eligible request twice → second is a hit (`usage.waddleai.cache == "exact"`); each ineligible variant twice → both misses.
- [ ] **Step 2: Streaming replay byte-equivalence E2E** — same request streamed on miss then on hit: assembled content and final usage identical across both, client-observable framing valid in both endpoint formats.
- [ ] **Step 3: TTL expiry** — entry with `ttl_seconds=1` (or clock-advanced fakeredis) no longer hits after expiry.
- [ ] **Step 4: Org-isolation (SECURITY)** — `@pytest.mark.security`: two orgs, identical requests through the full pipeline; org B's request after org A's warm-up is a **miss** on both exact and semantic layers; `pytest -m security tests/ -v --no-cov` green.
- [ ] **Step 5: Semantic corpus + threshold regression** — the labeled should-hit/should-miss corpus (Task 6 fixture) run at 0.95 through the pipeline with semantic enabled for the test org; any classification flip fails the build.
- [ ] **Step 6: `cache_control` injection vs recorded Anthropic responses** — E2E over the stub connector loaded with the recorded fixture: injected request produces `usage.waddleai.cached_tokens > 0` sourced from `cache_read_input_tokens`; client-supplied `cache_control` request passes through byte-identical.
- [ ] **Step 7: Flag-off zero-behavior-change proof (§14.2)** — with `waddleai.response_cache` forced OFF: repeated identical requests all dispatch upstream (call-count assertion on the stub connector), responses carry **no** `usage.waddleai.cache` field beyond pre-existing shape, no Valkey cache keys created, no `response_cache_entries` rows, contract snapshots green — `make test-contract 2>&1 | tail -20`.
- [ ] **Step 8: Blocked-response never cached** — output-filter-blocked response followed by an identical request → second request dispatches upstream again (poisoning defense, §3.6).
- [ ] **Step 9: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` (≥90% on changed modules, §14.2); lint clean (`make lint`).
- [ ] **Step 10: Commit**
  ```bash
  git add tests/integration/test_response_cache_acceptance.py
  git commit -m "test(cache): §6.5 acceptance suite — eligibility E2E, replay, TTL, org-isolation security, threshold regression, flag-off proof" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §6

| Spec §6 requirement | Task |
|---|---|
| §6.1 SHA-256 key over (org, model class, messages, tools, temp, top_p, max_tokens) | 2 |
| §6.1 `temperature == 0` + no-tool-results eligibility, default ON | 2, 10 |
| §6.1 TTL 24h default, per-org/key configurable; `max_entry_kb` + per-org quota LRU | 3, 5 |
| §6.1 streaming hits replay as synthetic SSE (indistinguishable from miss) | 4, 12 |
| §6.1/§3.6 org-scoped entries, post-filter content only (poisoning defense) | 3, 10, 12 |
| §6.2 `response_cache_entries` pgvector(768) + HNSW | 1, 6 |
| §6.2 default OFF; single-turn/no-tools/temp-0/informational restriction; 0.95 threshold per-org tunable | 5, 6 |
| §6.3 Anthropic `cache_control` auto-inject (>1024 tok, ≥2×, default ON, per-org toggle, passthrough untouched) | 7 |
| §6.3 OpenAI `cached_tokens` surfaced | 8 |
| §6.3 Gemini CachedContent lifecycle | 8 |
| §6.3 Ollama/llama.cpp KV session-affinity map in Valkey | 9 |
| §6.4 `usage.waddleai` `{cache, cached_tokens, tokens_saved}` additive-only | 10 |
| §6.4 `token_usage.cache_status`/`tokens_saved` | 1, 10 |
| §6.4 `cache_configs` table + resolution | 1, 5 |
| §6.4 dashboard hit rates + $ saved per org/key | 11 |
| §6.5 determinism-eligibility matrix | 2, 12 |
| §6.5 streaming replay byte-equivalence | 4, 12 |
| §6.5 TTL expiry | 12 |
| §6.5 **org-isolation as security test** | 3, 6, 12 |
| §6.5 semantic should-hit/should-miss corpus + threshold regression | 6, 12 |
| §6.5 `cache_control` verified against recorded Anthropic responses | 7, 12 |
| §6.5/§14.2 flag-off proves zero behavior change | 10, 12 |
| §13.1 migration 009a round-trip + downgrade | 1 |
| §14.5 flag `waddleai.response_cache`, fail-safe OFF via `features.enabled` | 10, 12 |
| §3.2 stage-4 placement (after security-in, before routing/dispatch) | 10 |
