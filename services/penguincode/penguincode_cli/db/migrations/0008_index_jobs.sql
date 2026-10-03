-- 0008_index_jobs.sql
--
-- Durable background-job tracking for the `Index`/`IndexCode` async queue
-- (O10-a security-audit fix). `Index`/`IndexCode` used to run the whole
-- doc-indexing/code-graph job inline on the shared gRPC `ThreadPoolExecutor`
-- -- a few large calls starved `Health.Check`/`Chat`, causing timeouts and a
-- restart loop. Jobs now enqueue here and are drained by a bounded worker
-- pool (see `penguincode_cli/indexing/`), so a pod restart mid-job is
-- recoverable: on startup, every row still `running` from a dead process is
-- marked `failed` (reason `"interrupted"`) by `IndexJobStore.reap_interrupted`
-- -- never silently re-queued, since the worker that owned it is gone and
-- re-running an unknown-progress job could double-write partial results.
--
-- Visibility columns intentionally mirror the narrowest ("user") tier of
-- `docs_vectors`/`graph_nodes`' three-level model, not the full
-- user/team/tenant spread: a job is visible to its own tenant AND its own
-- owner only (`IndexJobStore.get`/`.list_jobs`'s `WHERE tenant_id = ... AND
-- owner_user_id = ...`) -- there is no team- or tenant-wide job visibility,
-- unlike the three-tier vector/graph rows. `team_id`/`org_id` are therefore
-- provenance columns only (which of the caller's teams it indexed into),
-- the same split `0006_pending_lessons.sql` uses between visibility and
-- provenance.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.index_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    org_id uuid,
    team_id uuid,
    owner_user_id uuid NOT NULL,
    job_type text NOT NULL CHECK (job_type IN ('index_docs', 'index_code')),
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'running', 'succeeded', 'failed')),
    chunks_done integer NOT NULL DEFAULT 0,
    chunks_total integer NOT NULL DEFAULT 0,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Hot-path query: `IndexJobStore.get`/`.list_jobs` always filter by
-- (tenant_id, owner_user_id) first -- the job-visibility hard boundary --
-- then order by recency.
CREATE INDEX IF NOT EXISTS idx_index_jobs_scope
    ON penguincode.index_jobs (tenant_id, owner_user_id, created_at DESC);

-- Startup-recovery query: `IndexJobStore.reap_interrupted` scans every
-- `running` row process-wide (not scope-filtered -- it runs once at server
-- boot, before any request). Partial index keeps that scan cheap even on a
-- large table, since `running` rows are always a tiny fraction of the total.
CREATE INDEX IF NOT EXISTS idx_index_jobs_running
    ON penguincode.index_jobs (state)
    WHERE state = 'running';
