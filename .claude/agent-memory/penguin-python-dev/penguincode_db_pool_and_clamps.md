---
name: penguincode-db-pool-and-clamps
description: penguincode O7 fix (shared psycopg_pool ConnectionPool + graph_depth/n_vector/limit clamps) -- psycopg_pool stats-key names, OTel private Gauge alias, pg_stat_activity authoritative-bound proof, server/main.py pre-existing-coverage swap-in-place technique.
metadata:
  type: project
---

Fix for ops-audit O7 (`PR #263`, `fix/penguincode-store-pool-and-clamps` ->
release/v0.2.X): every `stores/vector.py`/`stores/graph.py` call opened a
fresh `psycopg.connect()` per query (connection-exhaustion risk against
Postgres `max_connections`, could starve management's SQLAlchemy pool too);
`graph_depth`/`n_vector`/`limit` had no server-side bound.

**`psycopg_pool.ConnectionPool.get_stats()` key names** (psycopg-pool
3.3.3): `pool_min`, `pool_max`, `pool_size`, `pool_available`,
`requests_waiting`, `requests_num`, `usage_ms`, `connections_num` -- not
documented inline in the package; found by reading
`psycopg_pool/base.py`'s class constants. `in_use = pool_size -
pool_available`.

**`opentelemetry.metrics.Gauge` (synchronous, 1.44.0) is importable only as
the private `_Gauge` alias** -- `Meter.create_gauge()`'s own return-type
annotation says `opentelemetry.metrics.Gauge` (resolves inside
`_internal/instrument.py`'s own namespace where the class is literally
named `Gauge`), but the public `opentelemetry.metrics` package re-exports
it as `_Gauge` (still experimental upstream). Don't fight this -- type the
module-level instrument variable `Any` rather than importing the private
name or chasing a public alias that doesn't exist yet.

**Kill-switch flags needing a `ScopeContext` can only gate call sites that
already have one.** `flags.client.is_enabled(key, ctx)` requires a real
`ctx.tenant_id` for the PostHog distinct_id -- server lifecycle code
(pool open/close in `server/main.py`'s `start()`/`stop()`) has no request
scope, so the opt-out flag for "pooled vs per-call connect" is evaluated
**per store-method-call** inside `db/pool.py`'s `connection()` helper
(every `VectorStore`/`GraphStore` method already carries `ctx`), not at
pool-open time. The pool itself is unconditionally opened/closed; only the
borrow path branches on the flag.

**Authoritative "pool is bounded" proof needs `pg_stat_activity`, not
`pool.get_stats()`.** The pool's own stats are this process's bookkeeping
-- query `SELECT count(*) FROM pg_stat_activity WHERE datname =
current_database() AND pid != pg_backend_pid()` from a separate ad hoc
connection instead, since nothing else talks to the ephemeral test
container. Pattern: 2 threads each hold a borrowed connection via a
`threading.Barrier`, main thread attempts a 3rd borrow with a short
`pool_timeout_seconds` and asserts `psycopg_pool.PoolTimeout`, then asserts
the `pg_stat_activity` count is `<= max_size`.

**`server/main.py` baseline coverage is ~20% and this is pre-existing, not
a regression** -- no test in the suite ever constructs `PenguinCodeServer`
and calls `.start()`/`.stop()` (the integration e2e harness builds a raw
`grpc.aio.server()` directly, bypassing it entirely). Proof technique: copy
`git show origin/<base>:<path>` over the working file, rerun `pytest
--cov=penguincode_cli` (broad target only -- see
[[penguincode_coverage_gate_numpy_cov_crash]]), confirm identical
percentage, then restore the edited file from a `/tmp` backup. Don't try
to write a full `start()/stop()` integration test to chase 90% on a file
another agent may be concurrently editing -- report the baseline instead.

**File-set discipline when a sibling agent owns adjacent methods in the
same file**: `server/services/knowledge.py`'s `Index`/`IndexCode` handlers
were explicitly off-limits (another agent's concurrent edit). Added a new
helper (`_clamp_param`) near the existing `_valid_table` helper and edited
only `Query`/`MemorySearch` bodies + one import line -- grep the target
method's line range first (`grep -n "async def "`) to confirm the edit
doesn't bleed into the excluded functions.

See also [[penguincode_coverage_gate_numpy_cov_crash]] (broad `--cov=`
target requirement, ephemeral pgvector recipe) and
[[twire_wiring_knowledge_memory_code_retrieval]] (ScopeContext-required
pattern, pre-existing-debt diff-line correlation technique).
