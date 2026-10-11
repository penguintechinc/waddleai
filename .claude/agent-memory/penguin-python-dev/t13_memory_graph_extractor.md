---
name: t13-memory-graph-extractor
description: T13 penguincode memory-graph extractor — source_metadata scope-stamp reuse pattern, GraphStore validation chokepoint, live pgvector test recipe
metadata:
  type: project
---

T13 (`services/penguincode/penguincode_cli/graphs/memory.py`) mirrors T12's
`graphs/knowledge.py` (LLM triple extraction, robust malformed-JSON parsing,
`httpx.HTTPError` degrades to empty `Subgraph`, flag-gated) with one added
wrinkle: `extract_memory_graph(ctx, content, *, source_metadata=None,
visibility="user", team_id=None, settings=None, ollama_client=None,
graph_store=None)` is designed to be called right after T8's
`ScopedMemoryManager.add()` returns, passed that call's own
`result["results"][0]["metadata"]` as `source_metadata` — T8's
`_scope_metadata()` output (`tenant_id`/`org_id`/`team_id`/`owner_user_id`/
`visibility`). When `source_metadata["visibility"]` is a non-empty string it
and the paired `team_id` (may legitimately be `None`) replace the
`visibility`/`team_id` keyword defaults wholesale — so extracted triples
land in the identical scope bucket as the memory itself, not one
independently re-derived and possibly drifted.

**Why this is safe without extra validation in memory.py**:
`PostgresGraphStore.upsert_nodes`/`upsert_edges` (`stores/graph.py`'s
`_scope_columns`) already (a) derives `tenant_id`/`org_id`/`owner_user_id`
from `ctx` only, never from any caller-supplied value, and (b) validates
`team_id in ctx.team_ids` when `visibility=="team"`, raising `ValueError`
otherwise. So reading `visibility`/`team_id` straight out of
`source_metadata` and passing them to the store cannot widen `ctx`'s own
identity or let a caller stamp into a team it doesn't belong to — the store
is the enforcement chokepoint, `memory.py` doesn't need to re-validate.

Default `visibility="user"` (not T12's `"tenant"`) — memories are personal
by nature, matching `ScopedMemoryManager.add()`'s own default.

Live-Postgres test recipe that worked: `docker run -d --rm --name
<unique> -e POSTGRES_PASSWORD=test -e POSTGRES_DB=penguincode_test -p
55446:5432 pgvector/pgvector:pg17`, wait on `pg_isready`, then
`TEST_DATABASE_URL=postgresql://postgres:test@localhost:55446/penguincode_test
pytest tests/test_graphs_memory.py` — the fixture's `run_migrations(dsn=...)`
(from `db.migrate`) builds the schema fresh per module. `docker stop` cleans
up (`--rm` auto-removes).

`mypy --strict` on penguincode has 26 pre-existing baseline errors, all in
`config/settings.py` (untyped `yaml` stubs, missing generic type args,
bool/int/float assignment mismatches) and `ollama/client.py` (missing
annotations, `Any` returns) — confirmed identical count/lines when running
mypy against T12's already-merged `knowledge.py` + its test file, so these
are pre-existing debt, not something a new graphs/*.py file introduces.
Zero new errors is the correct bar per file, not zero errors repo-wide.

See also [apikey_o1_lookup_and_producer_consistency](apikey_o1_lookup_and_producer_consistency.md)
for the general pattern of "producer contract must match consumer
assumption exactly" — same shape of risk (T8's `add()` and T13's extractor
must agree on the scope-metadata dict shape) though a different bug class.
