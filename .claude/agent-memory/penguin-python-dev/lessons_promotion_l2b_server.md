---
name: lessons-promotion-l2b-server
description: T-L2b LessonsService server handlers + approval authz + client + REPL — approve-scope, SoD, firm-wide memory write, nested RS256 interceptor routing
metadata:
  type: project
  project: waddleai
  category: penguincode-knowledge-platform
  task: T-L2b
---

Built the lessons-learned promotion review workflow server-side on top of L1 (scrub.py)
+ L2a (proto/store, already merged). Branch `feature/lp-l2b-server` off the lessons feature
branch, in `.worktrees/lp-l2b/services/penguincode`.

**Approval authz**: minted a new scope constant `LESSONS_APPROVE_SCOPE = "lessons:approve"`
in `server/services/lessons.py` (no prior admin/approve scope existed in penguincode —
unlike WaddleAI's management service, this is a standalone gRPC service with no scope
registry to reuse). `ApproveLesson`/`RejectLesson` check `LESSONS_APPROVE_SCOPE in ctx.scopes`
and abort `PERMISSION_DENIED` otherwise. Separation of duties is ENFORCED (not just allowed)
on approve: `record.proposer_user_id == ctx.user_id` → `PERMISSION_DENIED`, even with the
scope — reject has no such check (only approve promotes to firm-wide).

**Firm-wide write**: `ApproveLesson` calls `ScopedMemoryManager.add(ctx, generalized_text,
visibility="tenant", metadata={...})` (tools/memory.py, pre-existing from T8/knowledge
platform) — tenant-visibility already means readable by any team in the same tenant per
that module's `_is_visible`. The write is best-effort (a `None` result — memory disabled or
`penguincode.rag` flag off — is logged and approval still proceeds); `pending_lessons.status`
transition is the RPC's only guaranteed effect, per the proto's own doc comment.

**All four RPCs derive `ScopeContext` from `current_scope_context()` only** (never request
body). `ListPendingLessons`/`ApproveLesson`/`RejectLesson` additionally call an explicit
`_require_flag_enabled` (aborts `FAILED_PRECONDITION`) since they don't go through
`generalize_and_scrub` — only `PromoteLesson` gets the flag gate "for free" (scrub.py returns
a blocked ScrubResult when the flag is off, never an abort, preserving `blocked=true` as a
normal 200-style response rather than an RPC error).

**Interceptor wiring**: `MethodPrefixRoutingInterceptor` (server/interceptors.py, F2-owned,
not touched) only supports ONE prefix pair. Rather than editing it to support a multi-prefix
table, nested two instances in `server/main.py`: outer routes KnowledgeService prefix →
WaddleAIAuthInterceptor, unmatched → inner router (LessonsService prefix →
same WaddleAIAuthInterceptor instance, unmatched → legacy HS256). Confirmed
mypy-error-count-identical (8 in main.py, 30 in repl.py) before/after via
`git show HEAD:path > path` in-place diff trick (not `git stash`, which is forbidden) — same
error messages, just shifted line numbers, proving zero new errors from wiring changes in
files that already carry pre-existing mypy debt.

**PendingLessonStore is sync/blocking** (plain psycopg, no async) — every call from the
async servicer goes through `asyncio.to_thread(...)`, mirroring how `knowledge.py`'s
`IndexCode` already wraps `graphs.code.index_code`.

**Live pgvector proof of "firm-wide"**: real `PendingLessonStore` (live pg) + real
`ScopedMemoryManager` wrapping a hand-rolled `_FakeMem0Backend` (in-process
add/search dict, no real Ollama/embeddings) — proves the actual `ApproveLesson` handler +
`ScopedMemoryManager`'s scope-stamping/`_is_visible` read-filter genuinely make a
tenant-visibility write readable by a THIRD team/ctx in the same tenant, without needing a
real mem0/Ollama stack. See [[t13_memory_graph_extractor]] and [[twire_wiring_knowledge_memory_code_retrieval]]
for the underlying `ScopedMemoryManager` conventions this reuses unmodified.

**Ollama-mock-at-the-real-boundary pattern for a genuine end-to-end scrub test**: rather than
monkeypatching `generalize_and_scrub` itself (which would just be re-testing a mock),
`monkeypatch.setattr("penguincode_cli.lessons.scrub.OllamaClient", lambda **_kw: FakeCtx(...))`
lets the REAL `generalize_and_scrub` + `verify_scrubbed` pipeline run through
`LessonsServiceImpl.PromoteLesson`, with only the network-facing Ollama call faked — proves
client-confidential content is genuinely blocked by the real verifier, not a test that only
proves a mock returns what it was told to return.

**mypy gate for this service** = `mypy <path> --ignore-missing-imports` (pyproject.toml sets
`strict = true`, so this combo is the real effective gate, matching the Makefile's `lint`
target) — confirmed by running the SAME command on `knowledge.py`/`knowledge_client.py`
(zero errors) before starting. Bare `--strict` alone adds ~5 false "library stubs not
installed" noise per file that imports `grpc`/`google.protobuf` — don't chase those.

**No `.venv` existed in this worktree** — `uv venv -p 3.13 .venv && uv pip install -e ".[dev]"`
from `services/penguincode` (installs mem0ai/qdrant-client/tree-sitter, a few minutes but
uv's cache makes it fast). Baseline before any change: 860 passed / 89 skipped, 0 failed.

**Live-pgvector recipe reused from T-L2a/F2**: `docker run -d --rm --name pc-lessons-pg -p
55463:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=penguincode_test
pgvector/pgvector:pg17`, then `TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55463/penguincode_test`.
Each live test module's own fixture does `DROP SCHEMA IF EXISTS penguincode CASCADE` +
`run_migrations` — safe to share one container across all live test files in one run.
