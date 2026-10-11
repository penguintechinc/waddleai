---
name: ollama-embedding-bulkhead
description: Ollama chat/embedding QoS split (PR #276) — pre-existing wiring gaps fixed, lint-baseline line-drift, and a pre-existing test hang found during verification
metadata:
  type: feedback
---

PR #276 (release/v0.2.X) added an opt-in `ollamaEmbeddings` Helm Deployment +
`OLLAMA_EMBEDDING_URL` (proxy) / `PENGUINCODE_EMBEDDING_OLLAMA_URL`
(penguincode) so bulk embedding traffic can be routed off the chat-serving
Ollama instance. Key findings for future work in this area:

**penguincode's embedding call sites were NEVER wired to `settings.ollama`
at all** — `KnowledgeServiceImpl.__init__`'s default `DocumentationIndexer()`
and `KnowledgeServiceImpl.Query`'s `retrieve()` calls used hardcoded
`localhost:11434` defaults, completely ignoring the real `ollama.api_url`
config. Fixing the bulkhead required fixing this underlying gap first (not
scope creep — the bulkhead is unreachable without it). Grep for a
"default-constructed with a literal URL, never passed `settings.X`" pattern
before assuming a config value is actually live anywhere in this codebase.

**mypy/ruff baseline files are line-number-keyed, not message-keyed** —
inserting code anywhere above an existing error in the same file makes
every downstream baseline entry read as "new" even with zero semantic
change. Proof technique: `sed 's/path:[0-9]*: //'` both old (`git show
<base>:<path>`) and new file's mypy output, `sort`, `diff` — zero diff
means pure line drift, safe to `--write-baseline` regenerate. Did this for
every touched penguincode file before regenerating; all were zero-diff.

**Pre-existing test hang found, NOT caused by this PR**:
`tests/test_server_lessons_service.py`'s `TestPromoteLesson`/
`TestPromoteLessonEndToEnd` classes hang indefinitely in a sandbox with no
reachable Postgres — `PromoteLesson`'s `asyncio.to_thread(self.
_known_identifiers, ctx)` → `_known_tenant_identifier_names` → graph-store
lookup blocks rather than failing fast (no `requires_postgres` skip guards
this path, unlike the explicitly-marked live-DB tests in the same file).
Reproduced byte-identically by swapping in the unmodified base-commit
`lessons.py` and rerunning the same test — confirms it's unrelated to any
diff touching that file. [[tests_encode_broken_behaviour]] is the sibling
pattern (wrong assertion); this is the hang/missing-skip-guard variant —
worth a dedicated fix PR adding a `requires_postgres`-style guard or a
fast-fail connection timeout to `db/pool.py`'s pool construction.

**`proxy/requirements.txt` + root `.venv`, not `services/penguincode`'s own
venv, for proxy-side `shared/`/`tests/unit` work** — confirmed again:
`uv venv -p 3.13 .venv && uv pip install -r proxy/requirements.txt` at repo
root is the correct recipe (no root `requirements.txt` works standalone).

**Severe CPU contention from concurrent sibling agents in this sandbox
makes `| tail -N` on a long-running pytest invocation show ZERO output
until the whole run finishes or is killed** — `tail` buffers until EOF on
a pipe. Use `tee <file>` (so the file fills progressively and can be
`cat`'d mid-run) or redirect straight to a file with no pipe at all; never
diagnose "stuck" purely from an empty `| tail` capture — check the file
directly and the process's `ps -o etimes,time,pcpu` trend before
concluding a hang vs. starvation.
