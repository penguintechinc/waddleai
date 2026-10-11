-- 0009_index_jobs_scope_text.sql
--
-- Fixes a release-blocking schema defect in `0008_index_jobs.sql`: its four
-- scope columns (`tenant_id`, `org_id`, `team_id`, `owner_user_id`) were
-- typed `uuid`, but WaddleAI's real JWT `tenant`/`sub`/`org`/`teams` claims
-- are opaque strings -- in production specifically the *stringified
-- integer* `organizations.id` primary key (see
-- `shared/auth/penguin_auth.py`'s `tenant=str(user_context.organization_id)`
-- and `services/management/app/models_sqlalchemy.py`'s `Organization.id =
-- Column(Integer, ...)`), never a UUID. `auth.scope.ScopeContext`'s fields
-- are typed `str` for exactly this reason. Any real caller (tenant id
-- `"1"`, `"42"`, ...) hitting `IndexJobStore` would fail every write with
-- `psycopg.errors.InvalidTextRepresentation: invalid input syntax for type
-- uuid` -- caught here by a live-Postgres end-to-end test, not a synthetic
-- `uuid.uuid4()` fixture (see the regression test added alongside this
-- migration in `tests/test_indexing_store.py`).
--
-- Does NOT touch `id` (the job's own generated primary key, legitimately a
-- UUID -- `IndexJobStore` always casts it with `::uuid` and nothing external
-- ever supplies it) or `created_at`/`updated_at`.
--
-- Idempotent and safe against both a fresh (0008-only) and an already-
-- migrated database: `ALTER COLUMN ... TYPE text USING col::text` succeeds
-- whether `col` is currently `uuid` (casts to its string form) or already
-- `text` (no-op cast) -- this migration is also safe to apply to a
-- populated table, since `uuid::text`/`text::text` never lose data for
-- these columns. `ALTER COLUMN TYPE` automatically rebuilds the table's
-- indexes that reference the altered columns, but `idx_index_jobs_scope` is
-- dropped and recreated explicitly below anyway, for the same
-- self-documenting-over-implicit reason `0008`'s own index comments give.
ALTER TABLE penguincode.index_jobs
    ALTER COLUMN tenant_id TYPE text USING tenant_id::text,
    ALTER COLUMN org_id TYPE text USING org_id::text,
    ALTER COLUMN team_id TYPE text USING team_id::text,
    ALTER COLUMN owner_user_id TYPE text USING owner_user_id::text;

DROP INDEX IF EXISTS penguincode.idx_index_jobs_scope;
CREATE INDEX IF NOT EXISTS idx_index_jobs_scope
    ON penguincode.index_jobs (tenant_id, owner_user_id, created_at DESC);

-- `idx_index_jobs_running` is on `state` only (no scope column) -- unaffected,
-- not dropped/recreated.
