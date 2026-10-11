---
name: twire-wiring-knowledge-memory-code-retrieval
description: penguincode T-wire task -- wiring pre-built extractors/stores/retrieval into real flows (docs indexer, memory manager, REPL, ContextInjector); UUID-column gotchas in live tests; ScopeContext strict-typing boundary; pre-existing debt identification via diff-line correlation.
metadata:
  type: project
---

Task: wire penguincode's already-merged knowledge-platform pieces
(`graphs.knowledge.extract_knowledge`, `graphs.memory.extract_memory_graph`,
`graphs.code.index_code`, `retrieval.graphrag.retrieve`) into real call
sites, without touching those modules themselves. Owned files:
`docs_rag/indexer.py`, `docs_rag/injector.py`, `tools/memory.py`,
`core/repl.py`.

**`ScopeContext` is non-Optional in every new module (T10-T14), unlike the
older docs-RAG code (T7/T8) which tolerates `ctx: ScopeContext | None`.**
`index_code`, `extract_knowledge`, `extract_memory_graph`, `graphrag.retrieve`
all type `ctx: ScopeContext` (required) and call `is_enabled(flag, ctx)`
which needs real attributes. Any call site that still has an optional scope
(the CLI REPL, `ContextInjector`) MUST check `ctx is None` and short-circuit
to a graceful no-op/empty-result *before* calling into the new modules --
never pass `None` through and never fabricate a scope.

**Live-Postgres test gotcha: several scope/id columns are `uuid`, not
`text`.** `docs_vectors.id`, and `team_id` on every scope-columned table
(`docs_vectors`, `memory_vectors`, `graph_nodes`, `graph_edges`) are `uuid`
type -- plain test strings like `"chunk-1"` or `"team-1"` fail with
`psycopg.errors.InvalidTextRepresentation` against a real Postgres (silent
pass with any in-memory fake store, since fakes don't type-check). Use
`str(uuid.uuid4())` for these in live tests. By contrast `graph_nodes.key`
IS plain `text` -- reusing a vector row's `id` (a UUID string) as a graph
node's `key` for GraphRAG seed-derivation works fine there.

**GraphRAG seed-key derivation needs zero extra metadata if the producer
reuses the vector row's own `id` as the graph node's `key`.**
`retrieval.graphrag._seed_keys_from_hits` always includes `hit.id` as a
seed candidate (plus optional `node_key`/`node_keys` metadata). For a live
hybrid-retrieval test, upsert a vector row with id X and a graph edge with
`src_key=X` -- no metadata stamping required to prove vector+graph fusion.

**Pre-existing-debt identification recipe (don't fix, don't chase):** when
`ruff format --check` / `mypy --strict` flag a file you touched, diff the
reported line number against `git diff --unified=0 <file> | grep '^@@'`
hunk ranges (old/new line offsets) -- if the flagged line falls outside
every hunk's *new*-file range, it predates your edit (confirm further via
`git show HEAD:<path> | sed -n '<line>p'` if needed). `core/repl.py` and
`docs_rag/indexer.py` both had pre-existing `ruff format` violations and
`core/repl.py`/its transitive imports (`agents/*.py`) have ~350
pre-existing `mypy --strict` errors (untyped legacy code) unrelated to any
touch here -- confirmed via this line-correlation method, not fixed (task
scope = "0 new" errors, not zero total).

**`mypy --ignore-missing-imports` (the actual CI/Makefile invocation) vs
`--strict` disagree on `# type: ignore[import-untyped]` comments.**
`tools/memory.py`'s `from mem0 import Memory  # type: ignore[import-untyped]`
is *needed* under `--strict` (mem0 ships no stubs) but flagged
`unused-ignore` under `--ignore-missing-imports` (which blanket-suppresses
the same error class, making the local ignore redundant). This is a
pre-existing config-flag interaction bug, identical regardless of which
line the comment sits on -- not something a wiring change can trigger or
fix; don't touch `pyproject.toml`'s `[tool.mypy]` to "fix" it without being
asked, since CI apparently already tolerates it.

**Extractor-failure isolation pattern (applies to any "layered enrichment on
a primary write" wiring):** call the primary store write first, THEN the
enrichment extractor in a `try/except Exception` (broad, `# noqa: BLE001`,
logged at WARNING) -- never let the extractor's own internal degradation
(it already handles its own LLM failures) be the only safety net, since a
`GraphStore` write failure inside the extractor is deliberately NOT caught
by the extractor itself (by T12/T13's own design) and would otherwise
propagate into the primary flow.

See also [T13 memory-graph extractor](t13_memory_graph_extractor.md) for
the `source_metadata` scope-stamp contract this task's memory wiring
consumes verbatim.
