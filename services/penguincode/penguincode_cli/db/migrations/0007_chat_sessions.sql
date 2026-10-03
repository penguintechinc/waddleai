-- 0007_chat_sessions.sql
--
-- Cross-pod chat-session persistence (security audit finding O4-a, High).
-- `ChatServiceImpl` (server/services/chat.py) used to keep
-- `sessions: dict[str, SessionState]` as in-process state while
-- CreateSession/Chat/GetHistory/CloseSession are independent gRPC RPCs and
-- prod runs `replicas=3` -- a session created on one pod 404s on every
-- other pod, and a rollout drops every in-flight session. This table is
-- the shared-Postgres replacement; see
-- `penguincode_cli/sessions/store.py`'s `PostgresSessionStore` for the sole
-- read/write chokepoint (never raw SQL from a servicer).
--
-- Scope columns mirror graph_nodes'/pending_lessons' tenant hard boundary;
-- a session is additionally owned by exactly one user (user_id) -- unlike
-- graph_nodes' three-tier visibility, a chat session is never team- or
-- tenant-shared, only ever visible to its own tenant AND owning user (see
-- store.py's module docstring). `team_ids` records the caller's team
-- membership at creation time for audit/provenance only, same rationale as
-- pending_lessons.source_team_id -- it is never part of the read filter.
--
-- `expires_at` is set (and refreshed on every update_state) from
-- PENGUINCODE_SESSION_TTL_SECONDS (default 24h) -- see store.py's
-- SessionSweeper, which deletes expired rows in bounded batches on
-- PENGUINCODE_SESSION_SWEEP_INTERVAL_SECONDS.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.chat_sessions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    org_id uuid,
    team_ids uuid[] NOT NULL DEFAULT '{}',
    user_id uuid NOT NULL,
    project_dir text NOT NULL,
    client_tools jsonb NOT NULL DEFAULT '[]'::jsonb,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- Hot-path lookup: Chat/GetHistory/CloseSession all filter on exactly this
-- pair (a caller's own session within its own tenant).
CREATE INDEX IF NOT EXISTS idx_chat_sessions_scope
    ON penguincode.chat_sessions (tenant_id, user_id);

-- SessionSweeper's expiry scan.
CREATE INDEX IF NOT EXISTS idx_chat_sessions_expires
    ON penguincode.chat_sessions (expires_at);
