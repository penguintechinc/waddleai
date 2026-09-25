-- 0003_memory_vectors.sql
--
-- mem0 memory embedding store (T8: services/penguincode/tools/memory.py
-- wraps mem0 add/search so scope is enforced here, not trusted from mem0's
-- own metadata filter -- see spec section 16). Same shape as docs_vectors:
-- Ollama nomic-embed-text, 768-dim, cosine distance, identical scope
-- columns and the same hard tenant boundary enforced at the store layer.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.memory_vectors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    embedding vector(768) NOT NULL,
    document text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    tenant_id uuid NOT NULL,
    org_id uuid,
    team_id uuid,
    owner_user_id uuid,
    visibility text NOT NULL CHECK (visibility IN ('user', 'team', 'tenant')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_vectors_scope
    ON penguincode.memory_vectors (tenant_id, team_id);

CREATE INDEX IF NOT EXISTS idx_memory_vectors_metadata
    ON penguincode.memory_vectors USING gin (metadata);

DO $$
BEGIN
    BEGIN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_memory_vectors_embedding_hnsw '
            || 'ON penguincode.memory_vectors USING hnsw (embedding vector_cosine_ops)';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'hnsw index unavailable for memory_vectors (%), falling back to ivfflat', SQLERRM;
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_memory_vectors_embedding_ivfflat '
            || 'ON penguincode.memory_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)';
    END;
END;
$$;
