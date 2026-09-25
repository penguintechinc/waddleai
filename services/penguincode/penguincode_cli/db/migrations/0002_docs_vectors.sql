-- 0002_docs_vectors.sql
--
-- docs-RAG embedding store (replaces chromadb, see pyproject.toml / T9).
-- Embeddings are Ollama nomic-embed-text, 768-dim, cosine distance.
--
-- Scope columns (tenant_id/org_id/team_id/owner_user_id/visibility) are the
-- hard multi-tenancy boundary enforced by the store layer
-- (services/penguincode/stores/vector.py, T6) -- every read is filtered on
-- these columns, never a raw unscoped query.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE TABLE IF NOT EXISTS penguincode.docs_vectors (
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

-- Composite index leading with the scope columns -- every store query filters
-- on tenant_id (hard boundary) and usually team_id, before the vector
-- distance operator narrows further.
CREATE INDEX IF NOT EXISTS idx_docs_vectors_scope
    ON penguincode.docs_vectors (tenant_id, team_id);

-- Metadata filters (VectorStore.query(where=...)) hit this GIN index.
CREATE INDEX IF NOT EXISTS idx_docs_vectors_metadata
    ON penguincode.docs_vectors USING gin (metadata);

-- Cosine-distance ANN index: HNSW when the installed pgvector supports it
-- (0.5.0+), falling back to ivfflat on older builds. Wrapped in a DO block so
-- the migration stays idempotent and portable across pgvector versions
-- without the runner needing to introspect the extension version itself.
DO $$
BEGIN
    BEGIN
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_docs_vectors_embedding_hnsw '
            || 'ON penguincode.docs_vectors USING hnsw (embedding vector_cosine_ops)';
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'hnsw index unavailable for docs_vectors (%), falling back to ivfflat', SQLERRM;
        EXECUTE 'CREATE INDEX IF NOT EXISTS idx_docs_vectors_embedding_ivfflat '
            || 'ON penguincode.docs_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)';
    END;
END;
$$;
