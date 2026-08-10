# Memory Access Control — Personal vs Organizational Scope

**Date:** 2026-07-14
**Status:** Approved design (brainstormed section-by-section)
**Target release:** v0.2.x (dedicated feature branch)
**Relationship to platform spec:** Forward-compatible subset of `2026-07-09-waddleai-platform-spec.md` §9.7 (Memory Scoping & Trust). This design ships two of §9.7's five scopes (`user`, `org`) using §9.7's field names (`scope_type`, `author_user_id`) so v0.4.x *extends* the model (adds `session`/`project`/`repo` scopes, trust tiers, promotion, versioning) rather than migrating it.

## Context

PR #50 locked the mem0-compatible memory API to personal-per-user as the secure default: `organization_id` and `user_id` are forced from the token, cross-user and cross-org access return 403, and org-0 tokens are rejected. That closed the security gap but removed any way for a team to share memories.

This feature adds an explicit, opt-in **organizational scope** so a team/org can share memories and learnings through the same mem0/MCP layer, while personal memories stay locked to their owner. Sharing is always a deliberate act signaled by the caller — never a default and never a side effect.

## Decision summary

| Topic | Decision |
|---|---|
| Scopes (this release) | `user` (personal, **default**) and `org` (shared within the token's org) |
| Signaling | Top-level `scope` body field; fallback `metadata.scope`; absent → `user`; invalid → 400 |
| Write model | Any org member can write org-scoped memories; author or moderator can delete shared entries; personal is owner-only |
| Read model | Search/list return personal + org merged, ranked by normal relevance — no artificial scope ordering; optional scope filter |
| Implementation | Both: authoritative `scope_type` column (pgvector/Postgres) **and** `metadata.scope` mirror (backward compat + metadata-only backends) |
| Moderation | New `Permission.MEMORY_MODERATE = "memory:moderate"` granted to ADMIN and RESOURCE_MANAGER role bundles; checks on the permission, never role names |
| Auto-capture | Automatic chat-turn memory always stays personal; promotion-to-org is a §9.7 v0.4 feature |
| Backfill | All existing rows → `scope_type='user'`, `author_user_id=user_id`; nothing becomes shared retroactively |
| Tenancy | All PR #50 locks unchanged — org forced from token, org-0 rejected, request `user_id` must match token. Scope is a visibility dimension *inside* the org; it never crosses orgs |

---

## Section 1 — Data model & migration

**Alembic migration 006** (`006_add_memory_scope.py`) on `memory_embeddings`:

| Column | Type | Default | Purpose |
|---|---|---|---|
| `scope_type` | `String(20)`, indexed, NOT NULL | `'user'` | `'user'` (personal) or `'org'` (shared). §9.7 field name — v0.4 adds values (`session`, `project`, `repo`) without renaming |
| `author_user_id` | `Integer`, indexed, NOT NULL | backfill = `user_id` | Who wrote it. Ownership basis for delete rights on shared entries; attribution later |

- **Backfill:** existing rows → `scope_type='user'`, `author_user_id=user_id`. Downgrade drops both columns cleanly (round-trip tested).
- **Composite index** `(organization_id, scope_type)` so org-shared searches stay indexed.
- **SQLAlchemy:** mirror both columns on `MemoryEmbedding` (`services/management/app/models_sqlalchemy.py`). Alembic remains sole schema authority.
- **Dataclass:** `MemoryEntry` (`shared/utils/memory_integration.py`) gains `scope_type: str = "user"` and `author_user_id: int = 0`.
- **Dual-write compat:** on store, the entry's `metadata` JSON also gets `"scope": "<user|org>"` — stock mem0 clients and metadata-only backends see scope without knowing about columns. The column is authoritative wherever it exists (pgvector).
- **Field semantics:** `user_id` keeps meaning "whose memory space this lives in" for personal entries. For org entries `user_id = author_user_id` (kept populated so nothing downstream breaks), but reads select org entries by `(organization_id, scope_type='org')`, never by `user_id`.

## Section 2 — API surface & scope signaling

All five handlers in `proxy/apps/proxy_server/mem0_api.py`. Existing clients that send nothing new keep exactly today's behavior (everything personal) — no breaking change.

**Writing (`POST /mem0/memories`):**
- Accept top-level `"scope": "user" | "org"`; fallback to `metadata.scope` if absent; default `"user"` when neither present.
- Any other value → **400** `"invalid scope"` (strict — no silent coercion of typos into personal).
- Stored entry always gets the `scope_type` column, the `metadata.scope` mirror, and `author_user_id` = token user.

**Reading (`POST /mem0/search`, `GET /mem0/memories`):**
- Default: merged view — caller's personal memories **plus** all org-scoped memories for the token's org — ranked purely by relevance (search) / recency (list).
- Optional `scope` filter (body field on search, query param on list): `"user"` → personal only; `"org"` → shared only; absent → merged; invalid → 400.
- Every returned memory object gains two **additive** fields: `"scope"` and `"author_user_id"` (enables "shared" badges and attribution). The mem0 golden contract snapshots change additively and are re-recorded with justification; non-mem0 snapshots must not change.

**Deleting:**
- `delete_memory` (`DELETE /mem0/memories/<id>`): no API-shape change — Section 3 permission logic decides 403 vs 200 based on the target row's scope and author.
- `clear_memories`: gains the same optional `scope` param. Absent → clears **personal only** (an unscoped clear must never nuke shared team knowledge). `scope=user` → same. `scope=org` → clears only org entries authored by the caller. `scope=org&all=true` → full org wipe, moderator-gated (Section 3).

## Section 3 — Enforcement rules

**New permission (the only RBAC change):** `MEMORY_MODERATE = "memory:moderate"` added to the `Permission` enum in `shared/auth/rbac.py`, granted in `ROLE_PERMISSIONS` to the **ADMIN** and **RESOURCE_MANAGER** bundles. Handlers check `user.has_permission(Permission.MEMORY_MODERATE)` — never role names. No new permission is needed for *writing* org memories: any authenticated org member can share; existing proxy-use auth establishes that.

**Enforcement matrix (all within the token's org — cross-org stays impossible):**

| Action | `scope=user` (personal) | `scope=org` (shared) |
|---|---|---|
| Create | owner only (`user_id` = token, as today) | any org member; `author_user_id` = token |
| Read (search/list) | owner only | any org member |
| Delete single | owner only | **author** OR holder of `memory:moderate` |
| Clear own | yes (default clear) | `scope=org` clears caller-authored entries only |
| Clear entire org store | n/a | `scope=org&all=true`, requires `memory:moderate`; otherwise 403 |

**Mechanics in `delete_memory`:** replaces the blind `WHERE id AND user_id AND organization_id` delete with fetch-then-decide: SELECT the row by `(id, organization_id)`; missing → 404; `scope_type='user'` and not owner → 403 (matches the handler's existing mismatch responses); `scope_type='org'` → allow if `author_user_id == token_user` or moderator, else 403 `"not memory author"`. The clear path's `all=true` branch uses the same fetch-then-decide.

**Claims-path note:** permissions are not carried in the JWT — `claims_dict_to_user_context` re-derives them from the role claim via `ROLE_PERMISSIONS`, so both auth paths (middleware `api_key_verifier` and the `get_current_user` fallback) pick up `memory:moderate` automatically with no token-format change.

## Section 4 — Backend changes (`shared/utils/memory_integration.py`)

**`PgvectorMemoryStore` (authoritative — default/production backend):**
- **Store:** writes the real `scope_type` / `author_user_id` columns plus the `metadata.scope` mirror.
- **Search/list:** where-clause changes from `(user_id AND organization_id)` to `organization_id = :org AND (scope_type = 'org' OR (scope_type = 'user' AND user_id = :user))` — one indexed query returns the merged view, relevance-ranked. The optional scope filter collapses the OR to a single branch. The composite `(organization_id, scope_type)` index keeps the org branch cheap.
- **Delete/clear:** fetch-then-decide per Section 3, using the columns.

**`ChromaDBMemoryStore` and `Mem0MemoryStore` (metadata-only backends):**
- No schema; `metadata.scope` **is** the scope. Store writes it. **Absent `scope` in stored metadata = personal** — covers all legacy entries with zero backfill on these backends.
- Search: where the backend's filter language supports it, one `$or` filter (`scope='org' AND org matches` OR `user_id matches AND scope != 'org'`); where it doesn't (mem0 client's simpler filters), issue **two queries — personal and org — and merge by relevance score** before truncating to the requested limit. Implementation note, not a behavior difference: same merged result either way.
- Delete/clear: fetch the entry's metadata first, apply the Section-3 decision. Slightly weaker guarantee than DB columns; acceptable and documented — these backends are the non-production tier.

**`WaddleAIMemoryManager`:** gains `scope` passthrough on store/search/delete methods with default `"user"` — every internal caller (including auto-capture) compiles unchanged and stays personal unless it opts in.

## Section 5 — Auto-captured memory & backfill

- **Auto-captured conversation turns stay personal.** The proxy's automatic memory capture always writes `scope_type='user'` — sharing is an explicit human act via the API, never a side effect of talking to a model. "Promote to org" is §9.7's explicit-promotion mechanism (v0.4), not this release.
- **Backfill (migration 006):** every existing row → `scope_type='user'`, `author_user_id = user_id`. Nothing becomes shared retroactively; no existing memory changes visibility. Downgrade drops both columns.
- **Metadata-only backends need no backfill:** absent `metadata.scope` already means personal.

## Section 6 — Testing

**Contract tests (proxy, seeded creds):**
- Org-scoped add → visible to a *second* seeded same-org user; personal add → invisible to that second user (core isolation assertion, `# regression:` tagged).
- Scope filter on search/list; invalid scope → 400.
- Unscoped clear leaves org entries untouched.
- Delete of another author's org entry → 403 without `memory:moderate`, → 200 with it (admin-role seeded key).
- `all=true` org wipe gated on the permission.
- PR #50 locks re-asserted unchanged: org-0, cross-org, cross-user 403s.

**Seed data:** `_seed_contract_test_data()` gains a second same-org user and one admin-role API key — required to prove sharing and moderation.

**Migration test:** 006 upgrade → backfill values asserted → downgrade round-trip.

**Snapshot discipline:** only mem0 snapshots may change (additive `scope`/`author_user_id` fields); any non-mem0 snapshot change fails the task.

**Unit tests:** pgvector where-clause branches; two-query merge for the mem0-client backend; `ROLE_PERMISSIONS` includes `memory:moderate` for ADMIN and RESOURCE_MANAGER only.

## Out of scope (deferred to §9.7 / v0.4.x)

- `session`, `project`, `repo` scopes
- Trust tiers (`verified` > `confirmed` > `derived` > `unverified`)
- Explicit promotion workflow (personal → org), versioning, quarantine, `superseded_by`, `status`, `expires_at`
- Memory-injection protections beyond current content-filter behavior
- Proxy PyDAL → penguin-dal refactor (separate tracked follow-up; this feature works with the current PyDAL layer)

## Delivery

- **Branch:** dedicated feature branch (`feature/memory-access-control`) in the v0.2.x line, built on top of the PR #50 consolidation code (the mem0 handlers as shipped there).
- **Feature flag:** `waddleai.memory-org-scope` (PostHog, default OFF) gating acceptance of `scope=org` on write; when OFF, behavior is identical to today's personal-only lockdown.
- **Merge gates:** contract suite green (including new isolation/moderation tests), unit suite green, migration round-trip test, no non-mem0 snapshot drift.
