-- 0004_graph_nodes.sql
--
-- Node table for the three logical graphs (graph_kind: code | knowledge |
-- memory), see stores/graph.py (T10) and graphs/{code,knowledge,memory}.py
-- (T11-T13). A node is uniquely identified within a tenant+graph+type by its
-- business key (e.g. a file path, a symbol qualified name, an extracted
-- entity name) -- see the uq_graph_nodes_tenant_kind_type_key constraint.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.graph_nodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    graph_kind text NOT NULL CHECK (graph_kind IN ('code', 'knowledge', 'memory')),
    node_type text NOT NULL,
    key text NOT NULL,
    props jsonb NOT NULL DEFAULT '{}'::jsonb,
    tenant_id uuid NOT NULL,
    org_id uuid,
    team_id uuid,
    owner_user_id uuid,
    visibility text NOT NULL CHECK (visibility IN ('user', 'team', 'tenant')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_graph_nodes_tenant_kind_type_key UNIQUE (tenant_id, graph_kind, node_type, key)
);

-- Composite index leading with the scope columns, then graph_kind -- matches
-- how GraphStore.neighbors/subgraph filter (tenant hard boundary first, then
-- which of the three logical graphs).
CREATE INDEX IF NOT EXISTS idx_graph_nodes_scope
    ON penguincode.graph_nodes (tenant_id, graph_kind, team_id);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_props
    ON penguincode.graph_nodes USING gin (props);
