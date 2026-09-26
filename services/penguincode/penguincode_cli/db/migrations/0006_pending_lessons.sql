-- 0006_pending_lessons.sql
--
-- Pending lessons-learned review queue (T-L2a). A lesson proposed for
-- firm-wide sharing is scrubbed + verified client-agnostic by
-- lessons/scrub.py's generalize_and_scrub/verify_scrubbed (T-L1); a CLEAN
-- result lands here awaiting human review -- it is never written directly
-- to tenant-visible storage. Approval is the only path to firm-wide
-- (tenant) visibility -- see lessons/store.py's PendingLessonStore (T-L2a)
-- for the sole read/write chokepoint enforcing that every row stays
-- scoped to the proposing tenant (the "firm"), even while pending.
--
-- Scope columns intentionally differ from docs_vectors/graph_nodes: a
-- pending lesson has no per-row team/user *visibility* of its own --
-- review is a tenant-wide administrative action, never scoped to the
-- proposer's team (see PendingLessonStore's module docstring). Instead,
-- source_team_id/proposer_user_id record *provenance* (who/which team
-- proposed it) and `visibility` records the visibility the lesson will be
-- granted on approval -- today always 'tenant', kept as an explicit column
-- (with the same CHECK constraint every other scoped table uses) rather
-- than a hardcoded constant so a future narrower-scope promotion path
-- never requires a schema change.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.pending_lessons (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    org_id uuid,
    source_team_id uuid,
    proposer_user_id uuid,
    visibility text NOT NULL DEFAULT 'tenant' CHECK (visibility IN ('user', 'team', 'tenant')),
    generalized_text text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    findings jsonb NOT NULL DEFAULT '[]'::jsonb,
    reviewed_by uuid,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Composite index leading with the tenant hard boundary, then status --
-- ListPendingLessons(status='pending') (tenant-scoped, never team-scoped)
-- is the hot-path query; created_at trails for the natural review-queue
-- ordering.
CREATE INDEX IF NOT EXISTS idx_pending_lessons_scope
    ON penguincode.pending_lessons (tenant_id, status, created_at);
