"""Auto Memory / Knowledge Layer (§9): CodeRAG, docs cache, ingestion + §9.7 scope/trust cols.

Creates ``code_repos``, ``code_chunks``, ``docs_cache_pages``, ``docs_sources``
(pgvector + FTS where Postgres is available) and extends ``rag_documents`` /
``memory_embeddings`` with the remaining §9.7 scope/trust/version/status/
provenance columns. ``memory_embeddings.scope_type``/``author_user_id``
already shipped in migration 006 -- this migration extends that column set,
it does not re-add them.

Vector/FTS columns are guarded on dialect: Postgres gets a real
``vector(768)`` column (matching the §7.1 ``embeddings`` assignment default,
nomic-embed-text) plus an ivfflat cosine index and a generated ``tsvector``
FTS column with a GIN index; any other dialect (SQLite in tests/dev) falls
back to a nullable ``Text`` column so schema creation stays portable, mirroring
the existing degrade-gracefully-without-pgvector pattern in
``models_sqlalchemy.py:init_schema``.

Revision ID: 012_knowledge
Revises: 006_add_memory_scope (placeholder -- see TODO(rebase) below)
Create Date: 2026-07-09

TODO(rebase): down_revision is a placeholder pointing at 006, the actual
resolvable head in this worktree. Migrations 007-011 are being written
concurrently on other feature branches (drop_ailb_add_native_limits,
model_registry, response_cache, proxy_memory, routing_engine, security_v2)
and do not exist here yet. The intended final chain per §13.1 is
006 -> 007 -> 008 -> 009a -> 009b -> 010 -> 011_security_v2 -> 012_knowledge
-> 013 -> 014; down_revision must be repointed to "011_security_v2" at merge
time, once that script actually exists.

Pointing down_revision at a revision ID that isn't present in this worktree
(e.g. the literal string "011_security_v2") was tried first, but Alembic's
``ScriptDirectory`` validates the *entire* revision graph on first access --
a single dangling down_revision reference breaks `heads`/`upgrade`/`stamp`
for every migration in the directory, not just this one, which took down the
passing migration-006 round-trip test as collateral damage. Chaining onto
the real local head keeps the graph valid until reconciliation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_knowledge"
down_revision: str | None = "011_security_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# §9.2 seed rows: fixed per-source license table. PyPI project docs are
# deliberately excluded -- license varies per package, so there is no single
# static row to seed; docs_cache.py resolves per-package license at fetch time.
_DOCS_SOURCES_SEED = [
    {
        "ecosystem": "python",
        "base_url": "https://docs.python.org",
        "license": "PSF",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "rust",
        "base_url": "https://doc.rust-lang.org",
        "license": "MIT/Apache-2.0",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "rust-crates",
        "base_url": "https://docs.rs",
        "license": "MIT/Apache-2.0",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "go",
        "base_url": "https://pkg.go.dev",
        "license": "BSD-3-Clause",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "node",
        "base_url": "https://nodejs.org/api",
        "license": "MIT",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "mdn",
        "base_url": "https://developer.mozilla.org",
        "license": "CC-BY-SA-2.5",
        "attribution_required": True,
        "robots_ttl": 86400,
        "rate_limit_rps": 0.5,
    },
    {
        "ecosystem": "ruby",
        "base_url": "https://ruby-doc.org",
        "license": "Ruby",
        "attribution_required": False,
        "robots_ttl": 86400,
        "rate_limit_rps": 1.0,
    },
    {
        "ecosystem": "cpp",
        "base_url": "https://en.cppreference.com",
        "license": "CC-BY-SA-3.0/GFDL",
        "attribution_required": True,
        "robots_ttl": 86400,
        "rate_limit_rps": 0.5,
    },
]


def _add_vector_column(table: str, bind_name: str) -> None:
    """Add a 768-dim pgvector column + ivfflat index on Postgres.

    Non-Postgres dialects (SQLite in tests/dev) get a nullable Text fallback
    instead -- vector search is disabled there, matching
    ``models_sqlalchemy.py:init_schema``'s existing degrade-gracefully path.
    """
    if bind_name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector(768)")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_emb_idx ON {table} "
            f"USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )
    else:
        op.add_column(table, sa.Column("embedding", sa.Text(), nullable=True))


def _add_fts_column(table: str, bind_name: str, expr: str) -> None:
    """Add a generated ``tsvector`` FTS column + GIN index on Postgres only."""
    if bind_name == "postgresql":
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS content_tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('english', {expr})) STORED"
        )
        op.execute(f"CREATE INDEX IF NOT EXISTS {table}_fts_idx ON {table} USING GIN (content_tsv)")


def upgrade() -> None:  # noqa: D103 -- alembic entrypoint, module docstring covers intent
    bind_name = op.get_bind().dialect.name

    # code_repos (§9.1)
    op.create_table(
        "code_repos",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer, nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(1024), nullable=False),
        # Reference into the provider-credential pattern; never a raw secret.
        sa.Column("credentials_ref", sa.String(255), nullable=True),
        sa.Column("index_status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("last_commit", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "name", name="uq_code_repos_org_name"),
    )

    # code_chunks (§9.1 + §9.7)
    op.create_table(
        "code_chunks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.Integer,
            sa.ForeignKey("code_repos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("symbol", sa.String(512), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),  # function|method|class|module|window
        sa.Column("start_line", sa.Integer, nullable=False),
        sa.Column("end_line", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
        # §9.7: chunks key on (repo, branch/commit) so parallel worktrees never
        # cross-contaminate.
        sa.Column("branch_ref", sa.String(255), nullable=False, server_default="main"),
        # §9.7 scope/trust/version/status/provenance model.
        sa.Column("scope_type", sa.String(20), nullable=False, server_default="repo"),
        sa.Column("scope_ref", sa.String(255), nullable=True),
        sa.Column("trust_tier", sa.String(20), nullable=False, server_default="derived"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("superseded_by", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_code_chunks_repo_branch", "code_chunks", ["repo_id", "branch_ref"])
    op.create_index(
        "idx_code_chunks_repo_branch_hash",
        "code_chunks",
        ["repo_id", "branch_ref", "content_hash"],
    )
    op.create_index("idx_code_chunks_status", "code_chunks", ["status"])
    _add_vector_column("code_chunks", bind_name)
    _add_fts_column("code_chunks", bind_name, "coalesce(content,'') || ' ' || coalesce(symbol,'')")

    # docs_sources (§9.2 + §2.5 per-source license table)
    op.create_table(
        "docs_sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ecosystem", sa.String(50), nullable=False, unique=True),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("license", sa.String(100), nullable=False),
        sa.Column("attribution_required", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("robots_ttl", sa.Integer, nullable=False, server_default="86400"),
        sa.Column("rate_limit_rps", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.bulk_insert(
        sa.table(
            "docs_sources",
            sa.column("ecosystem", sa.String),
            sa.column("base_url", sa.String),
            sa.column("license", sa.String),
            sa.column("attribution_required", sa.Boolean),
            sa.column("robots_ttl", sa.Integer),
            sa.column("rate_limit_rps", sa.Float),
        ),
        _DOCS_SOURCES_SEED,
    )

    # docs_cache_pages (§9.2). No org_id by design: cached content is public,
    # generically-licensed language documentation (docs.python.org, MDN, ...)
    # -- never org-private data -- so there is nothing to leak cross-org and a
    # shared cache is the point (one fetch serves every org). Contrast with
    # code_chunks/rag_documents/memory_embeddings, which hold org-private
    # content and are the tables the §9.8 org-isolation suite targets.
    op.create_table(
        "docs_cache_pages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ecosystem", sa.String(50), nullable=False, index=True),
        sa.Column("package", sa.String(255), nullable=True),
        sa.Column("version", sa.String(50), nullable=False, server_default="latest"),
        sa.Column("url", sa.String(1024), nullable=False),
        sa.Column("content_md", sa.Text, nullable=False),
        sa.Column("license", sa.String(100), nullable=True),
        sa.Column("attribution_required", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("fetched_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ttl", sa.Integer, nullable=False, server_default="2592000"),  # 30d default
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ecosystem", "package", "version", "url", name="uq_docs_cache_lookup"),
    )
    _add_vector_column("docs_cache_pages", bind_name)

    # rag_documents: fold in remaining §9.7 columns (org-scoped uploaded
    # knowledge, §9.3 -- scope_type/author_user_id are new here, unlike
    # memory_embeddings where they shipped in 006).
    op.add_column(
        "rag_documents",
        sa.Column("scope_type", sa.String(20), nullable=False, server_default="org"),
    )
    op.add_column("rag_documents", sa.Column("scope_ref", sa.String(255), nullable=True))
    op.add_column("rag_documents", sa.Column("author_user_id", sa.Integer, nullable=True))
    op.add_column(
        "rag_documents",
        sa.Column("trust_tier", sa.String(20), nullable=False, server_default="verified"),
    )
    op.add_column(
        "rag_documents", sa.Column("version", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("rag_documents", sa.Column("superseded_by", sa.Integer, nullable=True))
    op.add_column(
        "rag_documents", sa.Column("status", sa.String(20), nullable=False, server_default="active")
    )
    op.add_column("rag_documents", sa.Column("expires_at", sa.DateTime, nullable=True))
    op.add_column("rag_documents", sa.Column("provenance", sa.JSON, nullable=True))
    op.create_index("idx_ragdoc_org_scope", "rag_documents", ["organization_id", "scope_type"])
    op.create_index("idx_ragdoc_status", "rag_documents", ["status"])

    # memory_embeddings: extend the 006 column set. scope_type/author_user_id
    # already exist (006) -- do NOT re-add them here.
    op.add_column("memory_embeddings", sa.Column("scope_ref", sa.String(255), nullable=True))
    op.add_column(
        "memory_embeddings",
        sa.Column("trust_tier", sa.String(20), nullable=False, server_default="unverified"),
    )
    op.add_column(
        "memory_embeddings", sa.Column("version", sa.Integer, nullable=False, server_default="1")
    )
    op.add_column("memory_embeddings", sa.Column("superseded_by", sa.Integer, nullable=True))
    op.add_column(
        "memory_embeddings",
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
    )
    op.add_column("memory_embeddings", sa.Column("expires_at", sa.DateTime, nullable=True))
    op.add_column("memory_embeddings", sa.Column("provenance", sa.JSON, nullable=True))
    op.create_index("idx_mememb_status", "memory_embeddings", ["status"])


def downgrade() -> None:  # noqa: D103 -- alembic entrypoint, module docstring covers intent
    op.drop_index("idx_mememb_status", table_name="memory_embeddings")
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.drop_column("provenance")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("status")
        batch_op.drop_column("superseded_by")
        batch_op.drop_column("version")
        batch_op.drop_column("trust_tier")
        batch_op.drop_column("scope_ref")

    op.drop_index("idx_ragdoc_status", table_name="rag_documents")
    op.drop_index("idx_ragdoc_org_scope", table_name="rag_documents")
    with op.batch_alter_table("rag_documents") as batch_op:
        batch_op.drop_column("provenance")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("status")
        batch_op.drop_column("superseded_by")
        batch_op.drop_column("version")
        batch_op.drop_column("trust_tier")
        batch_op.drop_column("author_user_id")
        batch_op.drop_column("scope_ref")
        batch_op.drop_column("scope_type")

    op.drop_table("docs_cache_pages")
    op.drop_table("docs_sources")

    op.drop_index("idx_code_chunks_status", table_name="code_chunks")
    op.drop_index("idx_code_chunks_repo_branch_hash", table_name="code_chunks")
    op.drop_index("idx_code_chunks_repo_branch", table_name="code_chunks")
    op.drop_table("code_chunks")

    op.drop_table("code_repos")
