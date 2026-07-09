# Auto Memory / Knowledge Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends in a real `git commit`.

**Branch:** `feature/knowledge-layer` (off `release/v0.2.X`). **Depends on:** `feature/aiproxy-migration` (§5 — `ProxyPipeline` stage classes, `shared/licensing/features.py`, `ContentFilter`/`PromptSecurityScanner` wired at stage 3, `/api/v1/memory-config` re-home) **and** `feature/proxy-memory-layers` (§6A — `embedding_cache` table from migration 008, keyed `(model, content_hash)`) **and** `feature/smart-routing` (§7 — `model_assignments.embeddings` row / `model_registry`; migration 009). Migration 011 down-revisions `010_security_v2`.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §9 (with §9.1 CodeRAG, §9.2 docs cache, §9.3 PDF/MD ingestion, §9.4 conversation memory, §9.5 hybrid delivery, §9.6 injection-safety, §9.7 scoping/trust/isolation, §9.8 acceptance), plus §2.5 third-party content, §3.6 security posture, §13.1 migration 011, §14.2 standing gates, §14.5 flags `waddleai.coderag`/`waddleai.docs_cache`/`waddleai.knowledge_ingest`. Authoritative.

---

**Goal:** Four knowledge subsystems on one Postgres+pgvector substrate (embeddings via the §7.1 `embeddings` assignment, default `nomic-embed-text`, 768-dim), all **scoped, trust-tiered, attributable, correctable, and injection-safe**: (1) **CodeRAG** — tree-sitter (MIT) function/class chunking with `path > class > signature` headers, server-side git-pull worker with content-hash incremental re-index, hybrid pgvector+FTS reciprocal-rank search keyed on `(repo, branch/commit)`; (2) **docs research cache** — on-demand fetch → `markdownify` (MIT) → chunk → embed, TTL 30d/7d, robots.txt + rate-limit + per-source license (CC-BY-SA attribution); (3) **manual knowledge ingestion** — `/api/v1/knowledge` upload+CRUD + CLI, PDF via `pypdf` (BSD) / `docling` (MIT) optional (**PyMuPDF/AGPL banned**), Markdown direct → org-scoped `rag_documents`; (4) **conversation memory** config re-home + the §9.7 correction/promotion surface. Delivery is **hybrid by client type** (MCP-capable → pull; plain → budgeted auto-inject, default 2000 tokens). Migration 011. Every subsystem behind its PostHog flag, default OFF. **MCP tools that expose these land in `feature/mcp-v2-integrations`; this branch builds the services + API they call.**

**Architecture:** Every stored item carries `(scope, provenance, trust, version)` (§9.7). Retrieval is a composite scope key — narrower scopes override, broader are shared read-only; ranking is relevance × trust. Auto-captured memory/scratchpad stays at **session scope**; promotion to repo/project/org is **explicit**, never automatic. All retrieved content (memory, CodeRAG, docs, uploaded, external) is **provenance-tagged and re-filtered through content-filter tiers 1–3 before entering any prompt** — retrieved text is data, never instruction. Writes are filtered at store time; injection payloads are quarantined, never persisted clean. CodeRAG chunks key on branch so parallel worktrees never cross-contaminate. Org boundary is the hard isolation wall (§3.6). Embedding compute is deduplicated through the §6A.3 `embedding_cache`; CPU-heavy tree-sitter parsing and PDF extraction run off the event loop (`asyncio.to_thread` / `ProcessPoolExecutor`). Git clone/pull and docs fetch run in an async Management worker; the fetcher respects robots.txt with per-source rate limits. Every subsystem sits behind `features.enabled("coderag"|"docs_cache"|"knowledge_ingest", distinct_id=str(org_id))` — fail-safe OFF.

**Tech Stack:** Python 3.13, Quart + hypercorn, penguin-dal (runtime) / SQLAlchemy + Alembic (schema), pgvector (768-dim + HNSW/ivfflat) + Postgres FTS (`tsvector`), `tree-sitter` + `tree-sitter-language-pack` (MIT), `markdownify` (MIT), `pypdf` (BSD-3) + optional `docling` (IBM/MIT), `httpx` + `urllib.robotparser`, `GitPython`/`dulwich` for server-side pull, supercronic (cron re-index), orjson, pytest + pytest-asyncio, `pytest-httpserver` (local docs fixture — never live sites in CI).

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `services/management/alembic/versions/011_knowledge.py` | Migration 011 (down-rev `010`) — `code_repos`, `code_chunks`, `docs_cache_pages`, `docs_sources` (pgvector + FTS); extend `rag_documents` + `memory_embeddings` with §9.7 scope/trust/version/status cols; seed `docs_sources` |
| Create | `tests/unit/management/test_migration_011.py` | Round-trip + downgrade on seeded snapshot |
| Modify | `services/management/app/models_sqlalchemy.py` | `CodeRepo`, `CodeChunk`, `DocsCachePage`, `DocsSource` classes; `RAGDocument`/`MemoryEmbedding` gain `scope_type`,`scope_ref`,`author_user_id`,`trust_tier`,`version`,`superseded_by`,`status`,`expires_at`,`provenance` |
| Create | `shared/knowledge/__init__.py` | Package init |
| Create | `shared/knowledge/embed.py` | `embed_cached(content, model)` over §6A.3 `embedding_cache`; `resolve_embedding_model()` reads §7 `embeddings` assignment (fallback nomic-embed-text) |
| Create | `tests/unit/knowledge/test_embed.py` | Cache-hit avoids re-embed (call-count); assignment resolution |
| Create | `shared/knowledge/scoping.py` | `ScopeKey`, `TrustTier`, `ScopedRecord`; composite-scope read resolution; relevance×trust ranking; contradiction detect + quarantine/supersede |
| Create | `tests/unit/knowledge/test_scoping.py` | Isolation + trust-weighting + supersede (security) |
| Create | `shared/knowledge/injection_safety.py` | `filter_for_store()` (tiers 1–3 → quarantine), `filter_for_inject()` (re-filter + provenance-tagged quoted block, no role authority) |
| Create | `tests/unit/knowledge/test_injection_safety.py` | Write-time catch + read-time re-filter + provenance header (security) |
| Create | `shared/knowledge/code_chunker.py` | tree-sitter function/class/module chunking + `path > class > signature` headers; line-window fallback |
| Create | `tests/unit/knowledge/test_code_chunker.py` | Boundary + header + fallback tests |
| Create | `services/management/app/services/coderag_worker.py` | git clone/pull, content-hash diff, incremental re-chunk/embed/upsert; webhook/cron/manual triggers |
| Create | `tests/unit/management/test_coderag_worker.py` | Incremental re-index correctness (one file → only its chunks) |
| Create | `shared/knowledge/code_search.py` | Hybrid pgvector cosine + FTS `tsvector` reciprocal-rank fusion; symbol-exact short-circuit; branch-scoped filter |
| Create | `tests/unit/knowledge/test_code_search.py` | Symbol precision + branch isolation (security) |
| Create | `services/management/app/services/docs_cache.py` | On-demand fetch → markdownify → chunk → embed → cache; TTL; robots.txt + rate-limit; per-source license/attribution |
| Create | `tests/unit/management/test_docs_cache.py` | Fetch vs local fixture server; TTL + attribution + robots |
| Create | `services/management/app/api/v1/knowledge.py` | `/api/v1/knowledge` upload + CRUD; PDF(pypdf)/MD → chunk → embed → `rag_documents` (org-scoped, provenance) |
| Create | `tests/unit/management/test_knowledge_api.py` | Upload round-trip + provenance + PyMuPDF-ban guard |
| Create | `waddleai_cli/commands/knowledge.py` (or existing CLI tree) | `waddleai knowledge upload` mirror of the API |
| Create | `tests/unit/cli/test_knowledge_cli.py` | CLI upload → API parity |
| Modify | `services/management/app/api/v1/memory_config.py` | §9.4 seeded defaults (0.7 cutoff, top-3); §9.7 `memory_promote`/`memory_correct`/`memory_dispute` actions |
| Create | `tests/unit/management/test_memory_scoping_api.py` | promote/correct/dispute + contradiction→quarantine→supersede |
| Create | `shared/knowledge/retriever.py` | Unified ranked retrieval across memory/coderag/docs/uploaded; injection-safe; pull-path `search_code`/`search_docs`/`memory_search` service fns |
| Create | `proxy/apps/proxy_server/pipeline/knowledge_stage.py` | `KnowledgeInjectStage` — client-type detect (MCP→pull, plain→budgeted inject default 2000, per-key override); `usage.waddleai.injected_tokens` |
| Create | `tests/unit/proxy/test_knowledge_stage.py` | Client-type matrix + token-budget truncation boundary + flag-off |
| Modify | `services/management/app/api/v1/__init__.py` | Register `knowledge` blueprint |
| Create | `tests/integration/test_knowledge_acceptance.py` | §9.8 acceptance + org-isolation security suite |

---

### Task 1: Migration 011 + ORM models (knowledge tables + §9.7 scope/trust columns)

Land all schema first so every downstream service has tables to write to. Down-revision `010_security_v2` (§13.1). pgvector(768) + FTS on content tables; §9.7 columns fold into `rag_documents`/`memory_embeddings` here (`session_scratchpad`/`conversation_summaries` are migration 008's). Round-trip + downgrade tested (house rule).

**Files:** Create `services/management/alembic/versions/011_knowledge.py`, `tests/unit/management/test_migration_011.py`. Modify `services/management/app/models_sqlalchemy.py`.

- [ ] **Step 1: Write failing round-trip test** — on a SQLite/seeded snapshot: `upgrade` creates `code_repos(id, org_id, name, source_url, credentials_ref, index_status, last_commit)`, `code_chunks(repo_id, path, symbol, kind, start_line, end_line, content, embedding vector(768), content_hash, branch_ref, scope_type, scope_ref, trust_tier, version, superseded_by, status, expires_at)`, `docs_cache_pages(id, ecosystem, package, version, url, content_md, embedding, license, fetched_at, ttl)`, `docs_sources(ecosystem, base_url, license, attribution_required, robots_ttl, rate_limit_rps)`; `rag_documents` and `memory_embeddings` gain `scope_type, scope_ref, author_user_id, trust_tier, version, superseded_by, status, expires_at, provenance`; `docs_sources` seeded with the §9.2 rows (python.org/PSF, docs.rs+doc.rust-lang.org/MIT-Apache, pkg.go.dev/BSD, nodejs.org/MIT, MDN/CC-BY-SA, ruby-doc/Ruby, cppreference/CC-BY-SA); assert MDN + cppreference carry `attribution_required=True`; `downgrade` returns to the 010 shape. Run → fails (no 011).
- [ ] **Step 2: Run test, verify it fails** — `python3 -m pytest tests/unit/management/test_migration_011.py -v --no-cov` → module/revision absent.
- [ ] **Step 3: Implement migration 011** — guarded `op.create_table` / `op.add_column`; pgvector columns via the existing conditional pattern (`ADD COLUMN IF NOT EXISTS embedding vector(768)` + ivfflat/HNSW index) mirrored from the `rag_documents` block already in `models_sqlalchemy.py`; FTS: create a `tsvector` GIN index over `code_chunks(content || symbol)`; `op.bulk_insert` seed for `docs_sources`. Complete `downgrade()`.
- [ ] **Step 4: Update ORM models** — add `CodeRepo`, `CodeChunk`, `DocsCachePage`, `DocsSource`; extend `RAGDocument`/`MemoryEmbedding` with the §9.7 columns (`trust_tier` default `'unverified'`, `status` default `'active'`, `version` default 1).
- [ ] **Step 5: Run tests, verify pass** — `python3 -m pytest tests/unit/management/test_migration_011.py -v --no-cov`; `alembic -c services/management/alembic.ini heads` shows a single head `011_...`.
- [ ] **Step 6: Commit**
  ```bash
  git add services/management/alembic/versions/011_knowledge.py tests/unit/management/test_migration_011.py services/management/app/models_sqlalchemy.py
  git commit -m "feat(db): migration 011 — knowledge tables + §9.7 scope/trust columns" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 2: Embedding access layer (`embed.py`) — cache + assignment resolution

Every embedder in this branch goes through one wrapper so the §6A.3 `embedding_cache` deduplicates compute and the §7 `embeddings` assignment picks the model. Foundational; imported by Tasks 5, 7, 8, 10.

**Files:** Create `shared/knowledge/__init__.py`, `shared/knowledge/embed.py`, `tests/unit/knowledge/test_embed.py`.

- [ ] **Step 1: Write failing tests** — `embed_cached(content, model=None)` (a) computes `content_hash`, checks `embedding_cache` for `(model, content_hash)`, returns cached vector without calling the backend (assert `EmbeddingManager.embed` call-count == 0 on hit); (b) on miss, embeds via `EmbeddingManager` and writes the row; (c) identical content across two calls embeds once; (d) `resolve_embedding_model()` returns the §7 `model_assignments.embeddings` row's model, falling back to `nomic-embed-text` (768) when smart-routing/assignment is absent; (e) returned vector length == 768.
- [ ] **Step 2: Run tests, verify they fail** — `ImportError: cannot import name 'embed_cached'`.
- [ ] **Step 3: Implement** — `embed_cached` uses `hashlib.sha256`; cache lookup via penguin-dal against `embedding_cache`; backend via `create_embedding_manager(...)` (existing `shared/utils/embedding_manager.py`), CPU call wrapped in `asyncio.to_thread`. `resolve_embedding_model()` queries `model_assignments` for `tool_type='embeddings'` with a hardcoded `nomic-embed-text` fallback. No flag gate here (shared primitive; callers gate).
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/__init__.py shared/knowledge/embed.py tests/unit/knowledge/test_embed.py
  git commit -m "feat(knowledge): cached embedding access layer over embedding_cache + embeddings assignment" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 3: Scope / trust / isolation core (`scoping.py`) — §9.7

The model that makes memory safe for real teams: composite-scope reads (narrower overrides, broader shared read-only), trust-weighted ranking, contradiction→quarantine→supersede. Isolation is a **security** property. Reused by Tasks 6, 9, 10.

**Files:** Create `shared/knowledge/scoping.py`, `tests/unit/knowledge/test_scoping.py`.

- [ ] **Step 1: Write failing tests (security)** — (a) `ScopeKey(org, project, repo, branch, user, session)` resolution: a session read returns its own session items **plus** higher-scope items (org/repo) but **never another user's session/user items** — two users in the same repo are isolated (session-memory isolation); (b) `TrustTier` order `verified > confirmed > derived > unverified`; `rank(items)` sorts by relevance × trust; (c) an `unverified` item is always returned flagged for provenance-header injection; (d) `detect_contradiction(new, existing)` finds a semantic conflict and `resolve()` supersedes by trust→confirmation→recency, setting the loser `status='quarantined'` + `superseded_by` (held, not deleted); (e) low-trust session memory can never be auto-elevated into another user's context without explicit promotion; (f) `expires_at` decay: low-trust unconfirmed items past TTL are excluded from retrieval.
- [ ] **Step 2: Run tests, verify they fail** — `ModuleNotFoundError`.
- [ ] **Step 3: Implement** — `@dataclass(slots=True)` `ScopeKey`, `ScopedRecord`, `TrustTier(Enum)`; `visible_scopes(caller_key)` → set of readable `(scope_type, scope_ref)` pairs; `filter_visible(records, caller_key)`; `rank(records, relevances)`; `detect_contradiction` via cosine on embeddings + threshold; `resolve_conflict` returns supersede/quarantine actions (pure — DB writes done by callers). No auto-promotion path exists in code (promotion is an explicit action, Task 9).
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/scoping.py tests/unit/knowledge/test_scoping.py
  git commit -m "feat(knowledge): scope/trust/isolation core with contradiction→quarantine→supersede (§9.7)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 4: Injection-safety gateway (`injection_safety.py`) — §9.6 + §9.7

Two choke points every knowledge write and read passes through. Reuses the existing `ContentFilter` (tiers 1–3) and `PromptSecurityScanner`. This is a **security** boundary — retrieved text is data, never instruction.

**Files:** Create `shared/knowledge/injection_safety.py`, `tests/unit/knowledge/test_injection_safety.py`.

- [ ] **Step 1: Write failing tests (security)** — (a) `filter_for_store(content, org_id)` runs tiers 1–3; an injection payload ("ignore your instructions and…") is caught → returns `quarantined=True`, content never marked clean; (b) benign content passes with `quarantined=False`; (c) `filter_for_inject(records)` re-filters each record through tiers 1–3 (defense against pre-existing / scope-promoted poison) and drops/quarantines a poisoned record even if it was stored earlier; (d) the emitted block is a single **quoted reference** string with a provenance header naming scope, author, trust tier, date — never a `system`/`developer` role message (assert structure: it is `role="user"`-adjacent quoted material, structurally unable to carry role authority); (e) an `unverified` record's header reads "unverified note captured from user X's session on <date>".
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `filter_for_store` calls `ContentFilter.filter_input` + `PromptSecurityScanner.scan_messages`; on BLOCK/high-confidence threat → quarantine. `filter_for_inject` re-runs the filter, then wraps survivors in a provenance-headed fenced block. Never returns role-authoritative content.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/injection_safety.py tests/unit/knowledge/test_injection_safety.py
  git commit -m "feat(knowledge): injection-safety gateway — write-time filter + read-time re-filter + provenance (§9.6/§9.7)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 5: CodeRAG tree-sitter chunker (`code_chunker.py`) — §9.1

Function/class/module-boundary chunking with `path > class > signature` context headers; line-window fallback for unparseable files. Pure/CPU — no I/O, off the event loop.

**Files:** Create `shared/knowledge/code_chunker.py`, `tests/unit/knowledge/test_code_chunker.py`.

- [ ] **Step 1: Write failing tests** — (a) a Python fixture with two classes + free functions yields one chunk per function/method/class-body with `kind` in `{function, method, class, module}`, correct `start_line`/`end_line`, and a header `"<path> > <class> > <signature>"`; (b) a Go/JS fixture chunks at its own function/type boundaries (tree-sitter-language-pack grammar); (c) an unparseable/binary file falls back to fixed line-windows with overlap and `kind="window"`; (d) each chunk carries a stable `content_hash`; (e) deterministic across runs.
- [ ] **Step 2: Run tests, verify they fail** — `ImportError`.
- [ ] **Step 3: Implement** — `chunk_code(path, source, language=None) -> list[CodeChunkDraft]` using `tree-sitter` + `tree-sitter-language-pack` (MIT); language inferred from extension; walk the syntax tree collecting definition nodes; prepend the `path > class > signature` header to `content`; `content_hash = sha256(header+body)`; fallback to `chunk_text`-style line windows. `@dataclass(slots=True) CodeChunkDraft`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/code_chunker.py tests/unit/knowledge/test_code_chunker.py
  git commit -m "feat(knowledge): tree-sitter CodeRAG chunker with path>class>signature headers (§9.1)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 6: CodeRAG index worker (`coderag_worker.py`) — §9.1 incremental re-index

Async Management worker: clone/pull (server-side, credentials via the provider-credential pattern) → diff stored `content_hash`es → re-chunk **changed files only** → embed (Task 2) → upsert `code_chunks` keyed on `(repo, branch/commit)`. Triggers: push webhook, cron (supercronic), manual. Flag `waddleai.coderag`.

**Files:** Create `services/management/app/services/coderag_worker.py`, `tests/unit/management/test_coderag_worker.py`.

- [ ] **Step 1: Write failing tests** — against a local temp git repo fixture (no network): (a) initial index of N files creates N-files' worth of chunks with `index_status='indexed'`, `last_commit` recorded; (b) **incremental correctness** — change one file, re-run → only that file's chunks are re-chunked and re-embedded (assert `embed_cached` called only for the changed file's chunks; unchanged files' `content_hash`es short-circuit); (c) a deleted file's chunks are removed; (d) chunks are keyed on `(repo_id, branch_ref)` so indexing `feature/A` and `feature/B` produces disjoint chunk sets; (e) flag OFF → worker is a no-op (no clone, no writes).
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `CodeRagWorker.index(repo_id, branch=None, trigger=...)`: resolve credentials, clone/pull into a temp dir (blocking git wrapped in `asyncio.to_thread`), enumerate files, compare per-file `content_hash` against stored chunks, re-chunk changed files via Task 5, `embed_cached` each chunk, upsert/delete `code_chunks`, update `code_repos.index_status/last_commit`. `handle_webhook(payload)` (GitHub/Gitea) and a supercronic-invoked `run_scheduled()` entrypoint. Gated on `features.enabled("coderag", distinct_id=str(org_id))`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/services/coderag_worker.py tests/unit/management/test_coderag_worker.py
  git commit -m "feat(knowledge): CodeRAG git-pull worker with content-hash incremental re-index (§9.1)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 7: CodeRAG hybrid search (`code_search.py`) — §9.1 + §9.7 branch-scoped

Hybrid pgvector cosine + Postgres FTS `tsvector` fused by reciprocal-rank; symbol-exact match short-circuits; retrieval filtered to the caller's active `(repo, branch, session)` context. Branch isolation is a **security** property.

**Files:** Create `shared/knowledge/code_search.py`, `tests/unit/knowledge/test_code_search.py`.

- [ ] **Step 1: Write failing tests** — (a) an exact `symbol` match short-circuits and ranks first; (b) hybrid fusion — a query where vector and FTS disagree returns the reciprocal-rank-fused ordering (assert RRF math on a stubbed candidate set); (c) **branch isolation (security)** — a caller on `feature/A` never receives `feature/B`'s in-flight chunks even when semantically closer; (d) symbol-retrieval precision on a small labeled query set over the Task 6 fixture index ≥ threshold; (e) org isolation — org A's query never returns org B's chunks.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `search_code(query, caller_scope, top_k)`: embed query (Task 2), run pgvector cosine top-k and FTS `ts_rank` top-k in parallel, fuse by `1/(k+rank)` reciprocal-rank; symbol-exact pre-check; apply `scoping.filter_visible` for `(org, repo, branch)`; return `SearchResult`s with provenance. Reads honor `status='active'` only.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/code_search.py tests/unit/knowledge/test_code_search.py
  git commit -m "feat(knowledge): hybrid pgvector+FTS reciprocal-rank CodeRAG search, branch-scoped (§9.1/§9.7)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 8: Docs research cache (`docs_cache.py`) — §9.2 + §2.5

On-demand fetch → `markdownify` (MIT) → chunk → embed → cache with TTL 30d (versioned) / 7d ("latest"); robots.txt + per-source rate limits; per-source license with CC-BY-SA attribution. **Tested against a local HTTP fixture server — never live sites in CI.** Flag `waddleai.docs_cache`.

**Files:** Create `services/management/app/services/docs_cache.py`, `tests/unit/management/test_docs_cache.py`.

- [ ] **Step 1: Write failing tests** — using `pytest-httpserver`: (a) first request for `(ecosystem, package, version, url)` fetches, converts HTML→Markdown via `markdownify`, chunks, embeds, writes `docs_cache_pages` with `license` + `fetched_at` + `ttl`; (b) second request within TTL is served from cache (no second HTTP call — assert request count); (c) TTL boundary: versioned → 30d, "latest" → 7d, expired → re-fetch; (d) a `robots.txt` fixture disallowing a path blocks the fetch; (e) per-source rate limit throttles rapid requests; (f) a CC-BY-SA source (MDN/cppreference) returns content with an attribution + license notice in its provenance metadata; (g) flag OFF → fetch disabled, returns empty.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `DocsCache.fetch(ecosystem, package, version, url)`: `urllib.robotparser` check against cached `robots.txt`, per-source token-bucket rate limit (from `docs_sources.rate_limit_rps`), `httpx` GET, `markdownify` conversion, `chunk_text` + `embed_cached`, upsert `docs_cache_pages`; license/attribution from `docs_sources`. Gated on `features.enabled("docs_cache", ...)`.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/services/docs_cache.py tests/unit/management/test_docs_cache.py
  git commit -m "feat(knowledge): on-demand docs research cache — markdownify, TTL, robots, license attribution (§9.2/§2.5)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 9: Manual knowledge ingestion `/api/v1/knowledge` + CLI — §9.3

Upload + CRUD API and `waddleai knowledge upload` CLI mirror. PDF via `pypdf` (BSD-3) / `docling` (MIT) optional; **PyMuPDF/`fitz` is banned (AGPL)**. Markdown direct → chunk → embed → org-scoped `rag_documents` with filename + uploader provenance. Flag `waddleai.knowledge_ingest`.

**Files:** Create `services/management/app/api/v1/knowledge.py`, `waddleai_cli/commands/knowledge.py`, `tests/unit/management/test_knowledge_api.py`, `tests/unit/cli/test_knowledge_cli.py`. Modify `services/management/app/api/v1/__init__.py`.

- [ ] **Step 1: Write failing tests** — (a) `POST /api/v1/knowledge` with a PDF fixture extracts text via `pypdf`, chunks, embeds, writes `rag_documents` (org-scoped) with `provenance={source_filename, uploader_user_id, uploaded_at}`; (b) Markdown upload ingests directly; (c) round-trip — uploaded content is retrievable via search with provenance intact; (d) CRUD: list/get/delete scoped to org (org isolation — security); (e) **banned-import guard**: `grep`-clean assertion that `pymupdf`/`fitz` appear nowhere in the module and requirements; (f) write passes through `injection_safety.filter_for_store` (Task 4) — a poisoned upload is quarantined; (g) `require_scope` enforced; (h) flag OFF → 404/feature-disabled. CLI test: `waddleai knowledge upload <file>` posts to the API and reports the created id.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — Quart blueprint (`async def`, penguin-aaa `require_scope`, penguin-dal); PDF→text `pypdf` with optional `docling` path behind a capability check; Markdown passthrough; `chunk_text` + `embed_cached`; store with scope `org`, `trust_tier='verified'` (admin-curated per §9.7 org scope). CLI subcommand mirrors the request. Register blueprint in `__init__.py`.
- [ ] **Step 4: Run tests, verify pass** — plus `grep -rn "pymupdf\|import fitz" services/ shared/ requirements*.txt` returns nothing.
- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/api/v1/knowledge.py services/management/app/api/v1/__init__.py waddleai_cli/commands/knowledge.py tests/unit/management/test_knowledge_api.py tests/unit/cli/test_knowledge_cli.py
  git commit -m "feat(knowledge): /api/v1/knowledge PDF/MD ingestion + CLI (pypdf, PyMuPDF banned) (§9.3)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 10: Memory scoping API — promote / correct / dispute + memory-config — §9.4 + §9.7

The correction/promotion surface the MCP tools (`memory_promote`/`memory_correct`/`memory_dispute`, built in mcp-v2) call, plus §9.4 seeded conversation-memory defaults. Extends the phase1-created `/api/v1/memory-config`.

**Files:** Modify `services/management/app/api/v1/memory_config.py`. Create `tests/unit/management/test_memory_scoping_api.py`.

- [ ] **Step 1: Write failing tests** — (a) `POST /api/v1/memory-config` seeds/returns §9.4 defaults (0.7 relevance cutoff, top-3 injection); (b) `memory_promote(item_id, target_scope)` moves a session-scope item to repo/project/org **only on explicit call** — no code path auto-promotes; a non-owner/non-admin promote is rejected (security); (c) `memory_correct(item_id, new_content)` versions the item (author/timestamp/scope recorded) and supersedes the prior; (d) `memory_dispute(item_id)` sets `status='quarantined'` pending review; (e) contradiction→quarantine→supersede end-to-end: writing a higher-trust correction quarantines the wrong entry (held, retrievable for audit, absent from retrieval); (f) three kill-switches proven — decay (TTL), supersede, explicit dispute/delete; (g) all mutations attributable (no anonymous writes).
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — extend the blueprint with promote/correct/dispute routes using `scoping.py` (Task 3) for conflict resolution and versioning; writes go through `injection_safety.filter_for_store`; `require_scope` + ownership checks (owner corrects own; repo/project members dispute shared; admin curates any). Seed `ConversationMemoryConfig` defaults.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add services/management/app/api/v1/memory_config.py tests/unit/management/test_memory_scoping_api.py
  git commit -m "feat(knowledge): memory promote/correct/dispute + §9.4 defaults (§9.7 correction model)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 11: Unified retriever + hybrid delivery pipeline stage — §9.5 + §9.6

Unified ranked retrieval across memory/coderag/docs/uploaded, injection-safe, exposed both as pull-path service functions (for mcp-v2) and as a `ProxyPipeline` stage that auto-injects for plain clients. Client-type detection decides pull vs inject. Flag: the three §9 flags gate their respective sources.

**Files:** Create `shared/knowledge/retriever.py`, `proxy/apps/proxy_server/pipeline/knowledge_stage.py`, `tests/unit/proxy/test_knowledge_stage.py`.

- [ ] **Step 1: Write failing tests** — (a) `KnowledgeRetriever.retrieve(query, caller_scope, sources)` ranks across all enabled sources, each result injection-re-filtered (Task 4) and provenance-tagged; (b) **client-type matrix**: an MCP-capable key (MCP session exists or `mcp_capable`) → `KnowledgeInjectStage` injects **nothing** (agent pulls); a plain OpenAI-compatible key → retrieved context ranked, truncated to the token budget, injected as one system-adjacent provenance-headed message; (c) **token-budget truncation boundary** — default 2000, per-key override honored, truncation cuts at the budget without splitting a provenance block; (d) `usage.waddleai.injected_tokens` accounts the injected count; (e) per-key `memory_injection: {enabled, sources, token_budget}` override in both directions; (f) pull-path `search_code`/`search_docs`/`memory_search` service functions return the same injection-safe results (contract for mcp-v2); (g) **flag-off proof** — with `coderag`/`docs_cache`/`knowledge_ingest`/`proxy_memory` all OFF the stage is a no-op and request bytes are unchanged.
- [ ] **Step 2: Run tests, verify they fail.**
- [ ] **Step 3: Implement** — `KnowledgeRetriever` composes `code_search` (Task 7), `docs_cache` search, `rag_documents` search, and conversation-memory search, ranked by `scoping.rank`; `filter_for_inject` on the merged set. `KnowledgeInjectStage(Stage)` (flag-aware, ordered after routing/security-in, before dispatch): detect client type from the virtual-key `mcp_capable` flag / active MCP session; if plain, build the budgeted provenance-headed injection and set `ctx.usage.waddleai.injected_tokens`; expose `search_code`/`search_docs`/`memory_search` as module-level service fns. Each source gated on its own flag.
- [ ] **Step 4: Run tests, verify pass.**
- [ ] **Step 5: Commit**
  ```bash
  git add shared/knowledge/retriever.py proxy/apps/proxy_server/pipeline/knowledge_stage.py tests/unit/proxy/test_knowledge_stage.py
  git commit -m "feat(knowledge): unified retriever + hybrid-delivery KnowledgeInjectStage (§9.5/§9.6)" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

### Task 12: §9.8 acceptance verification + org-isolation security suite

Turn each §9.8 acceptance item into an explicit verify step; isolation/injection-safety are security tests; enforce the §14.2 gates (90% coverage on changed modules, flag-off proofs).

**Files:** Create `tests/integration/test_knowledge_acceptance.py`.

- [ ] **Step 1: CodeRAG precision + incremental** — index this repo as fixture → symbol-retrieval precision on a labeled query set; one changed file → only its chunks re-embedded (reuses Task 6/7 assertions in the acceptance suite).
- [ ] **Step 2: Docs fetch** — against the local HTTP fixture server (never live), assert TTL + attribution (CC-BY-SA notice present).
- [ ] **Step 3: PDF/MD ingestion round-trip** — upload → searchable → provenance intact.
- [ ] **Step 4: Client-type injection matrix** — MCP key → injection off; plain key → injection on; token-budget truncation boundary.
- [ ] **Step 5: Scoping/trust/isolation suite (security)** — session-memory isolation between two users in the same repo; branch-scoped CodeRAG (feature/A never returns feature/B in-flight code); explicit-promotion-only (auto-captured memory never auto-appears at org scope); contradiction→quarantine→supersede; dispute/correct kill-switches; unverified-memory provenance header present.
- [ ] **Step 6: Injection-safety (security)** — write-time injection caught before persistence; read-time re-filter on scope-promoted poison.
- [ ] **Step 7: Org-isolation on all stores (security)** — `code_chunks`, `docs_cache_pages`, `rag_documents`, `memory_embeddings`: org A never reads org B.
- [ ] **Step 8: Flag-off proof** — `coderag`/`docs_cache`/`knowledge_ingest` OFF → no knowledge behavior, request/response bytes unchanged; contract snapshots green (`make test-contract 2>&1 | tail -20`).
- [ ] **Step 9: Coverage + license gate** — `python3 -m pytest tests/ -k knowledge --cov --cov-fail-under=90 2>&1 | tail -15`; `pip-licenses` shows no non-OSI dep and no PyMuPDF/AGPL.
- [ ] **Step 10: Commit**
  ```bash
  git add tests/integration/test_knowledge_acceptance.py
  git commit -m "test(knowledge): §9.8 acceptance + org-isolation/injection-safety security suite" \
             -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
  ```

---

## Self-Review Against Spec §9

| Spec §9 requirement | Task |
|---|---|
| §9.1 tree-sitter function/class chunking + `path>class>signature` headers + line-window fallback | 5 |
| §9.1 `code_repos`/`code_chunks` tables | 1 |
| §9.1 server-side git-pull worker, content-hash incremental re-index, webhook/cron/manual | 6 |
| §9.1 hybrid pgvector+FTS reciprocal-rank search, symbol-exact short-circuit | 7 |
| §9.2 `docs_cache_pages` + on-demand fetch → markdownify → chunk → embed, TTL 30d/7d | 8 |
| §9.2 robots.txt + rate-limit + per-source license (`docs_sources`) | 1, 8 |
| §2.5 CC-BY-SA attribution (MDN/cppreference) in provenance headers | 1, 8 |
| §9.3 `/api/v1/knowledge` upload+CRUD + CLI; pypdf/docling, PyMuPDF banned; MD direct | 9 |
| §9.3 org-scoped `rag_documents` + provenance | 1, 9 |
| §9.4 conversation-memory config → `/api/v1/memory-config`, seeded 0.7/top-3 defaults | 10 |
| §9.5 hybrid delivery — MCP→pull, plain→budgeted auto-inject (2000, per-key override) | 11 |
| §9.5 `usage.waddleai.injected_tokens` | 11 |
| §9.6 provenance-tag + tier-1/2/3 re-filter before inject; filter writes at store | 4, 11 |
| §9.7 scope hierarchy, composite-key reads, narrower-overrides | 3 |
| §9.7 trust tiers + relevance×trust ranking + unverified provenance header | 3, 4 |
| §9.7 contradiction→quarantine→supersede; version/attribution; correct/dispute/promote | 3, 10 |
| §9.7 per-user session isolation; branch-scoped CodeRAG retrieval | 3, 7 |
| §9.7 memory tables scope/trust/version/status/expires columns (fold into 011) | 1 |
| §9.7 embedding_cache dedup (§6A.3) + §7 embeddings assignment | 2 |
| §9.8 acceptance items (each an explicit verify) | 12 |
| §9.8 isolation/injection-safety as security tests | 3, 4, 7, 9, 12 |
| §13.1 migration 011 (tables + provenance/scope cols) round-trip + downgrade | 1 |
| §14.5 flags `waddleai.coderag`/`docs_cache`/`knowledge_ingest`, fail-safe OFF | 6, 8, 9, 11, 12 |
