-- 0005_graph_edges.sql
--
-- Edge table for the three logical graphs. src_id/dst_id are foreign keys
-- into graph_nodes.id (the surrogate key) -- GraphStore (T10) resolves a
-- GraphEdge's src_key/dst_key (tenant_id, graph_kind, node_type, key) to the
-- node row's id before upserting here, so the FK -- not a bare text key --
-- is what "references node keys" in practice: an edge can never point at a
-- node outside its own tenant+graph, because both ids must already exist in
-- graph_nodes under that scope.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.graph_edges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_kind text NOT NULL CHECK (graph_kind IN ('code', 'knowledge', 'memory')),
    src_id uuid NOT NULL REFERENCES penguincode.graph_nodes (id) ON DELETE CASCADE,
    dst_id uuid NOT NULL REFERENCES penguincode.graph_nodes (id) ON DELETE CASCADE,
    rel_type text NOT NULL,
    props jsonb NOT NULL DEFAULT '{}'::jsonb,
    tenant_id uuid NOT NULL,
    org_id uuid,
    team_id uuid,
    owner_user_id uuid,
    visibility text NOT NULL CHECK (visibility IN ('user', 'team', 'tenant')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_graph_edges_tenant_kind_src_dst_rel UNIQUE (tenant_id, graph_kind, src_id, dst_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_scope
    ON penguincode.graph_edges (tenant_id, graph_kind, team_id);

CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON penguincode.graph_edges (src_id);

CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON penguincode.graph_edges (dst_id);
