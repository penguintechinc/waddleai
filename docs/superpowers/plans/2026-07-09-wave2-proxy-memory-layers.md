# Proxy Memory & Context-Efficiency Layers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/proxy-memory-layers` (off `release/v0.2.X`). **Depends on:** `feature/aiproxy-migration` (the stage-class `ProxyPipeline` in `proxy/apps/proxy_server/pipeline/` exists; both `/v1/chat/completions` and `/v1/messages` run it; `shared/licensing/features.py::features.enabled(...)` exists; Valkey is deployed).

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §6A (with §9.6/§9.7 memory scoping/trust/isolation — MUST be honored on every layer; §6.3 prefix-cache feed; §7.1 `summarize` assignment; §13.1 migration 008; §14.2/§14.5). Authoritative.

**⚠️ Migration-008 coordination (flagged):** §13.1 assigns one migration `008 cache_and_proxy_memory` to *both* the `feature/response-cache` branch (§6 tables) and this branch (§6A tables). To keep the branches independent, this plan writes the §6A memory tables as their **own Alembic revision `008b_proxy_memory`** with `down_revision = "007_model_registry"`. The response-cache branch owns `008a_response_cache` (same parent). **Whichever branch merges into `release/v0.2.X` second MUST re-point its migration's `down_revision` at the other's revision id** (one-line edit + re-run the round-trip test) so `alembic heads` stays single-headed. Task 1 encodes this as an explicit check.

---

**Goal:** Land the four §6A proxy memory / context-efficiency layers behind PostHog flag `waddleai.proxy_memory` (default OFF, fail-safe OFF): (1) **session scratchpad** — Valkey-hot / Postgres-durable KV isolated per (org, session, user), exposed as MCP tools `scratchpad_put/get/list` and as an opt-in plain-client reference-marker substitution keyed by `X-WaddleAI-Session`; (2) **rolling conversation summarization** — threshold-triggered distillation of older turns via the §7.1 `summarize` model seam (cheap local default), injecting `summary + recent-N turns` while originals stay retrievable, reported as `usage.waddleai.summarized` + `tokens_elided`; (3) **embedding cache** keyed `(model, content_hash)` (Valkey→Postgres pgvector) plus a Valkey-only **retrieval-result cache** keyed `(org, query_hash, corpus_version, top_k)` with TTL + corpus-version invalidation; (4) **tool-schema / system-prompt dedup store** — canonical blocks by content-hash per (org, session), intra-request duplicate elision before dispatch *and before token counting*, a tokenizer-length cache, and prefix-hash observations feeding §6.3 upstream prompt-cache orchestration. All layers are org-scoped, provenance-tagged, and injection-safe per §9.7; **isolation tests are security tests**.

**Architecture:** Everything lives in a new `shared/memory/` package consumed by three new pipeline stages in `proxy/apps/proxy_server/pipeline/memory_stages.py` — `ScratchpadStage`, `SummarizationStage`, `DedupStage` — inserted **after `SecurityInStage`, before `DispatchStage`** (context assembly happens on post-security-filter content, poisoning defense §3.6; relative order vs the §6 `CacheStage` is settled at merge: memory assembly first, so cache keys hash what would actually be dispatched). §9.7 is enforced structurally by two shared primitives every layer must route through: `filter_on_write(...)` (tiers 1–3 scan before any persist — injection payloads are quarantined, never stored clean) and `recall(...)` (tiers 1–3 re-filter on read + wrap as a provenance-headed quoted-data block — recalled content is data, never instruction, never a system/developer message). Every stored row carries `(scope_type, scope_ref, author_user_id, trust_tier, version, superseded_by, status, expires_at)`; scratchpad/summaries live at **session scope, trust `unverified`** — never auto-promoted. Hot state in Valkey (stateless pods), durable state in Postgres via penguin-dal; Alembic is sole schema authority. The `EmbeddingManager.embed` backend is blocking — cache misses call it via `asyncio.to_thread` (never on the event loop, §3.5).

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), Valkey 8 (redis-py asyncio), pgvector (768-dim, `nomic-embed-text` default), orjson, pytest + pytest-asyncio, fakeredis (unit).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/alembic/versions/008b_proxy_memory.py` | Migration 008b: `session_scratchpad`, `conversation_summaries`, `embedding_cache` (+§9.7 columns); §9.7 columns on `memory_embeddings`; `api_keys.proxy_memory` config |
| Create | `tests/unit/management/test_migration_008b.py` | Round-trip + downgrade on seeded snapshot; single-head check |
| Modify | `services/management/app/models_sqlalchemy.py` | `SessionScratchpad`, `ConversationSummary`, `EmbeddingCacheEntry` models; `MemoryEmbedding` §9.7 cols; `APIKey.proxy_memory` |
| Create | `shared/memory/__init__.py` | Package exports |
| Create | `shared/memory/provenance.py` | `ProvenanceTag`, `filter_on_write`, `recall` (re-filter + quoted-data wrap) — §9.6/§9.7 enforcement primitives |
| Create | `shared/memory/config.py` | Per-key `proxy_memory` config resolution + `features.enabled("proxy_memory")` gate |
| Create | `tests/unit/memory/test_provenance.py`, `test_memory_config.py` | Injection-safety + config/flag tests |
| Create | `shared/memory/scratchpad.py` | `ScratchpadStore`: Valkey hot / Postgres spill KV, per-(org,session,user) isolation, limits |
| Create | `tests/unit/memory/test_scratchpad.py` | Round-trip + **isolation security tests** |
| Create | `shared/memory/scratchpad_tools.py` | Transport-agnostic `scratchpad_put/get/list` tool handlers + MCP registration |
| Create | `tests/unit/memory/test_scratchpad_tools.py` | Tool contract + auth-context isolation tests |
| Create | `shared/memory/token_len_cache.py` | Tokenizer-length cache: token counts of stable blocks keyed `(model, content_hash)` (Valkey) |
| Create | `tests/unit/memory/test_token_len_cache.py` | Correctness-vs-fresh-count + hit tests |
| Create | `shared/memory/summarizer.py` | `ConversationSummarizer`: threshold, distill via `summarize` model seam, persistence, versioning |
| Create | `tests/unit/memory/test_summarizer.py` | Threshold / keep-recent-N / retrievability tests |
| Create | `shared/memory/embedding_cache.py` | `CachedEmbedder` wrapping `EmbeddingManager`: `(model, content_hash)` Valkey→Postgres |
| Create | `tests/unit/memory/test_embedding_cache.py` | Call-count assertion (hit avoids re-embed) |
| Create | `shared/memory/retrieval_cache.py` | Valkey-only result cache `(org, query_hash, corpus_version, top_k)` + corpus-version bump API |
| Create | `tests/unit/memory/test_retrieval_cache.py` | Hit/miss/TTL/invalidation + org-isolation security test |
| Create | `shared/memory/dedup_store.py` | Canonical block store by content-hash per (org, session); intra-request elision; §6.3 prefix-hash feed |
| Create | `tests/unit/memory/test_dedup_store.py` | Elision + token-reduction + isolation tests |
| Create | `proxy/apps/proxy_server/pipeline/memory_stages.py` | `ScratchpadStage`, `SummarizationStage`, `DedupStage` |
| Create | `tests/unit/proxy/test_memory_stages.py` | Stage behavior + flag-off no-op per stage |
| Modify | `shared/utils/memory_integration.py` | Route embeddings through `CachedEmbedder`; retrieval-result cache + corpus-version bumps in search/store paths |
| Modify | `shared/utils/mcp_interface.py` | Register scratchpad tools in the tool registry (guarded; MCP v2 re-exposes in §11) |
| Modify | `proxy/apps/proxy_server/main.py` | Insert memory stages into the pipeline build; `X-WaddleAI-Session` plumbed into `PipelineContext` |
| Create | `tests/unit/proxy/test_memory_pipeline_wiring.py` | Both-endpoint stage-log + `usage.waddleai` additive + whole-feature flag-off proof |
| Create | `tests/integration/test_proxy_memory_acceptance.py` | §6A.6 acceptance suite |

---

### Task 1: Migration 008b — proxy-memory tables with §9.7 scope/trust columns

Creates the three §6A.5 tables plus the §9.7 scoping/trust/attribution columns (`scope_type`, `scope_ref`, `author_user_id`, `trust_tier`, `version`, `superseded_by`, `status`, `expires_at`) on `session_scratchpad` and `conversation_summaries`, and retrofits the same §9.7 columns onto the existing `memory_embeddings` (spec: "folded into migrations 008/011" — `rag_documents`/`code_chunks` belong to 011 on the knowledge branch). `embedding_cache` follows the §6A.5 schema exactly — `(model, content_hash, embedding vector(768), created_at)`, content-addressed, **no stored plaintext and no org column**: it is a deterministic function cache holding only vectors; a caller must already possess the content to compute the key, so no org-readable data can leak (the org boundary for readable content is enforced on the retrieval-result cache, Task 10). Adds `api_keys.proxy_memory JSONB` (nullable = feature defaults) for the §6A.5 per-key config block. Down-revision `007_model_registry` — **see the migration-008 coordination note in the header**.

**Files:** Create `services/management/alembic/versions/008b_proxy_memory.py`, `tests/unit/management/test_migration_008b.py`. Modify `services/management/app/models_sqlalchemy.py`.

- [ ] **Step 1: Write failing round-trip test** — `tests/unit/management/test_migration_008b.py` on a seeded snapshot: `upgrade` creates `session_scratchpad(id, org_id, user_id, session_id, key, value, scope_type default 'session', scope_ref, author_user_id, trust_tier default 'unverified', version, superseded_by, status default 'active', created_at, updated_at, expires_at)` with a unique index on `(org_id, session_id, user_id, key)`; `conversation_summaries(id, conversation_id, org_id, summary, covers_through_turn, tokens_summarized, model_used, + the same §9.7 columns, updated_at)` unique on `(org_id, conversation_id, version)`; `embedding_cache(id, model, content_hash, embedding vector(768), created_at)` unique on `(model, content_hash)` (pgvector column; TEXT-serialized fallback asserted on SQLite); `memory_embeddings` gains the eight §9.7 columns with backfill defaults (`scope_type='session'`, `trust_tier='unverified'`, `status='active'`); `api_keys.proxy_memory` exists. `downgrade` returns the exact 007 shape. Assert `alembic heads` is single-headed. Run → fails (no 008b).

- [ ] **Step 2: Implement migration 008b** — guarded `op.create_table`/`op.add_column`; pgvector `Vector(768)` with a dialect guard (TEXT on SQLite for unit tests, matching existing project pattern); complete `downgrade()`. Top-of-file comment: `# COORDINATION: down_revision must be re-pointed at 008a_response_cache if that branch merges first (§13.1 shared migration 008).`

- [ ] **Step 3: Add ORM models** — `SessionScratchpad`, `ConversationSummary`, `EmbeddingCacheEntry` classes; §9.7 columns on `MemoryEmbedding`; `APIKey.proxy_memory = Column(JSON, nullable=True)`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_008b.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` → single head.

- [ ] **Step 5: Commit**
  ```bash
  git add services/management/alembic/versions/008b_proxy_memory.py tests/unit/management/test_migration_008b.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 008b — proxy memory tables with §9.7 scope/trust columns" \
             -m "Coordination: shares §13.1 migration-008 slot with feature/response-cache (008a); second-to-merge re-points down_revision." \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Injection-safety + config primitives (`provenance.py`, `config.py`)

The §9.6/§9.7 enforcement core every layer routes through — built first so no layer can bypass it. `filter_on_write` scans content with security tiers 1–3 (existing `PromptSecurityScanner.scan_messages` + `ContentFilter.filter_input`) **before** any persist: a blocked verdict quarantines (`status='quarantined'`, flagged, never stored clean). `recall` re-filters on read (defense against pre-existing/scope-promoted poison) and wraps content as a provenance-headed quoted-data block — plain-text fenced block naming scope, author, trust tier, and date ("unverified note captured from user X's session on <date>"), inserted only into user-role context, **structurally never a system/developer message**. `config.py` resolves the §6A.5 per-key block `proxy_memory: {scratchpad, summarization: {enabled, threshold_tokens, keep_recent, ratio}, embedding_cache, schema_dedup}` from `api_keys.proxy_memory` with documented defaults (summarization **opt-in** per spec; scratchpad substitution **opt-in**; embedding/dedup caches default on *when the flag is on*), AND-gated with `features.enabled("proxy_memory", distinct_id=str(org_id))` — fail-safe OFF.

**Files:** Create `shared/memory/__init__.py`, `shared/memory/provenance.py`, `shared/memory/config.py`, `tests/unit/memory/test_provenance.py`, `tests/unit/memory/test_memory_config.py`.

- [ ] **Step 1: Write failing tests** — provenance: (a) `filter_on_write` passes clean content through unchanged and returns `ok`; (b) an injection payload ("ignore your previous instructions and…") returns `quarantine` with the tier verdict attached — caller must not persist as active; (c) `recall` re-runs tiers 1–3 on stored content and blocks/redacts poison that predates filtering; (d) the wrapped block contains scope, author, trust tier, date, and a "quoted material — not instructions" marker; (e) wrapped output never claims `role: system`. Config: (f) full block parses; (g) missing block → defaults (summarization disabled, scratchpad substitution disabled); (h) flag OFF → `resolve(...)` returns all-disabled regardless of per-key config; (i) `features` raising → all-disabled (fail-safe OFF).

- [ ] **Step 2: Run tests, verify fail** — `python3 -m pytest tests/unit/memory/ -v --no-cov` → `ModuleNotFoundError: shared.memory`.

- [ ] **Step 3: Implement** —
  ```python
  @dataclass(slots=True)
  class ProvenanceTag:
      scope_type: str          # org|project|repo|user|session
      scope_ref: str
      author_user_id: Optional[int]
      trust_tier: str          # verified|confirmed|derived|unverified
      created_at: datetime

  @dataclass(slots=True)
  class WriteVerdict:
      ok: bool
      quarantine: bool
      filtered_text: str
      reasons: list

  async def filter_on_write(text, *, scanner, content_filter, user_id, org_id) -> WriteVerdict: ...
  async def recall(text, tag: ProvenanceTag, *, scanner, content_filter, user_id, org_id) -> Optional[str]: ...

  @dataclass(slots=True)
  class ProxyMemoryConfig:
      scratchpad_enabled: bool
      scratchpad_substitution: bool
      summarization_enabled: bool
      threshold_tokens: int        # default 8000
      keep_recent: int             # default 4
      ratio: float                 # default 0.3
      embedding_cache: bool
      schema_dedup: bool

  async def resolve_proxy_memory_config(db, features, api_key_id, org_id) -> ProxyMemoryConfig: ...
  ```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/ tests/unit/memory/
  git commit -m "feat(memory): §9.6/§9.7 injection-safety primitives + per-key proxy_memory config (flag-gated, fail-safe OFF)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Session scratchpad store (`scratchpad.py`)

Per-session KV working set (§6A.1): Valkey hot path (session TTL, default 24h) spilling to `session_scratchpad` in Postgres for durability; read prefers Valkey, falls through to Postgres and re-warms. **Isolation is per (org, session, user) — a composite key on every operation; these tests are security tests.** Writes route through `filter_on_write` (quarantined values are stored with `status='quarantined'` and never returned by `get`/`list`). Abuse limits from config: `max_value_kb` (default 256) and `max_keys` per session (default 128) → explicit errors, never silent truncation.

**Files:** Create `shared/memory/scratchpad.py`, `tests/unit/memory/test_scratchpad.py` (fakeredis + stubbed DAL).

- [ ] **Step 1: Write failing tests** — (a) `put/get` round-trip; (b) `list` returns keys + sizes + timestamps, not values; (c) `delete` removes both tiers; (d) **SECURITY: user B in the same org+session cannot `get` user A's key; same user in a different session cannot; a different org with identical session/user ids cannot** (all three axes asserted independently); (e) Valkey flush → `get` still serves from Postgres and re-warms Valkey; (f) TTL/`expires_at` honored; (g) injection payload in `put` → quarantined: `put` returns a quarantine notice, `get` returns None, row `status='quarantined'`; (h) `max_value_kb`/`max_keys` exceeded → typed errors; (i) rows carry `scope_type='session'`, `trust_tier='unverified'`, `author_user_id` set.

- [ ] **Step 2: Run tests, verify fail** — `ImportError: cannot import name 'ScratchpadStore'`.

- [ ] **Step 3: Implement `ScratchpadStore`** —
  ```python
  class ScratchpadStore:
      def __init__(self, valkey, db, scanner, content_filter, ttl_seconds: int = 86400): ...
      async def put(self, org_id, session_id, user_id, key, value, *, limits) -> PutResult: ...
      async def get(self, org_id, session_id, user_id, key) -> Optional[str]: ...
      async def list(self, org_id, session_id, user_id) -> list[ScratchpadKeyInfo]: ...
      async def delete(self, org_id, session_id, user_id, key) -> bool: ...
  ```
  Valkey key `waddleai:sp:{org_id}:{session_id}:{user_id}:{key}`; Postgres upsert on the composite unique index; all persisted values pass `filter_on_write` first.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/memory/test_scratchpad.py -v --no-cov`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/scratchpad.py tests/unit/memory/test_scratchpad.py
  git commit -m "feat(memory): session scratchpad store — Valkey→Postgres KV with (org,session,user) isolation security tests" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Scratchpad MCP tools (`scratchpad_tools.py`)

Exposes `scratchpad_put(key, value)`, `scratchpad_get(key)`, `scratchpad_list()` (§6A.1). Handlers are **transport-agnostic** (dict-in/dict-out, identity from the caller's authenticated `UserContext` + session — never from tool arguments, so a tool call can't reach across sessions/users) and registered into the existing `shared/utils/mcp_interface.py` tool registry behind a guard; the §11 MCP v2 branch re-exposes the same handlers over its new transports.

**Files:** Create `shared/memory/scratchpad_tools.py`, `tests/unit/memory/test_scratchpad_tools.py`. Modify `shared/utils/mcp_interface.py`.

- [ ] **Step 1: Write failing tests** — (a) each tool has a name/description/inputSchema triple suitable for MCP `tools/list`; (b) `scratchpad_put` → store write with the caller's (org, session, user) — **passing a different `session_id`/`user_id` in tool arguments is ignored/rejected** (security); (c) `scratchpad_get` unknown key → structured not-found, not an exception; (d) `scratchpad_list` returns key metadata; (e) tools registered in `MCPServer.tools` and dispatchable via `tools/call`; (f) flag OFF (config disabled) → tools return a structured `feature_disabled` error.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement** — `SCRATCHPAD_TOOLS` registry of `(schema, handler)`; handlers take `(store, config, user_context, session_id, arguments)`; wire registration in `MCPServer.__init__` (guarded so MCP interface changes don't break when the store isn't injected).

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/unit/test_mcp_interface.py -v --no-cov 2>&1 | tail -5` (no regressions).

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/scratchpad_tools.py tests/unit/memory/test_scratchpad_tools.py shared/utils/mcp_interface.py
  git commit -m "feat(memory): scratchpad_put/get/list MCP tools — transport-agnostic, caller-identity scoped" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: `ScratchpadStage` — plain-client substitution via `X-WaddleAI-Session`

For non-MCP clients (§6A.1): opt-in convention where a message contains a reference marker `waddleai://scratchpad/<key>` and the proxy substitutes the stored blob. Only active when the request carries `X-WaddleAI-Session` AND per-key config `scratchpad_substitution` is on AND the flag is on. Substituted content goes through `recall` (re-filter + provenance-wrapped quoted block, §9.7 rules 2–4). Unknown key → marker left untouched (fail-open to literal text, logged) — never a hard error mid-conversation.

**Files:** Create `proxy/apps/proxy_server/pipeline/memory_stages.py` (stage 1 of 3), `tests/unit/proxy/test_memory_stages.py`.

- [ ] **Step 1: Write failing tests** — (a) marker in a user message + header + config on → message content contains the stored value wrapped in a provenance-headed quoted block; (b) no `X-WaddleAI-Session` header → untouched; (c) config off or flag off → untouched, stage logs `skipped`; (d) unknown key → marker literal preserved; (e) **SECURITY: marker referencing a key stored by another user/session/org substitutes nothing**; (f) substituted content that fails re-filter (poison stored before this feature) is not injected; (g) multiple markers in one request all resolve; (h) `ctx.usage_meta["scratchpad_substitutions"]` counts substitutions.

- [ ] **Step 2: Run tests, verify fail** — `ModuleNotFoundError: ...memory_stages`.

- [ ] **Step 3: Implement `ScratchpadStage(Stage)`** — `name = "scratchpad"`, `flag = "proxy_memory"`; scans `ctx.messages` for the marker regex; resolves via `ScratchpadStore.get` with identity from `ctx` (auth stage output + session header), wraps via `recall`; annotates `ctx`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/proxy/test_memory_stages.py -v --no-cov`.

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/pipeline/memory_stages.py tests/unit/proxy/test_memory_stages.py
  git commit -m "feat(proxy): ScratchpadStage — opt-in X-WaddleAI-Session reference-marker substitution, injection-safe recall" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: Tokenizer-length cache (`token_len_cache.py`)

Token counts of stable blocks keyed `(model_or_tokenizer, content_hash)` in Valkey (§6A.4) — huge unchanged contexts aren't re-tokenized every turn. Valkey-only (derivable, §6A.5). Consumed by the summarizer (Task 7) for threshold counting and by dedup (Tasks 11–12) for savings accounting. Counting delegates to the resolved connector's `count_tokens` (existing per-connector method, tiktoken fallback) via a supplied callable.

**Files:** Create `shared/memory/token_len_cache.py`, `tests/unit/memory/test_token_len_cache.py`.

- [ ] **Step 1: Write failing tests** — (a) first `count(model, text)` invokes the counter callable and caches; (b) second identical call does NOT invoke it (call-count assertion) and returns the same number; (c) **correctness vs fresh count**: for a matrix of texts, cached result == direct counter result (§6A.6 item); (d) different model → separate entry; (e) TTL respected; (f) counter exception → propagated, nothing cached.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `TokenLenCache`** — key `waddleai:toklen:{model}:{sha256(text)}`, TTL default 7d; `async def count(self, model: str, text: str, counter: Callable[[str], Awaitable[int]]) -> int`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/token_len_cache.py tests/unit/memory/test_token_len_cache.py
  git commit -m "feat(memory): tokenizer-length cache keyed (model, content_hash) in Valkey" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: Conversation summarizer (`summarizer.py`)

Threshold-triggered distillation (§6A.2). Operates on the request's message history: when total tokens (via `TokenLenCache`) cross `threshold_tokens`, turns older than the last `keep_recent` are summarized. Summaries persist in `conversation_summaries` keyed `(org_id, conversation_id)` with `covers_through_turn` + `version` — a repeat turn whose existing summary still covers `len(history) - keep_recent` reuses it (no model call); otherwise a new version is generated **synchronously** (cheap local model — acceptable latency, deterministic tests) and supersedes the old (`superseded_by`, §9.7 correction model). **§7.1 seam (flagged):** `model_assignments` doesn't exist until migration 009 (smart-routing branch), so model selection goes through a single `resolve_summarize_model()` helper — env `SUMMARIZE_MODEL` / config, default a registry local model (e.g. `gemma3:1b`) dispatched via the existing `llm_manager` — with a `# TODO(§7.1): replace with model_assignments lookup when feature/smart-routing lands` marker at exactly one call site. Summary text passes `filter_on_write` before persist. Originals are untouched — they remain in the request store / conversation memory (retrievability is the store's job; only what's *injected* is compacted).

**Files:** Create `shared/memory/summarizer.py`, `tests/unit/memory/test_summarizer.py` (stubbed llm_manager + fakeredis + stubbed DAL).

- [ ] **Step 1: Write failing tests** — (a) below threshold → `should_summarize` False, zero model calls; (b) crossing threshold → summarize called once with only the older turns (last `keep_recent` never in the summarization prompt); (c) result persisted with `covers_through_turn == len(history) - keep_recent`, `model_used`, `tokens_summarized`, `scope_type='session'`, `trust_tier='unverified'`; (d) repeat turn with covering summary → **no** model call (call-count assertion), stored summary returned; (e) history grew past coverage → new version generated, old row `superseded_by` set; (f) `ratio` guardrail: a "summary" longer than `ratio * original_tokens` is rejected → fall back to un-summarized injection (never inject a bloated summary); (g) summarize-model failure → graceful: no summary, original history used, error logged (degradation never breaks the request); (h) injection payload in generated summary → `filter_on_write` quarantines, fallback to originals.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `ConversationSummarizer`** —
  ```python
  @dataclass(slots=True)
  class SummarizationResult:
      applied: bool
      summary: Optional[str]
      covers_through_turn: int
      tokens_elided: int

  class ConversationSummarizer:
      def __init__(self, db, llm_manager, token_len_cache, scanner, content_filter): ...
      async def maybe_summarize(self, org_id, user_id, conversation_id,
                                messages, cfg: ProxyMemoryConfig, model: str) -> SummarizationResult: ...
  ```

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/summarizer.py tests/unit/memory/test_summarizer.py
  git commit -m "feat(memory): rolling conversation summarizer — threshold, keep-recent-N, versioned persistence, §7.1 model seam" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: `SummarizationStage` — inject summary + recent-N

Pipeline stage applying Task 7's result to what gets dispatched (§6A.2): replaces older turns with a single provenance-wrapped summary block (via `recall` — the summary is recalled content, re-filtered, quoted-data, never system-role) followed by the last `keep_recent` turns verbatim. Records `ctx.usage_meta["summarized"] = True` and `tokens_elided` for the `usage.waddleai` object (Task 13). Opt-in per key; flag-off no-op. Conversation identity = `X-WaddleAI-Session` / `session_id` (falls back to disabled when absent — no conversation id means no safe summary key).

**Files:** Modify `proxy/apps/proxy_server/pipeline/memory_stages.py`, `tests/unit/proxy/test_memory_stages.py`.

- [ ] **Step 1: Write failing tests** — (a) over-threshold request → dispatched `ctx.messages` == `[system msgs…, provenance-wrapped summary block, last keep_recent turns]`; measured injected token count strictly less than the original (token reduction assertion, §6A.6); (b) under threshold → untouched; (c) opt-out key / flag off / missing session id → untouched, stage `skipped`; (d) `ctx.usage_meta` carries `summarized: True` + `tokens_elided` > 0 only when applied; (e) summary block is user-role quoted data with provenance header, never `role: system` authority; (f) summarizer degradation (Task 7 g/h) → original messages dispatched, `summarized` absent; (g) original request body (`ctx.body["messages"]`) remains unmodified — only the dispatch view is compacted (originals retrievable downstream).

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `SummarizationStage(Stage)`** — `name = "summarize"`, `flag = "proxy_memory"`; calls `ConversationSummarizer.maybe_summarize`, rebuilds the dispatch message list, annotates `ctx`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/proxy/test_memory_stages.py -v --no-cov`.

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/pipeline/memory_stages.py tests/unit/proxy/test_memory_stages.py
  git commit -m "feat(proxy): SummarizationStage — inject summary + recent-N, usage.waddleai.summarized + tokens_elided" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Embedding cache (`embedding_cache.py`) + wiring into memory paths

`CachedEmbedder` wrapping `EmbeddingManager` (§6A.3): key `(model, sha256(content))`; lookup Valkey → Postgres `embedding_cache` → miss computes via `asyncio.to_thread(manager.embed, text)` (the backend is blocking — never on the event loop) then writes both tiers. Wire into `shared/utils/memory_integration.py` so every pgvector store/search embedding call routes through it; config-gated (`embedding_cache`) with a transparent passthrough when off.

**Files:** Create `shared/memory/embedding_cache.py`, `tests/unit/memory/test_embedding_cache.py`. Modify `shared/utils/memory_integration.py`.

- [ ] **Step 1: Write failing tests** — (a) first `embed(model, text)` calls the underlying manager once, returns the vector; (b) second identical call → **zero** underlying calls (call-count assertion, §6A.6) and an equal vector; (c) Valkey flushed → served from Postgres, Valkey re-warmed, still zero manager calls; (d) different model, same text → separate entry, manager called; (e) config off → passthrough, no cache reads/writes; (f) cache rows contain vectors only — assert no plaintext column is written; (g) memory_integration store/search paths hit `CachedEmbedder` (patched call assertion), and existing `tests/unit/test_memory_integration.py` stays green.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement** — `CachedEmbedder(valkey, db, manager, enabled)`, Valkey key `waddleai:emb:{model}:{hash}` (TTL 7d; Postgres is the durable tier); orjson-packed float lists; wire construction in `create_memory_manager(...)` behind config.

- [ ] **Step 4: Run tests, verify pass**; `python3 -m pytest tests/unit/test_memory_integration.py -v --no-cov 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/embedding_cache.py tests/unit/memory/test_embedding_cache.py shared/utils/memory_integration.py
  git commit -m "feat(memory): embedding cache keyed (model, content_hash) — Valkey→Postgres, re-embed avoidance" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: Retrieval-result cache (`retrieval_cache.py`) + corpus-version invalidation

Valkey-only (§6A.3/§6A.5 — derivable, never durable): search results keyed `(org_id, query_hash, corpus_version, top_k)`, short TTL (default 300s). This cache **does** hold readable content, so it is strictly org-scoped — isolation is a security test. Corpus version = monotonic Valkey counter `waddleai:corpus_ver:{org_id}:{store}` bumped on every write/delete to the underlying store (memory writes here; CodeRAG re-index and docs re-fetch bump the same counter on the §9 branch); a bump makes all prior keys unreachable (natural invalidation — old entries age out via TTL). Wire into `memory_integration` search paths.

**Files:** Create `shared/memory/retrieval_cache.py`, `tests/unit/memory/test_retrieval_cache.py`. Modify `shared/utils/memory_integration.py`.

- [ ] **Step 1: Write failing tests** — (a) identical query twice → second served from cache, underlying vector search called once (call-count assertion); (b) TTL expiry → recomputed; (c) **corpus-version bump (a memory write) → previously cached query misses and recomputes** (§6A.6 invalidation item); (d) **SECURITY: org A's cached results never served to org B for an identical query**; (e) different `top_k` → distinct entries; (f) config/flag off → passthrough, search called every time; (g) `clear`/bump helper is exposed for the §9 branch to call on re-index.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `RetrievalResultCache`** — `get_or_compute(org_id, store, query, top_k, compute)`; key `waddleai:rr:{org_id}:{store}:{corpus_ver}:{sha256(query)}:{top_k}`; `bump_corpus_version(org_id, store)`; wire into `Mem0MemoryStore`/pgvector search + a bump in `store_memory`/`delete_memory`/`clear_memories`.

- [ ] **Step 4: Run tests, verify pass**; memory-integration suite tail.

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/retrieval_cache.py tests/unit/memory/test_retrieval_cache.py shared/utils/memory_integration.py
  git commit -m "feat(memory): Valkey retrieval-result cache with corpus-version invalidation, org-isolated" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Tool-schema / system-prompt dedup store (`dedup_store.py`)

Canonical copies of large stable blocks (tool schemas, system prompts, pasted files) keyed by content-hash per (org, session) in Valkey (§6A.4). Two functions: (1) `observe(...)` records stable blocks and their prefix hashes in the exact Valkey shape §6.3 consumes (`waddleai:prefix:{vkey_id}:{prefix_hash}` observation counters — these >1024-token blocks are precisely what gets `cache_control` breakpoints / Ollama KV affinity on the response-cache branch); (2) `elide_intra_request(...)` — pure function: the same block appearing ≥2× within one request is reduced to a single canonical copy plus short reference stubs (`[deduplicated: see block #N above]`), returning the rewritten messages + tokens saved (via `TokenLenCache`). Elision is content-mechanical (no model), lossless in reference (stub names the canonical block), and applies only to blocks over a size floor (default 512 tokens) so prose is never mangled.

**Files:** Create `shared/memory/dedup_store.py`, `tests/unit/memory/test_dedup_store.py`.

- [ ] **Step 1: Write failing tests** — (a) a doubly-pasted 2k-token block in one request → one canonical copy + one stub, counted tokens strictly reduced (§6A.6 assertion, via TokenLenCache); (b) triple occurrence → one copy + two stubs; (c) block below the size floor → untouched; (d) two *different* blocks → both kept; (e) `observe` writes prefix-hash observation keys in the §6.3 format (assert exact key shape + counter increment on repeat observation); (f) **SECURITY: canonical store is keyed per (org, session) — session B never resolves session A's block hash**; (g) idempotent: eliding already-elided messages is a no-op; (h) tokens_saved reported accurately (fresh-count comparison).

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `DedupStore`** — `observe(org_id, session_id, vkey_id, blocks)`, `elide_intra_request(messages, tools, system, *, model, token_len_cache, floor_tokens) -> (messages, tools, system, tokens_saved)`; block extraction = tool-schema entries, system prompt(s), and fenced/contiguous content runs ≥ floor; canonical registry `waddleai:dedup:{org_id}:{session_id}:{hash}`.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit**
  ```bash
  git add shared/memory/dedup_store.py tests/unit/memory/test_dedup_store.py
  git commit -m "feat(memory): tool-schema/system-prompt dedup store — intra-request elision + §6.3 prefix-hash feed" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: `DedupStage` — elision before dispatch and before token counting

Third pipeline stage (§6A.4): runs `elide_intra_request` on the dispatch view of the request **before** `DispatchStage` and before the dispatched token count is taken (the Task-1/phase-1 `TokenBudgetStage` reserve happens earlier on the raw estimate; `MeterStage` reconcile settles actuals — documented in the stage docstring), calls `observe` for §6.3, and accumulates `ctx.usage_meta["tokens_saved"]`.

**Files:** Modify `proxy/apps/proxy_server/pipeline/memory_stages.py`, `tests/unit/proxy/test_memory_stages.py`.

- [ ] **Step 1: Write failing tests** — (a) duplicated-block request → `DispatchStage` receives elided messages; `ctx.usage_meta["tokens_saved"]` > 0; (b) no duplication → passthrough, `tokens_saved` == 0; (c) flag/config off → untouched, `skipped`; (d) `observe` called with the request's stable blocks (prefix feed asserted); (e) elision output re-verified idempotent when the stage runs after `SummarizationStage` (stage-order integration: summarize → dedup); (f) original `ctx.body` untouched — only the dispatch view.

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement `DedupStage(Stage)`** — `name = "dedup"`, `flag = "proxy_memory"`.

- [ ] **Step 4: Run tests, verify pass** — `python3 -m pytest tests/unit/proxy/test_memory_stages.py -v --no-cov`.

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/pipeline/memory_stages.py tests/unit/proxy/test_memory_stages.py
  git commit -m "feat(proxy): DedupStage — intra-request elision pre-dispatch/pre-count, tokens_saved accounting" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 13: Pipeline wiring — both endpoints, `usage.waddleai` accounting, whole-feature flag-off proof

Insert the three stages into the pipeline build in `main.py` — order: `AuthStage → TokenBudgetStage → SecurityInStage → ScratchpadStage → SummarizationStage → DedupStage → DispatchStage → SecurityOutStage → MeterStage` (memory assembly on post-filter content; when `feature/response-cache` lands, its `CacheStage` slots between `DedupStage` and `DispatchStage` so cache keys hash the assembled context — coordination noted in a comment at the insertion point). Plumb `X-WaddleAI-Session` into `PipelineContext`; surface `ctx.usage_meta` as the additive `usage.waddleai` object (`{summarized, tokens_elided, tokens_saved, scratchpad_substitutions}`) in **both** endpoint response translations; construct `ScratchpadStore`/`ConversationSummarizer`/`DedupStore`/caches in `ProxyServer.startup()` and inject the scratchpad store into the MCP tool registration (Task 4).

**Files:** Modify `proxy/apps/proxy_server/main.py`. Create `tests/unit/proxy/test_memory_pipeline_wiring.py`.

- [ ] **Step 1: Write failing tests** — (a) with flag+config on, both endpoints show `scratchpad`, `summarize`, `dedup` in `ctx.stage_log` in the specified order, and the stage-log **parity** assertion between `/v1/chat/completions` and `/v1/messages` still holds; (b) a summarized+deduped request's response carries `usage.waddleai` with `summarized: true`, `tokens_elided`, `tokens_saved` on both endpoint shapes; (c) untouched request → `usage.waddleai` fields absent or zero — never removes/renames existing usage fields (additive-only, §14.2); (d) **flag-off proof: with `waddleai.proxy_memory` OFF, all three stages log `skipped`, responses are byte-identical to a pipeline built without the stages, no Valkey `waddleai:sp:/waddleai:dedup:/waddleai:rr:` keys are written, and no `session_scratchpad`/`conversation_summaries` rows appear** (§6A.6 / §14.2 standing gate); (e) `features` client raising → same as OFF (fail-safe).

- [ ] **Step 2: Run tests, verify fail.**

- [ ] **Step 3: Implement** — stage construction + insertion; `usage.waddleai` merge helper in the response translators; startup wiring; session-id extraction (`X-WaddleAI-Session` > body `session_id`).

- [ ] **Step 4: Run tests, verify pass**; golden contract snapshots — `make test-contract 2>&1 | tail -20` (green; `usage.waddleai` additions are deliberate additive snapshot updates); full unit tail `python3 -m pytest tests/unit/ --no-cov -q 2>&1 | tail -5`.

- [ ] **Step 5: Commit**
  ```bash
  git add proxy/apps/proxy_server/main.py tests/unit/proxy/test_memory_pipeline_wiring.py
  git commit -m "feat(proxy): wire memory stages into ProxyPipeline for both endpoints; additive usage.waddleai; flag-off proof" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 14: §6A.6 acceptance suite + coverage gate

Every §6A.6 acceptance item as an explicit integration test (stubbed upstream connector, fakeredis, seeded DB), plus the standing gates.

**Files:** Create `tests/integration/test_proxy_memory_acceptance.py`.

- [ ] **Step 1: Scratchpad round-trip + isolation** — put/get/list through the MCP tool handlers end-to-end; the three-axis (org, session, user) isolation matrix as **security tests**.
- [ ] **Step 2: Summarization end-to-end** — long conversation crosses threshold → response `usage.waddleai.summarized: true`; injected-token reduction measured (dispatched tokens < original tokens); original turns retrievable from the conversation store afterward.
- [ ] **Step 3: Embedding-cache hit avoids re-embed** — two identical memory-store operations → exactly one underlying `embed` call (call-count assertion).
- [ ] **Step 4: Retrieval cache + corpus-version invalidation** — repeated search hits cache; a memory write bumps the corpus version and the next search recomputes.
- [ ] **Step 5: Schema-dedup token reduction** — doubly-pasted block through the full pipeline → dedup applied, counted/dispatched tokens reduced, `tokens_saved` reported.
- [ ] **Step 6: Tokenizer-length cache correctness** — cached counts equal fresh counts across the fixture matrix.
- [ ] **Step 7: Injection-safety on all recalled content** — poison planted directly in scratchpad/summary rows (bypassing write filters, simulating pre-existing poison) is caught by read-time re-filter on every recall path; provenance headers present on every injected block; write-time planting attempt quarantined.
- [ ] **Step 8: Flag-off = no memory layers active** — whole acceptance fixture re-run with the flag OFF: behavior unchanged, zero memory-layer writes (reuses Task 13's proof at integration level).
- [ ] **Step 9: Coverage gate** — `python3 -m pytest tests/ --cov --cov-fail-under=90 2>&1 | tail -15` (≥90% on changed modules, §14.2).
- [ ] **Step 10: Migration coordination check** — `alembic -c services/management/alembic.ini heads` → single head; if `008a_response_cache` has merged since Task 1, re-point `008b`'s `down_revision` now and re-run `tests/unit/management/test_migration_008b.py`.
- [ ] **Step 11: Commit**
  ```bash
  git add tests/integration/test_proxy_memory_acceptance.py
  git commit -m "test(memory): §6A.6 acceptance suite — isolation, summarization, cache hits, dedup, injection-safety, flag-off" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §6A (+§9.7)

| Spec requirement | Task |
|---|---|
| §6A.1 scratchpad Valkey→Postgres, per-(org,session,user) isolation | 3 |
| §6A.1 MCP tools `scratchpad_put/get/list` | 4 |
| §6A.1 plain-client `X-WaddleAI-Session` substitution (opt-in) | 5 |
| §6A.2 threshold trigger, keep-recent-N, tunable ratio, opt-in per key | 7, 8 |
| §6A.2 §7.1 `summarize` model assignment (cheap local default) — seam pending migration 009 | 7 |
| §6A.2 originals retrievable; only injection compacted | 7, 8, 14 |
| §6A.2 `usage.waddleai.summarized` + tokens elided | 8, 13 |
| §6A.3 embedding cache (model, content_hash) Valkey→Postgres | 9 |
| §6A.3 retrieval-result cache (query-hash, corpus-version, top-k, TTL) + invalidation | 10 |
| §6A.4 canonical dedup store by hash per (org, session) | 11 |
| §6A.4 feeds §6.3 prefix-cache (breakpoints / KV affinity) | 11, 12 |
| §6A.4 intra-request dedup elision pre-dispatch + pre-count | 11, 12 |
| §6A.4 tokenizer-length cache | 6 |
| §6A.4 savings in `usage.waddleai.tokens_saved` | 12, 13 |
| §6A.5 tables + §9.7 scope/trust columns (migration 008b, coordination flagged) | 1, 14 |
| §6A.5 per-key `proxy_memory` config block | 1, 2 |
| §6A.5 retrieval/tokenizer caches Valkey-only | 6, 10 |
| §6A.5 single additive `usage.waddleai` object | 13 |
| §6A.6 acceptance items (each an explicit test) | 3–14 |
| §9.6/§9.7 write-time filtering, read re-filter, quoted-data provenance injection, never system-role | 2, 3, 5, 7, 8, 14 |
| §9.7 session scope default, trust `unverified`, versioned/attributable, quarantine status | 1, 2, 3, 7 |
| §9.7 org boundary absolute on readable stores (security tests) | 3, 5, 10, 11, 14 |
| §14.5 flag `waddleai.proxy_memory`, default OFF, fail-safe OFF, flag-off proof | 2, 13, 14 |
| §3.5 blocking embed off event loop; stateless pods (Valkey state) | 9; 3, 6, 10, 11 |
