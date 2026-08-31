# CodeRAG Core Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish WaddleAI's first-party CodeRAG (§9.1) so it works end-to-end in production: a real pgvector+FTS `CodeSearchBackend` scoped to `(org, repo, branch)` **in SQL** (not just the post-fetch Python filter), and full wiring from a management repo-registration API + webhook/cron triggers through to the proxy's auto-inject pipeline and the `/mcp` `search_code`/`get_symbol` tools.

**Architecture:** `shared/knowledge/code_search.py` already defines the `CodeSearchBackend` Protocol (symbol-exact short-circuit, RRF-fused vector+FTS) and `shared/knowledge/scoping.py` already defines the Python-level `(org, repo, branch)` visibility filter — both stay. This plan (1) widens `CodeSearchBackend`'s Protocol methods to accept the caller's `ScopeKey` so a real implementation can push `org_id`/`repo_id`/`branch_ref` into its SQL `WHERE` clause (`scoping.filter_visible` remains as defense-in-depth, never the primary boundary), (2) implements that real backend against Postgres pgvector (`<=>` cosine operator, `ivfflat` index) + FTS (`tsvector`/`plainto_tsquery`/GIN index) via raw parameterized SQL through `penguin_dal`'s `db.executesql()` — the exact pattern already proven in `shared/utils/memory_integration.py::PgvectorMemoryStore`, and (3) wires that backend into every consumer: the management `code_repos` registration/webhook/cron REST API, the proxy `KnowledgeInjectStage`'s `KnowledgeRetriever.sources["code"]`, and the `/mcp` `search_code`/`get_symbol` tools (replacing `NotWiredKnowledgeService`).

**Tech Stack:** Python 3.13, Quart + hypercorn, `penguin_dal` (raw `executesql` for pgvector/FTS — PyDAL query builder does not speak `vector`/`tsvector` operators), PostgreSQL 17 + pgvector (`vector(768)`, `ivfflat` cosine) + native FTS (`tsvector`/GIN, `plainto_tsquery`), Alembic (schema), `shared.security.credential_encryption` (Fernet, `enc:` prefix) for the webhook shared secret, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-09-waddleai-platform-spec.md` §9.1 (CodeRAG) and §9.7 (scope/trust/isolation model); prior scaffolding plan `docs/superpowers/plans/2026-07-09-wave3-knowledge-layer.md` Task 6 (chunker), Task 7 (`code_search.py`, built the Protocol this plan widens).

## Global Constraints

- **Postgres + pgvector only** — no Neo4j, no graph database, no graph extension of any kind. Vector search and FTS both run as plain SQL against `code_chunks`/`code_repos`.
- **penguin-dal for all runtime DB ops** — `db.executesql()` (raw parameterized SQL) for the vector/FTS queries, PyDAL query builder only where it already speaks the schema natively (nowhere in this plan, per the design decision in Task 4).
- **`@dataclass(slots=True)` for every new data structure** — no exceptions.
- **Async only** — every new function is `async def`; blocking calls (`db.executesql`, git/file IO already in `coderag_worker.py`) go through `asyncio.to_thread`.
- **90% coverage gate** — every new module ships a matching test file exercising happy path, org/repo/branch isolation, and flag-off no-op.
- **Embed/helper models ≥2B parameters** — no change needed here (the existing `nomic-embed-text` 768-dim default already satisfies this; do not substitute a smaller model).
- **Tenant/org isolation enforced IN SQL** — every new query's `WHERE` clause carries `org_id` unconditionally; `repo_id`/`branch_ref` are added when the caller's scope specifies them. An unresolvable repo name must return empty results, never silently widen to an org-wide search.
- **Docstrings on every new class/function** (PEP 257, 2-3 lines: what + why).
- **Feature-gated**: every new code path (API routes, webhook, MCP tools, proxy auto-inject) stays behind `waddleai.coderag` (fail-safe OFF on flag-evaluation error, matching the existing `_coderag_enabled()` pattern in `coderag_worker.py`). Likely Enterprise-tier at the license layer — no license-gating code changes in this plan (out of scope; flag-gating only).
- **OIDC scopes, never role names** — the new REST routes gate on a new `Permission.CODE_REPO_WRITE` scope (mirroring the existing `Permission.KNOWLEDGE_WRITE` admin+resource_manager tier), never `role == "admin"` checks.
- **No raw secrets in DB** — the webhook shared secret is Fernet-encrypted via `shared.security.credential_encryption` before storage, shown to the caller exactly once at creation.
- **Follow established patterns, don't restructure** — new route file mirrors `services/management/app/api/v1/knowledge.py`'s shape exactly (manual Quart routes + `jsonify`, explicit `_serialize()` DTO, `require_auth`/`require_scope`, IDOR-safe 404-outside-org); new SQL backend mirrors `shared/utils/memory_integration.py::PgvectorMemoryStore`'s raw-`executesql` style exactly.

---

## File Structure

| Action | File | Responsibility |
|---|---|---|
| Create | `services/management/alembic/versions/019_code_repos_webhook_secret.py` | Migration: `code_repos.webhook_secret` column (down-rev `018_model_access_policies`) |
| Create | `tests/unit/management/test_migration_019.py` | Round-trip test for migration 019 |
| Modify | `services/management/app/models_sqlalchemy.py` | Add `CodeRepo.webhook_secret` column |
| Modify | `shared/auth/rbac.py` | Mint `Permission.CODE_REPO_WRITE`; add to `Role.ADMIN`/`Role.RESOURCE_MANAGER` bundles |
| Create | `tests/unit/test_code_repo_write_scope.py` | `CODE_REPO_WRITE` scope + tier test |
| Modify | `shared/knowledge/code_search.py` | Widen `CodeSearchBackend` Protocol + `search_code()` orchestration to thread `scope: ScopeKey` into `vector_search`/`fts_search`/`fetch_records` |
| Modify | `tests/unit/knowledge/test_code_search.py` | Update `_StubBackend.fetch_records` signature; add repo-vs-repo-same-org isolation test; add scope-threading test |
| Create | `shared/knowledge/coderag_backend.py` | `PgCodeSearchBackend` (real pgvector+FTS SQL, SQL-scoped), `CodeKnowledgeSourceAdapter`, `build_code_knowledge_sources()` |
| Create | `tests/unit/knowledge/test_coderag_backend.py` | SQL-scoping proof tests (assert `WHERE` clause + params, not just Python filtering) |
| Create | `shared/mcp/knowledge_adapter.py` | `CodeRagKnowledgeService` (real `search_code`/`get_symbol`, subclasses `NotWiredKnowledgeService` so `search_docs`/`fetch_docs` stay honestly not-wired — separate subsystem, out of scope) |
| Create | `tests/unit/mcp/test_knowledge_adapter.py` | Adapter tests (org-scoped, symbol lookup, none-found) |
| Modify | `proxy/apps/proxy_server/main.py` | Replace `sources={}` with `build_code_knowledge_sources(self.db)`; pass a real `service_factory` to `MCPMount(...)` |
| Create | `tests/unit/proxy/test_coderag_wiring.py` | Proves `main.py` no longer wires the no-op `sources={}` / `NotWiredKnowledgeService` for CodeRAG |
| Create | `services/management/app/api/v1/code_repos.py` | `/api/v1/code-repos` CRUD + `/reindex` + `/reindex-all` (cron) + `/webhook` (HMAC-verified GitHub/Gitea push) |
| Create | `tests/unit/management/test_code_repos_api.py` | Route tests: org isolation/IDOR, flag-gated 404, scope enforcement, webhook signature verification |
| Modify | `services/management/app/api/v1/__init__.py` | Append `code_repos` to the route-module import block |
| Modify | `tests/unit/management/conftest.py` | Add `services.management.app.api.v1.code_repos` to `ROUTE_MODULES` |
| Modify | `tests/unit/management/test_scope_authz.py` | Add `Permission.CODE_REPO_WRITE` to `_B_TIER_SCOPES`/`_MIGRATED_SCOPES`; bump `_EXPECTED_ROUTE_COUNT` by 4 |
| Modify | `openapi/v1.yaml` | Regenerated via `make generate-openapi` after the new routes land |
| Modify | `tests/integration/test_knowledge_acceptance.py` | End-to-end: SQL-scoping proof at the acceptance layer + flag-on wiring smoke |

---

### Task 1: Migration 019 — `code_repos.webhook_secret`

**Files:**
- Create: `services/management/alembic/versions/019_code_repos_webhook_secret.py`
- Modify: `services/management/app/models_sqlalchemy.py:648-663` (`CodeRepo` class)
- Test: `tests/unit/management/test_migration_019.py`

**Interfaces:**
- Produces: `code_repos.webhook_secret` column (nullable `String(512)`, Fernet-encrypted `enc:`-prefixed value), consumed by Task 9's webhook route.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/unit/management/test_migration_019.py
"""Migration 019 round-trip test: ``webhook_secret`` column on ``code_repos``.

Same technique as ``test_migration_017.py`` -- ``code_repos`` is an
Alembic-created table (migration 012), not a ``create_all()`` table, so the
scratch DB pre-creates the post-012 shape by hand and exercises
``upgrade()``/``downgrade()`` via a direct ``Operations`` context.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    "..",
    "services",
    "management",
    "alembic",
    "versions",
    "019_code_repos_webhook_secret.py",
)


def _load_migration_019():
    """Import ``019_code_repos_webhook_secret.py`` by path (filename isn't an identifier)."""
    spec = importlib.util.spec_from_file_location(
        "migration_019_code_repos_webhook_secret", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pre_019_shape(conn: sa.Connection) -> None:
    """Create the post-012 shape of ``code_repos`` (no ``webhook_secret`` yet)."""
    conn.execute(
        sa.text(
            "CREATE TABLE code_repos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "org_id INTEGER NOT NULL, "
            "name VARCHAR(255) NOT NULL, "
            "source_url VARCHAR(1024) NOT NULL, "
            "credentials_ref VARCHAR(255), "
            "index_status VARCHAR(50) NOT NULL DEFAULT 'pending', "
            "last_commit VARCHAR(64))"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO code_repos (org_id, name, source_url) "
            "VALUES (7, 'waddleai', 'https://github.com/penguintechinc/waddleai.git')"
        )
    )


@pytest.fixture
def scratch_db(tmp_path):
    """A scratch SQLite DB pre-loaded with the pre-019 ``code_repos`` shape."""
    db_path = tmp_path / "migration019.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _pre_019_shape(conn)
    yield engine
    engine.dispose()


def test_revision_metadata_chains_off_018() -> None:
    """Revision id and down_revision match the current chain head."""
    module = _load_migration_019()
    assert module.revision == "019_code_repos_webhook_secret"
    assert module.down_revision == "018_model_access_policies"


def test_upgrade_adds_webhook_secret_column(scratch_db) -> None:
    """upgrade() adds ``webhook_secret``, nullable, existing rows default to NULL."""
    module = _load_migration_019()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(code_repos)"))}
        assert "webhook_secret" in cols

        row = conn.execute(
            sa.text("SELECT name, webhook_secret FROM code_repos WHERE name = 'waddleai'")
        ).one()
        assert row.name == "waddleai"
        assert row.webhook_secret is None


def test_downgrade_drops_webhook_secret_column(scratch_db) -> None:
    """downgrade() restores the pre-019 shape, preserving other data."""
    module = _load_migration_019()
    with scratch_db.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        conn.commit()
        with Operations.context(ctx):
            module.downgrade()
        conn.commit()

        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(code_repos)"))}
        assert "webhook_secret" not in cols

        row = conn.execute(
            sa.text("SELECT name FROM code_repos WHERE name = 'waddleai'")
        ).one()
        assert row.name == "waddleai"


def test_alembic_chain_still_single_head_after_019() -> None:
    """Adding 019 keeps a single resolvable head, no divergent branches."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.abspath(os.path.join(os.path.dirname(MIGRATION_PATH), "..")),
    )
    cfg.set_main_option("sqlalchemy.url", "sqlite://")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    assert len(heads) == 1
    assert heads[0] == "019_code_repos_webhook_secret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_019.py -v`
Expected: FAIL with `FileNotFoundError`/`ModuleNotFoundError` (the migration file doesn't exist yet).

- [ ] **Step 3: Write the migration**

```python
# services/management/alembic/versions/019_code_repos_webhook_secret.py
"""Webhook secret for CodeRAG repo registration (§9.1 core-completion).

Adds ``code_repos.webhook_secret`` -- a Fernet-encrypted (``enc:`` prefix via
``shared.security.credential_encryption``) shared secret generated at
repo-registration time and returned to the caller exactly once. The push
webhook route (``services/management/app/api/v1/code_repos.py``) verifies
the inbound GitHub/Gitea ``X-Hub-Signature-256`` HMAC against the decrypted
value before trusting the payload -- same encrypt-before-store pattern
already used for ``provider_credentials.api_key``, never a raw plaintext
secret column.

Revision ID: 019_code_repos_webhook_secret
Revises: 018_model_access_policies
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019_code_repos_webhook_secret"
down_revision: str | None = "018_model_access_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``code_repos.webhook_secret`` (nullable, Fernet-encrypted at the app layer)."""
    op.add_column("code_repos", sa.Column("webhook_secret", sa.String(512), nullable=True))


def downgrade() -> None:
    """Drop ``code_repos.webhook_secret``."""
    with op.batch_alter_table("code_repos") as batch_op:
        batch_op.drop_column("webhook_secret")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/management/test_migration_019.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Update the SQLAlchemy model**

In `services/management/app/models_sqlalchemy.py`, inside the `CodeRepo` class (currently lines 648-663), add the column between `credentials_ref` and `index_status`:

```python
    credentials_ref = Column(String(255), nullable=True)  # provider-credential pattern
    webhook_secret = Column(String(512), nullable=True)  # Fernet-encrypted, enc: prefix
    index_status = Column(String(50), nullable=False, default="pending", server_default="pending")
```

- [ ] **Step 6: Commit**

```bash
git add services/management/alembic/versions/019_code_repos_webhook_secret.py \
        tests/unit/management/test_migration_019.py \
        services/management/app/models_sqlalchemy.py
git commit -m "feat(coderag): add code_repos.webhook_secret column (migration 019)"
```

---

### Task 2: `Permission.CODE_REPO_WRITE` scope

**Files:**
- Modify: `shared/auth/rbac.py:88` (enum), `:173` (`Role.ADMIN` bundle), `:220` (`Role.RESOURCE_MANAGER` bundle)
- Test: `tests/unit/test_code_repo_write_scope.py`

**Interfaces:**
- Produces: `Permission.CODE_REPO_WRITE` (value `"code_repo:write"`), consumed by Task 9's routes and Task 10's `test_scope_authz.py` updates.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_code_repo_write_scope.py
"""CODE_REPO_WRITE scope: minted for the CodeRAG repo-registration API (§9.1 core-completion).

Mirrors KNOWLEDGE_WRITE's tier exactly -- admin + resource_manager only,
never reporter/user.
"""

from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role


def test_code_repo_write_scope_exists() -> None:
    """The scope value matches the house resource:action convention."""
    assert Permission.CODE_REPO_WRITE.value == "code_repo:write"


def test_code_repo_write_held_by_admin_and_resource_manager_only() -> None:
    """Exactly admin + resource_manager hold CODE_REPO_WRITE -- same tier as KNOWLEDGE_WRITE."""
    holders = {role for role, perms in ROLE_PERMISSIONS.items() if Permission.CODE_REPO_WRITE in perms}
    assert holders == {Role.ADMIN, Role.RESOURCE_MANAGER}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_code_repo_write_scope.py -v`
Expected: FAIL with `AttributeError: CODE_REPO_WRITE`

- [ ] **Step 3: Mint the scope**

In `shared/auth/rbac.py`, in the `Permission` enum's "Admin + resource_manager" tier block (next to `KNOWLEDGE_WRITE = "knowledge:write"` at line 88):

```python
    KNOWLEDGE_WRITE = "knowledge:write"
    CODE_REPO_WRITE = "code_repo:write"
```

In `ROLE_PERMISSIONS[Role.ADMIN]` (next to `Permission.KNOWLEDGE_WRITE` at line 173):

```python
        Permission.KNOWLEDGE_WRITE,
        Permission.CODE_REPO_WRITE,
```

In `ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]` (next to `Permission.KNOWLEDGE_WRITE` at line 220):

```python
        Permission.KNOWLEDGE_WRITE,
        Permission.CODE_REPO_WRITE,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_code_repo_write_scope.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/auth/rbac.py tests/unit/test_code_repo_write_scope.py
git commit -m "feat(coderag): mint CODE_REPO_WRITE scope for the repo-registration API"
```

---

### Task 3: Thread `scope: ScopeKey` through `CodeSearchBackend`

**Files:**
- Modify: `shared/knowledge/code_search.py` (Protocol at lines 48-69, orchestration at lines 86-137)
- Test: `tests/unit/knowledge/test_code_search.py`

**Interfaces:**
- Consumes: `shared.knowledge.scoping.ScopeKey` (existing).
- Produces: `CodeSearchBackend.vector_search(query_embedding, scope, top_k)`, `.fts_search(query_text, scope, top_k)`, `.fetch_records(chunk_ids, scope)` — all now take `scope: ScopeKey` as their 2nd positional parameter. `symbol_exact(query_text, scope)` is unchanged (already took scope). Consumed by Task 4's `PgCodeSearchBackend`.

This is the fix for the audit finding: today only `symbol_exact` receives the caller's scope, so no real backend implementation could push `org_id`/`repo_id`/`branch_ref` into `vector_search`/`fts_search`/`fetch_records`'s SQL — isolation was only ever a post-fetch Python filter (`scoping.filter_visible`, kept below as defense-in-depth).

- [ ] **Step 1: Add the repo-vs-repo-same-org isolation test**

This closes the audit-identified test gap. It documents behavior `scoping.is_visible()` already implements correctly at the Python-filter layer (parallel to the existing `TestBranchIsolation`/`TestOrgIsolation` classes) — it passes today, before any code change in this task; keep it as regression coverage going forward.

Add to `tests/unit/knowledge/test_code_search.py`, after `class TestOrgIsolation`:

```python
class TestRepoIsolation:
    """(f) Repo isolation (security): repo-1's caller never receives repo-2's chunks, same org."""

    @pytest.mark.asyncio
    async def test_other_repo_chunk_excluded_even_if_top_ranked(self) -> None:
        """A semantically-closer chunk from a different repo in the same org is dropped."""
        own_repo_chunk = _chunk(id="own", repo="repo-1", content="repo 1 code")
        other_repo_chunk = _chunk(id="other", repo="repo-2", content="repo 2 code")
        backend = _StubBackend(
            vector_ranked=["other", "own"],  # other-repo chunk ranks HIGHER
            fts_ranked=["other", "own"],
            records={"own": own_repo_chunk, "other": other_repo_chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        results = await search_code("code", caller, backend, embed_db=None)

        chunk_ids = {r.chunk_id for r in results}
        assert chunk_ids == {"own"}
```

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_code_search.py::TestRepoIsolation -v`
Expected: PASS already (documents existing Python-filter behavior; not a regression risk from this task).

- [ ] **Step 2: Write the failing scope-threading test**

Add to `tests/unit/knowledge/test_code_search.py`:

```python
class TestBackendReceivesCallerScope:
    """(g) The orchestration threads the caller's scope into every backend call.

    A real SQL backend can only push org/repo/branch into its WHERE clause if
    search_code() actually hands it the scope -- this proves the plumbing,
    independent of any specific backend implementation.
    """

    @pytest.mark.asyncio
    async def test_fetch_records_receives_the_caller_scope(self) -> None:
        """fetch_records is called with (chunk_ids, caller) -- not chunk_ids alone."""
        chunk = _chunk(id="a", symbol="alpha")
        backend = _StubBackend(
            vector_ranked=["a"],
            fts_ranked=["a"],
            records={"a": chunk},
        )
        caller = ScopeKey(org="org-a", repo="repo-1", branch="main")

        received_scopes: list[object] = []
        original_fetch_records = backend.fetch_records

        async def _spy_fetch_records(chunk_ids, scope):
            received_scopes.append(scope)
            return await original_fetch_records(chunk_ids, scope)

        backend.fetch_records = _spy_fetch_records

        await search_code("query", caller, backend, embed_db=None)

        assert received_scopes == [caller]
```

Also update `_StubBackend.fetch_records`'s signature (test infrastructure, not the system under test) so the spy above can even be defined against it:

```python
    async def fetch_records(self, chunk_ids: list[str], scope: ScopeKey) -> dict[str, CodeChunkRecord]:
        return {cid: self.records[cid] for cid in chunk_ids if cid in self.records}
```

Run: `.venv/bin/python -m pytest tests/unit/knowledge/ -v`
Expected: FAIL — every test in `test_code_search.py` that reaches `fetch_records` now errors with `TypeError: fetch_records() missing 1 required positional argument: 'scope'`, because `search_code()`'s orchestration still calls `backend.fetch_records(candidate_ids)` with one argument. This is the real, honest RED state for this signature-widening refactor.

- [ ] **Step 3: Widen the Protocol and thread scope through the orchestration**

In `shared/knowledge/code_search.py`, replace the `CodeSearchBackend` Protocol:

```python
class CodeSearchBackend(Protocol):
    """The DB-facing seam ``search_code`` calls through.

    Swap for a real pgvector+FTS implementation in production, a stub in
    tests. Every method takes the caller's ``ScopeKey`` so a real
    implementation can push org/repo/branch scoping into its SQL WHERE
    clause -- ``scoping.filter_visible`` in ``search_code()`` below is
    defense-in-depth, never the sole isolation boundary.
    """

    async def vector_search(
        self, query_embedding: list[float], scope: ScopeKey, top_k: int
    ) -> list[str]:
        """Return chunk_ids ranked by cosine similarity, best first, scoped to ``scope``."""
        ...

    async def fts_search(self, query_text: str, scope: ScopeKey, top_k: int) -> list[str]:
        """Return chunk_ids ranked by Postgres ``ts_rank``, best first, scoped to ``scope``."""
        ...

    async def symbol_exact(self, query_text: str, scope: ScopeKey) -> CodeChunkRecord | None:
        """Return a record whose symbol exactly matches ``query_text``, if any, scoped to ``scope``."""
        ...

    async def fetch_records(
        self, chunk_ids: list[str], scope: ScopeKey
    ) -> dict[str, CodeChunkRecord]:
        """Resolve chunk_ids to their full CodeChunkRecord, scoped to ``scope``."""
        ...
```

And update `search_code()`'s body:

```python
    exact = await backend.symbol_exact(query, caller)

    query_embedding = await embed_cached(query, db=embed_db)
    vector_ranked = await backend.vector_search(query_embedding, caller, top_k=top_k * 2)
    fts_ranked = await backend.fts_search(query, caller, top_k=top_k * 2)

    fused_scores = reciprocal_rank_fusion([vector_ranked, fts_ranked])
    candidate_ids = [cid for cid in fused_scores if not exact or cid != exact.id]

    records = await backend.fetch_records(candidate_ids, caller)
```

- [ ] **Step 4: Run the full knowledge test suite to verify everything passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/ -v`
Expected: PASS (all tests, including the new `TestRepoIsolation` and `TestBackendReceivesCallerScope` classes)

- [ ] **Step 5: Commit**

```bash
git add shared/knowledge/code_search.py tests/unit/knowledge/test_code_search.py
git commit -m "feat(coderag): thread caller scope through CodeSearchBackend for SQL-level isolation"
```

---

### Task 4: `PgCodeSearchBackend` — the real pgvector+FTS SQL backend

**Files:**
- Create: `shared/knowledge/coderag_backend.py`
- Test: `tests/unit/knowledge/test_coderag_backend.py`

**Interfaces:**
- Consumes: `shared.knowledge.code_search.CodeSearchBackend`/`CodeChunkRecord` (Task 3), `shared.knowledge.scoping.ScopeKey`/`ScopeType`/`TrustTier`.
- Produces: `PgCodeSearchBackend(db: object)`, consumed by Task 5 and Task 6.

Every method uses raw parameterized SQL via `db.executesql(sql, params)` — the exact style already proven in `shared/utils/memory_integration.py::PgvectorMemoryStore` (`embedding <=> %s::vector` cosine distance, `%s` placeholders, `# nosec B608` on the f-string line since every value is bound through `executesql`'s params, never string-interpolated). Repo-name resolution also goes through `executesql` (not the PyDAL query builder) so the whole class has one, consistently-testable DB access pattern.

- [ ] **Step 1: Write the failing test for org-scoped repo resolution + vector_search**

```python
# tests/unit/knowledge/test_coderag_backend.py
"""Tests for shared.knowledge.coderag_backend.PgCodeSearchBackend.

SQL-scoping is a **security** property (§9.7): every query's WHERE clause
must filter by org_id (and repo_id/branch_ref when the caller specifies
them), not rely solely on the post-fetch Python filter in
shared.knowledge.scoping. These tests capture the SQL string + params
through a fake DAL (same technique as
tests/unit/test_memory_scope_pgvector.py) and assert the WHERE clause
literally contains the scoping filters -- proving the SQL itself is scoped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from shared.knowledge.coderag_backend import PgCodeSearchBackend
from shared.knowledge.scoping import ScopeKey, ScopeType, TrustTier


class FakeDB:
    """Captures executesql calls; returns queued results, one batch per call in order."""

    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._results = results or []

    def executesql(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, tuple(params) if params else ()))
        return self._results.pop(0) if self._results else []


@pytest.mark.asyncio
async def test_vector_search_where_clause_scopes_by_org_and_repo() -> None:
    """vector_search's SQL WHERE clause filters by org_id and repo_id, not just Python."""
    db = FakeDB(
        results=[
            [(42,)],  # repo-name -> id resolution
            [("chunk-9",), ("chunk-3",)],  # vector_search rows
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == ["chunk-9", "chunk-3"]
    resolve_sql, resolve_params = db.calls[0]
    assert "code_repos" in resolve_sql
    assert resolve_params == (7, "waddleai")
    search_sql, search_params = db.calls[1]
    assert "r.org_id = %s" in search_sql
    assert "c.repo_id = %s" in search_sql
    assert "c.branch_ref = %s" in search_sql
    assert search_params[0:3] == (7, 42, "main")


@pytest.mark.asyncio
async def test_vector_search_unknown_repo_returns_empty_never_org_wide() -> None:
    """A typo'd repo name must return empty, never silently search the whole org."""
    db = FakeDB(results=[[]])  # repo-name resolution finds nothing
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="typo-repo", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == []
    assert len(db.calls) == 1  # the search query was never issued at all


@pytest.mark.asyncio
async def test_vector_search_no_repo_scopes_to_org_only() -> None:
    """caller.repo=None searches every repo in the org -- the org filter is still mandatory."""
    db = FakeDB(results=[[("chunk-1",)]])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", branch="main")

    chunk_ids = await backend.vector_search([0.1] * 768, scope, top_k=5)

    assert chunk_ids == ["chunk-1"]
    search_sql, search_params = db.calls[0]
    assert "r.org_id = %s" in search_sql
    assert "c.repo_id = %s" not in search_sql
    assert search_params[0] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.knowledge.coderag_backend'`

- [ ] **Step 3: Implement repo resolution + `vector_search`**

```python
# shared/knowledge/coderag_backend.py
"""The real production CodeSearchBackend (§9.1 core-completion): pgvector + Postgres FTS.

Implements shared.knowledge.code_search.CodeSearchBackend against
PostgreSQL's pgvector (`<=>` cosine distance, `code_chunks_emb_idx`
ivfflat index) and native FTS (`content_tsv` generated tsvector column,
`code_chunks_fts_idx` GIN index, both from migration 012). Every query's
WHERE clause is scoped to the caller's (org, repo, branch) directly in SQL
-- not just via the post-fetch shared.knowledge.scoping.filter_visible
Python filter, which stays as defense-in-depth only.

Raw parameterized SQL via db.executesql() throughout (never the PyDAL query
builder, which has no vector/tsvector operator support) -- the same style
already proven in shared/utils/memory_integration.py::PgvectorMemoryStore.
"""

from __future__ import annotations

import asyncio

from shared.knowledge.code_search import CodeChunkRecord
from shared.knowledge.scoping import ScopeKey, ScopeType, TrustTier

_RECORD_COLUMNS = (
    "c.id, c.path, c.symbol, c.kind, c.content, c.branch_ref, "
    "c.scope_type, c.scope_ref, c.trust_tier, c.status, c.created_at, "
    "r.org_id, r.name"
)


def _build_scope_where(org_id: int, repo_id: int | None, branch_ref: str | None) -> tuple[str, list]:
    """Build the mandatory org filter plus optional repo/branch filters for a code_chunks query.

    org_id is always present; repo_id/branch_ref are added only when the
    caller's scope specifies them, mirroring scoping.is_visible()'s
    permissive-when-unspecified semantics so the SQL-level filter and the
    Python defense-in-depth filter never disagree.
    """
    clauses = ["r.org_id = %s"]
    params: list = [org_id]
    if repo_id is not None:
        clauses.append("c.repo_id = %s")
        params.append(repo_id)
    if branch_ref is not None:
        clauses.append("c.branch_ref = %s")
        params.append(branch_ref)
    return " AND ".join(clauses), params


def _row_to_record(row: tuple) -> CodeChunkRecord:
    """Build a CodeChunkRecord from one _RECORD_COLUMNS row."""
    (
        chunk_id,
        path,
        symbol,
        kind,
        content,
        branch_ref,
        scope_type,
        scope_ref,
        trust_tier,
        status,
        created_at,
        org_id,
        repo_name,
    ) = row
    return CodeChunkRecord(
        id=str(chunk_id),
        content=content,
        scope_type=ScopeType(scope_type),
        scope_ref=scope_ref,
        trust_tier=TrustTier(trust_tier),
        author_user_id=None,  # code_chunks has no author column -- AST-derived, not user-authored
        org=str(org_id),
        repo=repo_name,
        branch=branch_ref,
        status=status,
        created_at=created_at,
        path=path,
        symbol=symbol,
        kind=kind,
    )


class PgCodeSearchBackend:
    """Real CodeSearchBackend: pgvector cosine + Postgres FTS, org/repo/branch-scoped in SQL."""

    def __init__(self, db: object) -> None:
        """Bind to a penguin-dal handle exposing ``executesql`` (management or proxy's ``db``)."""
        self.db = db

    async def _resolve_scope(self, scope: ScopeKey) -> tuple[int, int | None, bool]:
        """Resolve (org_id, repo_id, repo_requested_but_missing) from a ScopeKey.

        repo_requested_but_missing is True only when scope.repo was given
        but no such repo exists in this org -- callers must short-circuit to
        empty results in that case, never fall back to an org-wide search.
        """
        org_id = int(scope.org)
        if scope.repo is None:
            return org_id, None, False
        rows = await asyncio.to_thread(
            self.db.executesql,
            "SELECT id FROM code_repos WHERE org_id = %s AND name = %s LIMIT 1",  # nosec B608 -- fixed literal, values bound via executesql params
            [org_id, scope.repo],
        )
        if not rows:
            return org_id, None, True
        return org_id, int(rows[0][0]), False

    async def vector_search(self, query_embedding: list[float], scope: ScopeKey, top_k: int) -> list[str]:
        """Return chunk_ids ranked by pgvector cosine similarity, scoped to org/repo/branch."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return []
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        embedding_str = "[" + ",".join(str(f) for f in query_embedding) + "]"
        sql = (
            "SELECT c.id FROM code_chunks c "  # nosec B608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' AND c.embedding IS NOT NULL "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s"
        )
        params = [*where_params, embedding_str, top_k]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return [str(row[0]) for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for `fts_search`**

Add to `tests/unit/knowledge/test_coderag_backend.py`:

```python
@pytest.mark.asyncio
async def test_fts_search_uses_plainto_tsquery_and_scopes_by_org() -> None:
    """fts_search ranks by ts_rank over the generated content_tsv column, org-scoped."""
    db = FakeDB(results=[[("chunk-5",)]])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7")

    chunk_ids = await backend.fts_search("handle_request", scope, top_k=5)

    assert chunk_ids == ["chunk-5"]
    sql, params = db.calls[0]
    assert "plainto_tsquery" in sql
    assert "ts_rank" in sql
    assert "r.org_id = %s" in sql
    assert params[0] == 7
    assert "handle_request" in params
```

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py::test_fts_search_uses_plainto_tsquery_and_scopes_by_org -v`
Expected: FAIL with `AttributeError: 'PgCodeSearchBackend' object has no attribute 'fts_search'`

- [ ] **Step 6: Implement `fts_search`**

Add to the `PgCodeSearchBackend` class in `shared/knowledge/coderag_backend.py`:

```python
    async def fts_search(self, query_text: str, scope: ScopeKey, top_k: int) -> list[str]:
        """Return chunk_ids ranked by Postgres ts_rank over content_tsv, scoped to org/repo/branch."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return []
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        sql = (
            "SELECT c.id FROM code_chunks c "  # nosec B608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' "
            "AND c.content_tsv @@ plainto_tsquery('english', %s) "
            "ORDER BY ts_rank(c.content_tsv, plainto_tsquery('english', %s)) DESC LIMIT %s"
        )
        params = [*where_params, query_text, query_text, top_k]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return [str(row[0]) for row in rows]
```

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Write the failing test for `symbol_exact`**

Add to `tests/unit/knowledge/test_coderag_backend.py`:

```python
@pytest.mark.asyncio
async def test_symbol_exact_builds_a_scoped_record() -> None:
    """symbol_exact resolves the full record, scoped by org/repo, org_id/name from the join."""
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = FakeDB(
        results=[
            [(42,)],  # repo resolution
            [
                (
                    "9",
                    "billing.py",
                    "calculate_total",
                    "function",
                    "def calculate_total(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                )
            ],
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    record = await backend.symbol_exact("calculate_total", scope)

    assert record is not None
    assert record.id == "9"
    assert record.symbol == "calculate_total"
    assert record.org == "7"
    assert record.repo == "waddleai"
    assert record.trust_tier == TrustTier.DERIVED
    sql, params = db.calls[1]
    assert "c.symbol = %s" in sql
    assert "r.org_id = %s" in sql


@pytest.mark.asyncio
async def test_symbol_exact_no_match_returns_none() -> None:
    """No matching symbol resolves to None, not an empty record."""
    db = FakeDB(results=[[(42,)], []])
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai")

    record = await backend.symbol_exact("does_not_exist", scope)

    assert record is None
```

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -k symbol_exact -v`
Expected: FAIL with `AttributeError: 'PgCodeSearchBackend' object has no attribute 'symbol_exact'`

- [ ] **Step 9: Implement `symbol_exact`**

```python
    async def symbol_exact(self, query_text: str, scope: ScopeKey) -> CodeChunkRecord | None:
        """Return the record whose symbol exactly matches query_text, scoped to org/repo/branch."""
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return None
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        sql = (
            f"SELECT {_RECORD_COLUMNS} FROM code_chunks c "  # nosec B608 -- fixed literal fragments, values bound via executesql params
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.status = 'active' AND c.symbol = %s LIMIT 1"
        )
        params = [*where_params, query_text]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return _row_to_record(rows[0]) if rows else None
```

- [ ] **Step 10: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: PASS (6 tests)

- [ ] **Step 11: Write the failing test for `fetch_records`**

Add to `tests/unit/knowledge/test_coderag_backend.py`:

```python
@pytest.mark.asyncio
async def test_fetch_records_scopes_by_org_and_repo_with_in_clause() -> None:
    """fetch_records resolves multiple ids in one scoped IN (...) query."""
    created = datetime(2026, 8, 31, 12, 0, 0)
    db = FakeDB(
        results=[
            [(42,)],  # repo resolution
            [
                (
                    "1",
                    "a.py",
                    "alpha",
                    "function",
                    "def alpha(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                ),
                (
                    "2",
                    "b.py",
                    "beta",
                    "function",
                    "def beta(): ...",
                    "main",
                    "repo",
                    "waddleai",
                    "derived",
                    "active",
                    created,
                    7,
                    "waddleai",
                ),
            ],
        ]
    )
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7", repo="waddleai", branch="main")

    records = await backend.fetch_records(["1", "2"], scope)

    assert set(records) == {"1", "2"}
    assert records["1"].symbol == "alpha"
    assert records["2"].symbol == "beta"
    sql, params = db.calls[1]
    assert "IN (%s, %s)" in sql
    assert "r.org_id = %s" in sql
    assert params[-2:] == (1, 2)


@pytest.mark.asyncio
async def test_fetch_records_empty_ids_never_queries() -> None:
    """An empty chunk_ids list short-circuits without issuing a query."""
    db = FakeDB()
    backend = PgCodeSearchBackend(db)
    scope = ScopeKey(org="7")

    records = await backend.fetch_records([], scope)

    assert records == {}
    assert db.calls == []
```

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -k fetch_records -v`
Expected: FAIL with `AttributeError: 'PgCodeSearchBackend' object has no attribute 'fetch_records'`

- [ ] **Step 12: Implement `fetch_records`**

```python
    async def fetch_records(
        self, chunk_ids: list[str], scope: ScopeKey
    ) -> dict[str, CodeChunkRecord]:
        """Resolve chunk_ids to full records in one scoped IN (...) query."""
        if not chunk_ids:
            return {}
        org_id, repo_id, missing = await self._resolve_scope(scope)
        if missing:
            return {}
        where_sql, where_params = _build_scope_where(org_id, repo_id, scope.branch)
        placeholders = ", ".join(["%s"] * len(chunk_ids))
        sql = (
            f"SELECT {_RECORD_COLUMNS} FROM code_chunks c "  # nosec B608 -- placeholder count matches chunk_ids length, all bound
            "JOIN code_repos r ON r.id = c.repo_id "
            f"WHERE {where_sql} AND c.id IN ({placeholders})"
        )
        params = [*where_params, *[int(cid) for cid in chunk_ids]]
        rows = await asyncio.to_thread(self.db.executesql, sql, params)
        return {str(row[0]): _row_to_record(row) for row in rows}


__all__ = ["PgCodeSearchBackend"]
```

- [ ] **Step 13: Run the full backend test file to verify everything passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: PASS (8 tests)

- [ ] **Step 14: Commit**

```bash
git add shared/knowledge/coderag_backend.py tests/unit/knowledge/test_coderag_backend.py
git commit -m "feat(coderag): real pgvector+FTS CodeSearchBackend, SQL-scoped by org/repo/branch"
```

---

### Task 5: `CodeKnowledgeSourceAdapter` + `build_code_knowledge_sources()`

**Files:**
- Modify: `shared/knowledge/coderag_backend.py` (append)
- Test: `tests/unit/knowledge/test_coderag_backend.py` (append)

**Interfaces:**
- Consumes: `PgCodeSearchBackend` (Task 4), `shared.knowledge.retriever.KnowledgeSourceBackend`/`search_code`, `shared.knowledge.scoping.ScopedRecord`.
- Produces: `build_code_knowledge_sources(db: object) -> dict[str, KnowledgeSourceBackend]`, consumed by Task 7 (`proxy/apps/proxy_server/main.py`).

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/knowledge/test_coderag_backend.py`:

```python
from shared.knowledge.coderag_backend import CodeKnowledgeSourceAdapter, build_code_knowledge_sources
from shared.knowledge.code_search import SearchResult
from shared.knowledge.scoping import ScopedRecord


def _search_result(record_id: str) -> SearchResult:
    record = ScopedRecord(
        id=record_id,
        content="def handler(): ...",
        scope_type=ScopeType.REPO,
        scope_ref="waddleai",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="7",
        repo="waddleai",
        branch="main",
    )
    return SearchResult(
        chunk_id=record_id,
        path="handler.py",
        symbol="handler",
        kind="function",
        content="def handler(): ...",
        score=0.9,
        record=record,
    )


@pytest.mark.asyncio
async def test_code_knowledge_source_adapter_delegates_to_search_code(monkeypatch) -> None:
    """CodeKnowledgeSourceAdapter.search() returns the underlying records, unwrapped."""
    adapter = CodeKnowledgeSourceAdapter(db=FakeDB())

    async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
        return [_search_result("chunk-1")]

    monkeypatch.setattr(
        "shared.knowledge.coderag_backend.retriever_search_code", _fake_search_code
    )

    caller = ScopeKey(org="7", repo="waddleai", branch="main")
    records = await adapter.search("handler", caller, top_k=5)

    assert len(records) == 1
    assert records[0].id == "chunk-1"


def test_build_code_knowledge_sources_returns_code_key_only() -> None:
    """build_code_knowledge_sources() wires exactly the 'code' source -- docs/uploaded/memory land separately."""
    sources = build_code_knowledge_sources(FakeDB())

    assert set(sources) == {"code"}
    assert isinstance(sources["code"], CodeKnowledgeSourceAdapter)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -k "adapter or build_code" -v`
Expected: FAIL with `ImportError: cannot import name 'CodeKnowledgeSourceAdapter'`

- [ ] **Step 3: Implement**

Append to `shared/knowledge/coderag_backend.py`:

```python
from dataclasses import dataclass, field

from shared.knowledge.retriever import KnowledgeSourceBackend
from shared.knowledge.retriever import search_code as retriever_search_code
from shared.knowledge.scoping import ScopedRecord


@dataclass(slots=True)
class CodeKnowledgeSourceAdapter:
    """Adapts PgCodeSearchBackend to KnowledgeSourceBackend for KnowledgeRetriever's "code" source.

    Used by the proxy's KnowledgeInjectStage (auto-inject path for plain,
    non-MCP-capable clients) -- the MCP pull-path tools use
    shared.mcp.knowledge_adapter.CodeRagKnowledgeService instead, which
    wraps the same PgCodeSearchBackend for the KnowledgeService Protocol.
    """

    db: object
    backend: PgCodeSearchBackend = field(init=False)

    def __post_init__(self) -> None:
        """Bind the underlying PgCodeSearchBackend to this adapter's db handle."""
        self.backend = PgCodeSearchBackend(self.db)

    async def search(self, query: str, caller: ScopeKey, top_k: int) -> list[ScopedRecord]:
        """Hybrid CodeRAG search, unwrapped to the ScopedRecord list KnowledgeRetriever expects."""
        results = await retriever_search_code(query, caller, self.backend, top_k, embed_db=self.db)
        return [r.record for r in results]


def build_code_knowledge_sources(db: object) -> dict[str, KnowledgeSourceBackend]:
    """Real KnowledgeRetriever.sources wiring for CodeRAG (§9.1) -- replaces the sources={} no-op."""
    return {"code": CodeKnowledgeSourceAdapter(db)}


__all__ = [
    "PgCodeSearchBackend",
    "CodeKnowledgeSourceAdapter",
    "build_code_knowledge_sources",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/test_coderag_backend.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/knowledge/coderag_backend.py tests/unit/knowledge/test_coderag_backend.py
git commit -m "feat(coderag): CodeKnowledgeSourceAdapter for the proxy auto-inject 'code' source"
```

---

### Task 6: `CodeRagKnowledgeService` — the real MCP `KnowledgeService` adapter

**Files:**
- Create: `shared/mcp/knowledge_adapter.py`
- Test: `tests/unit/mcp/test_knowledge_adapter.py`

**Interfaces:**
- Consumes: `shared.knowledge.coderag_backend.PgCodeSearchBackend` (Task 4), `shared.knowledge.retriever.search_code`, `shared.mcp.stub_adapters.NotWiredKnowledgeService`, `shared.mcp.tools.KnowledgeService`.
- Produces: `CodeRagKnowledgeService(db: object)`, implementing `search_code`/`get_symbol` for real; `search_docs`/`fetch_docs` stay inherited from `NotWiredKnowledgeService` (docs-cache is a separate subsystem, out of this plan's scope). Consumed by Task 8 (`mcp_mount.py` wiring in `main.py`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/mcp/test_knowledge_adapter.py
"""Tests for shared.mcp.knowledge_adapter.CodeRagKnowledgeService.

search_code/get_symbol are real (wired to PgCodeSearchBackend); search_docs/
fetch_docs stay ServiceUnavailableError (inherited from NotWiredKnowledgeService)
-- docs-cache is a separate subsystem this plan does not touch.
"""

from __future__ import annotations

import pytest

from shared.knowledge.code_search import CodeChunkRecord
from shared.knowledge.scoping import ScopeType, TrustTier
from shared.mcp.knowledge_adapter import CodeRagKnowledgeService
from shared.mcp.tools import ServiceUnavailableError


def _record(**overrides: object) -> CodeChunkRecord:
    defaults: dict[str, object] = dict(
        id="9",
        content="def calculate_total(): ...",
        scope_type=ScopeType.REPO,
        scope_ref="waddleai",
        trust_tier=TrustTier.DERIVED,
        author_user_id=None,
        org="7",
        repo="waddleai",
        branch="main",
        path="billing.py",
        symbol="calculate_total",
        kind="function",
    )
    defaults.update(overrides)
    return CodeChunkRecord(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_search_code_scopes_to_org_and_returns_serialized_results(monkeypatch) -> None:
    """search_code builds a ScopeKey from org_id/repo/branch and serializes SearchResults."""
    service = CodeRagKnowledgeService(db=object())

    async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
        assert caller.org == "7"
        assert caller.repo == "waddleai"
        assert caller.branch == "main"
        from shared.knowledge.code_search import SearchResult

        return [
            SearchResult(
                chunk_id="9",
                path="billing.py",
                symbol="calculate_total",
                kind="function",
                content="def calculate_total(): ...",
                score=0.95,
                record=_record(),
            )
        ]

    monkeypatch.setattr(
        "shared.mcp.knowledge_adapter.retriever_search_code", _fake_search_code
    )

    results = await service.search_code(org_id=7, query="calculate_total", repo="waddleai", branch="main")

    assert results == [
        {
            "chunk_id": "9",
            "path": "billing.py",
            "symbol": "calculate_total",
            "kind": "function",
            "content": "def calculate_total(): ...",
            "score": 0.95,
        }
    ]


@pytest.mark.asyncio
async def test_get_symbol_found(monkeypatch) -> None:
    """get_symbol resolves a symbol-exact hit to a plain dict."""
    service = CodeRagKnowledgeService(db=object())

    async def _fake_symbol_exact(self, query_text, scope):
        assert scope.org == "7"
        return _record()

    monkeypatch.setattr(
        "shared.knowledge.coderag_backend.PgCodeSearchBackend.symbol_exact", _fake_symbol_exact
    )

    result = await service.get_symbol(org_id=7, symbol="calculate_total", repo="waddleai")

    assert result == {
        "path": "billing.py",
        "symbol": "calculate_total",
        "kind": "function",
        "content": "def calculate_total(): ...",
    }


@pytest.mark.asyncio
async def test_get_symbol_not_found(monkeypatch) -> None:
    """get_symbol returns None, not an error, when the symbol isn't indexed."""
    service = CodeRagKnowledgeService(db=object())

    async def _fake_symbol_exact(self, query_text, scope):
        return None

    monkeypatch.setattr(
        "shared.knowledge.coderag_backend.PgCodeSearchBackend.symbol_exact", _fake_symbol_exact
    )

    result = await service.get_symbol(org_id=7, symbol="missing", repo="waddleai")

    assert result is None


@pytest.mark.asyncio
async def test_search_docs_and_fetch_docs_remain_not_wired() -> None:
    """docs-cache is a separate subsystem -- CodeRagKnowledgeService does not implement it."""
    service = CodeRagKnowledgeService(db=object())

    with pytest.raises(ServiceUnavailableError):
        await service.search_docs(query="q", ecosystem=None)
    with pytest.raises(ServiceUnavailableError):
        await service.fetch_docs(ecosystem="python", package="requests", version=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_knowledge_adapter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.mcp.knowledge_adapter'`

- [ ] **Step 3: Implement**

```python
# shared/mcp/knowledge_adapter.py
"""The real MCP KnowledgeService adapter for CodeRAG (§9.1/§11.1 core-completion).

Replaces NotWiredKnowledgeService.search_code/.get_symbol with real
PgCodeSearchBackend-backed implementations, wired into the /mcp
search_code/get_symbol tools via McpServiceFactory
(proxy/apps/proxy_server/mcp_mount.py). Subclasses NotWiredKnowledgeService
so search_docs/fetch_docs keep raising ServiceUnavailableError honestly --
docs-cache is a separate subsystem this plan does not touch.
"""

from __future__ import annotations

from typing import Any

from shared.knowledge.coderag_backend import PgCodeSearchBackend
from shared.knowledge.retriever import search_code as retriever_search_code
from shared.knowledge.scoping import ScopeKey
from shared.mcp.stub_adapters import NotWiredKnowledgeService


class CodeRagKnowledgeService(NotWiredKnowledgeService):
    """Real search_code/get_symbol, backed by PgCodeSearchBackend; docs stay not-wired."""

    def __init__(self, db: object) -> None:
        """Bind to a penguin-dal handle, constructing the underlying search backend."""
        self.db = db
        self.backend = PgCodeSearchBackend(db)

    async def search_code(
        self, *, org_id: int, query: str, repo: str | None, branch: str | None
    ) -> list[dict[str, Any]]:
        """Hybrid CodeRAG search over an org's indexed repos, serialized for the MCP tool response."""
        caller = ScopeKey(org=str(org_id), repo=repo, branch=branch)
        results = await retriever_search_code(query, caller, self.backend, top_k=10, embed_db=self.db)
        return [
            {
                "chunk_id": r.chunk_id,
                "path": r.path,
                "symbol": r.symbol,
                "kind": r.kind,
                "content": r.content,
                "score": r.score,
            }
            for r in results
        ]

    async def get_symbol(
        self, *, org_id: int, symbol: str, repo: str | None
    ) -> dict[str, Any] | None:
        """Symbol-exact chunk lookup, serialized; None if the symbol isn't indexed."""
        caller = ScopeKey(org=str(org_id), repo=repo)
        record = await self.backend.symbol_exact(symbol, caller)
        if record is None:
            return None
        return {
            "path": record.path,
            "symbol": record.symbol,
            "kind": record.kind,
            "content": record.content,
        }


__all__ = ["CodeRagKnowledgeService"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/mcp/test_knowledge_adapter.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add shared/mcp/knowledge_adapter.py tests/unit/mcp/test_knowledge_adapter.py
git commit -m "feat(coderag): real CodeRagKnowledgeService for the /mcp search_code/get_symbol tools"
```

---

### Task 7: Wire the proxy auto-inject pipeline

**Files:**
- Modify: `proxy/apps/proxy_server/main.py:816-825` (the `sources={}` block)
- Test: `tests/unit/proxy/test_coderag_wiring.py`

**Interfaces:**
- Consumes: `shared.knowledge.coderag_backend.build_code_knowledge_sources` (Task 5).

- [ ] **Step 1: Write the failing regression-guard test**

```python
# tests/unit/proxy/test_coderag_wiring.py
"""Regression guard: proxy/apps/proxy_server/main.py must not wire the CodeRAG no-op.

Same grep-based technique as tests/unit/management/test_scope_authz.py's
test_no_require_role_outside_tests -- catches the sources={} no-op (or a
NotWiredKnowledgeService default) being reintroduced.
"""

from __future__ import annotations

from pathlib import Path

from shared.knowledge.coderag_backend import CodeKnowledgeSourceAdapter, build_code_knowledge_sources
from shared.knowledge.scoping import ScopeKey

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PY = REPO_ROOT / "proxy" / "apps" / "proxy_server" / "main.py"


def test_main_py_does_not_wire_the_sources_noop() -> None:
    """main.py must call build_code_knowledge_sources, not construct sources={} directly."""
    text = MAIN_PY.read_text()
    assert "sources={}" not in text
    assert "build_code_knowledge_sources" in text


def test_main_py_passes_a_real_mcp_service_factory() -> None:
    """MCPMount must be constructed with a service_factory, not the all-stub default."""
    text = MAIN_PY.read_text()
    assert "service_factory=" in text
    assert "CodeRagKnowledgeService" in text


def test_build_code_knowledge_sources_is_independently_testable() -> None:
    """The factory function itself returns a working 'code' source without booting the app."""
    sources = build_code_knowledge_sources(db=object())
    assert isinstance(sources["code"], CodeKnowledgeSourceAdapter)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_coderag_wiring.py -v`
Expected: FAIL — `test_main_py_does_not_wire_the_sources_noop` and `test_main_py_passes_a_real_mcp_service_factory` fail (both conditions still true against unmodified `main.py`).

- [ ] **Step 3: Wire it in `main.py`**

Replace the block at `proxy/apps/proxy_server/main.py:808-825` (the `KnowledgeRetriever` construction):

```python
        # KnowledgeRetriever (§9.5): "code" is wired to the real
        # PgCodeSearchBackend-backed adapter (§9.1 core-completion);
        # docs/uploaded/memory sources remain unwired (separate subsystems,
        # out of this plan's scope) -- __call__ still resolves per-key/
        # per-source flags for all four, so those three stay documented
        # no-ops until their own backends land.
        from shared.knowledge.coderag_backend import build_code_knowledge_sources

        self.knowledge_retriever = KnowledgeRetriever(
            sources=build_code_knowledge_sources(self.db),
            scanner=self.security_scanner,
            content_filter=self.content_filter,
        )
```

Then update the top-level import at `proxy/apps/proxy_server/main.py:64` to also bring in `McpServiceFactory` (defined in the same `mcp_mount.py` module as `MCPMount`):

```python
from .mcp_mount import MCPMount, McpServiceFactory
```

And update the `MCPMount(...)` construction at `proxy/apps/proxy_server/main.py:999-1001`:

```python
    from shared.mcp.knowledge_adapter import CodeRagKnowledgeService

    app.asgi_app = MCPMount(
        app.asgi_app,
        rbac=proxy_server.rbac,
        oidc_provider=proxy_server.oidc_provider,
        service_factory=McpServiceFactory(
            knowledge_factory=lambda: CodeRagKnowledgeService(proxy_server.db)
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_coderag_wiring.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing knowledge pipeline wiring tests to confirm no regression**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_knowledge_pipeline_wiring.py tests/unit/proxy/test_knowledge_stage.py -v`
Expected: PASS (unchanged — those tests inject their own stub/mock retriever and are unaffected by `main.py`'s wiring change)

- [ ] **Step 6: Commit**

```bash
git add proxy/apps/proxy_server/main.py tests/unit/proxy/test_coderag_wiring.py
git commit -m "feat(coderag): wire the real CodeRAG backend into the proxy auto-inject + /mcp paths"
```

---

### Task 8: `McpServiceFactory` default stays test-only; confirm `mcp_mount` tests still pass

**Files:**
- Test: `tests/unit/mcp/test_server.py`, `tests/unit/proxy/test_mcp_mount.py` (if present) — no production code changes beyond Task 7.

**Interfaces:**
- Consumes: Task 7's wiring.

Task 7 already wires the real `service_factory` at the one production call site (`main.py`). `McpServiceFactory`'s dataclass defaults (`NotWiredKnowledgeService`, etc.) intentionally stay unchanged — they exist precisely so tests can construct an `MCPMount` without a real DB. This task is a verification-only step, not a new implementation.

- [ ] **Step 1: Run the full MCP test suite to confirm the stub-default tests are unaffected**

Run: `.venv/bin/python -m pytest tests/unit/mcp/ -v`
Expected: PASS (all existing tests continue to pass — they construct `McpServiceFactory`/`MCPMount` explicitly with their own stubs or defaults, independent of `main.py`)

- [ ] **Step 2: Find and run any proxy-level MCP mount test**

Run: `.venv/bin/python -m pytest tests/unit/proxy/ -k mcp -v`
Expected: PASS

- [ ] **Step 3: Commit (if any test file needed adjustment; otherwise skip — no diff)**

If Step 1 or 2 surfaced a failure caused by Task 7's `main.py` change (for example a test importing `main.py` at module scope and asserting on the old `sources={}` literal), fix that test's assertion to match the new wiring, then:

```bash
git add tests/unit/mcp/ tests/unit/proxy/
git commit -m "test(coderag): confirm MCP stub-default tests are unaffected by real service wiring"
```

---

### Task 9: Management REST API — `code_repos.py`

**Files:**
- Create: `services/management/app/api/v1/code_repos.py`
- Modify: `services/management/app/api/v1/__init__.py` (append import)
- Modify: `tests/unit/management/conftest.py:266-293` (`ROUTE_MODULES`)
- Test: `tests/unit/management/test_code_repos_api.py`

**Interfaces:**
- Consumes: `shared.knowledge.coderag_backend` not needed here (this file talks to `code_repos`/triggers `CodeRagWorker` directly); `services.management.app.services.coderag_worker.create_coderag_worker` (existing); `shared.security.credential_encryption.encrypt_credential`/`decrypt_credential` (existing); `services.management.app.api.v1.webhooks.verify_webhook_signature` (existing); `Permission.CODE_REPO_WRITE` (Task 2).
- Produces: `POST/GET /api/v1/code-repos`, `GET/DELETE /api/v1/code-repos/<id>`, `POST /api/v1/code-repos/<id>/reindex`, `POST /api/v1/code-repos/reindex-all`, `POST /api/v1/code-repos/webhook`.

- [ ] **Step 1: Add `code_repos` to the test-fixture route-module list**

In `tests/unit/management/conftest.py`, append to the `ROUTE_MODULES` list (after `"services.management.app.api.v1.fleet"`):

```python
    "services.management.app.api.v1.fleet",
    "services.management.app.api.v1.code_repos",
]
```

- [ ] **Step 2: Write the failing test for create + org isolation + IDOR**

```python
# tests/unit/management/test_code_repos_api.py
"""Tests for /api/v1/code-repos: register/list/get/delete/reindex + webhook, org isolation (§9.1)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from tests.unit.management.conftest import make_dal_row, make_select_result


@pytest.fixture(autouse=True)
def _stub_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLEAI_STUB_UPSTREAM", "1")


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "1")


class TestCreateCodeRepo:
    async def test_create_returns_webhook_secret_once(self, client, rm_auth_headers, app_mock_db) -> None:
        """A successful create returns the plaintext webhook_secret exactly once."""
        app_mock_db.code_repos.insert.return_value = 1
        app_mock_db.return_value.select.return_value = make_select_result([])  # no existing repo

        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={"name": "waddleai", "source_url": "https://github.com/penguintechinc/waddleai.git"},
        )

        assert resp.status_code == 201
        body = await resp.get_json()
        assert body["name"] == "waddleai"
        assert "webhook_secret" in body and len(body["webhook_secret"]) > 20

    async def test_create_requires_code_repo_write_scope(self, client, user_auth_headers) -> None:
        """A caller without CODE_REPO_WRITE is refused (403)."""
        resp = await client.post(
            "/api/v1/code-repos",
            headers=user_auth_headers,
            json={"name": "x", "source_url": "https://example.com/x.git"},
        )
        assert resp.status_code == 403

    async def test_create_returns_404_when_flag_off(
        self, client, rm_auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag-off path never touches the DB -- 404, not 201/500."""
        monkeypatch.setenv("WADDLEAI_FLAG_CODERAG", "0")
        resp = await client.post(
            "/api/v1/code-repos",
            headers=rm_auth_headers,
            json={"name": "x", "source_url": "https://example.com/x.git"},
        )
        assert resp.status_code == 404


class TestGetCodeRepoIDORSafe:
    async def test_get_outside_org_returns_404_not_403(
        self, client, rm_auth_headers, app_mock_db
    ) -> None:
        """A repo id belonging to a different org resolves to 404 -- never leaks existence."""
        app_mock_db.return_value.select.return_value = make_select_result([])

        resp = await client.get("/api/v1/code-repos/999", headers=rm_auth_headers)

        assert resp.status_code == 404


class TestWebhook:
    def _sign(self, secret: str, body: bytes) -> str:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    async def test_webhook_rejects_bad_signature(self, client, app_mock_db) -> None:
        """A push payload with an invalid HMAC signature is rejected (401), never triggers a re-index."""
        row = make_dal_row(
            id=1,
            org_id=7,
            source_url="https://github.com/penguintechinc/waddleai.git",
            # No "enc:" prefix -- decrypt_credential() returns non-"enc:"-prefixed
            # values as-is (pre-encryption/test-environment compatibility path),
            # so this exercises the signature mismatch, not a decryption error.
            webhook_secret="whs_test_not_the_real_secret",  # noqa: S105 -- test fixture, not a real secret
        )
        app_mock_db.return_value.select.return_value = make_select_result([row])
        payload = {
            "repository": {"clone_url": "https://github.com/penguintechinc/waddleai.git"},
            "ref": "refs/heads/main",
        }
        body = json.dumps(payload).encode()

        resp = await client.post(
            "/api/v1/code-repos/webhook",
            data=body,
            headers={"X-Hub-Signature-256": "sha256=wrong", "Content-Type": "application/json"},
        )

        assert resp.status_code == 401

    async def test_webhook_unknown_repo_returns_404(self, client, app_mock_db) -> None:
        """An unregistered clone_url is rejected (404), never falls through to signature checks."""
        app_mock_db.return_value.select.return_value = make_select_result([])
        payload = {"repository": {"clone_url": "https://example.com/unknown.git"}, "ref": "refs/heads/main"}

        resp = await client.post(
            "/api/v1/code-repos/webhook",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 404
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_code_repos_api.py -v`
Expected: FAIL with `404` for every request (no `code_repos.py` blueprint registered yet) / collection error once `code_repos` is referenced in `ROUTE_MODULES` but the module doesn't exist.

- [ ] **Step 4: Implement the route module**

```python
# services/management/app/api/v1/code_repos.py
"""§9.1 CodeRAG repo registration: ``/api/v1/code-repos`` CRUD + reindex + webhook.

Registers a git repository for CodeRAG indexing (``code_repos``), triggers
manual/cron re-indexing via ``CodeRagWorker``, and accepts GitHub/Gitea push
webhooks (HMAC-verified via a per-repo, Fernet-encrypted shared secret
generated at registration time and shown to the caller exactly once).

Flag: ``waddleai.coderag``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Any

from quart import g, jsonify, request

from shared.auth.rbac import Permission
from shared.security.credential_encryption import decrypt_credential, encrypt_credential

from ...extensions import db
from ...services.coderag_worker import create_coderag_worker
from . import api_v1_bp
from .auth import require_auth, require_scope
from .webhooks import verify_webhook_signature

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.coderag"


def _coderag_enabled(org_id: int) -> bool:
    """Fail-safe-OFF check of the ``waddleai.coderag`` flag (§14.5)."""
    try:
        from shared.utils.feature_flags import is_feature_enabled

        return is_feature_enabled(_FLAG_KEY, distinct_id=str(org_id), default=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("coderag flag evaluation failed, treating as OFF: %s", exc)
        return False


def _serialize(row: Any) -> dict[str, Any]:
    """Explicit response schema -- never serialize the raw ORM row, never the webhook secret."""
    created_at = getattr(row, "created_at", None)
    updated_at = getattr(row, "updated_at", None)
    return {
        "id": row.id,
        "org_id": row.org_id,
        "name": row.name,
        "source_url": row.source_url,
        "index_status": row.index_status,
        "last_commit": row.last_commit,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@api_v1_bp.route("/code-repos", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def create_code_repo():
    """Register a repo for CodeRAG indexing; returns its one-time webhook_secret."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    body = await request.get_json()
    if not body or not body.get("name") or not body.get("source_url"):
        return jsonify({"error": "name and source_url are required"}), 400
    name = body["name"]
    source_url = body["source_url"]
    credentials_ref = body.get("credentials_ref")

    webhook_secret_plain = secrets.token_urlsafe(32)
    webhook_secret_encrypted = encrypt_credential(webhook_secret_plain)
    now = datetime.utcnow()

    def _create() -> Any:
        existing = db(
            (db.code_repos.org_id == org_id) & (db.code_repos.name == name)
        ).select().first()
        if existing is not None:
            return None
        repo_id = db.code_repos.insert(
            org_id=org_id,
            name=name,
            source_url=source_url,
            credentials_ref=credentials_ref,
            webhook_secret=webhook_secret_encrypted,
            index_status="pending",
        )
        db.commit()
        return repo_id

    repo_id = await asyncio.to_thread(_create)
    if repo_id is None:
        return jsonify({"error": "a repo with this name already exists in your org"}), 409

    response = {
        "id": repo_id,
        "org_id": org_id,
        "name": name,
        "source_url": source_url,
        "index_status": "pending",
        "last_commit": None,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "webhook_secret": webhook_secret_plain,  # shown exactly once
    }
    return jsonify(response), 201


@api_v1_bp.route("/code-repos", methods=["GET"])
@require_auth
async def list_code_repos():
    """List CodeRAG-registered repos for the caller's org."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch() -> list[Any]:
        return list(db(db.code_repos.org_id == org_id).select())

    rows = await asyncio.to_thread(_fetch)
    return jsonify({"repos": [_serialize(r) for r in rows]}), 200


@api_v1_bp.route("/code-repos/<int:repo_id>", methods=["GET"])
@require_auth
async def get_code_repo(repo_id: int):
    """Fetch a single repo, org-scoped (IDOR-safe: 404 outside the caller's org)."""
    org_id = g.user.get("organization_id")
    if not _coderag_enabled(org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    def _fetch() -> Any:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_serialize(row)), 200


@api_v1_bp.route("/code-repos/<int:repo_id>", methods=["DELETE"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def delete_code_repo(repo_id: int):
    """Delete a repo registration, org-scoped (IDOR-safe: 404 outside the caller's org)."""
    org_id = g.user.get("organization_id")

    def _delete() -> bool:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        existing = db(query).select().first()
        if existing is None:
            return False
        db(query).delete()
        db.commit()
        return True

    deleted = await asyncio.to_thread(_delete)
    if not deleted:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted", "id": repo_id}), 200


@api_v1_bp.route("/code-repos/<int:repo_id>/reindex", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def reindex_code_repo(repo_id: int):
    """Manually trigger (re)indexing for one repo, org-scoped."""
    org_id = g.user.get("organization_id")

    def _fetch() -> Any:
        query = (db.code_repos.id == repo_id) & (db.code_repos.org_id == org_id)
        return db(query).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "not found"}), 404

    body = await request.get_json(silent=True) or {}
    worker = create_coderag_worker(db)
    result = await worker.index(repo_id, branch=body.get("branch"), trigger="manual")
    return (
        jsonify(
            {
                "repo_id": result.repo_id,
                "branch_ref": result.branch_ref,
                "index_status": result.index_status,
                "last_commit": result.last_commit,
                "files_changed": result.files_changed,
                "files_deleted": result.files_deleted,
                "error": result.error,
            }
        ),
        200,
    )


@api_v1_bp.route("/code-repos/reindex-all", methods=["POST"])
@require_auth
@require_scope(Permission.CODE_REPO_WRITE)
async def reindex_all_code_repos():
    """Cron entrypoint: re-index every non-disabled registered repo (all orgs, flag-gated per-repo)."""
    worker = create_coderag_worker(db)
    results = await worker.run_scheduled()
    return (
        jsonify(
            {
                "indexed": len(results),
                "results": [
                    {
                        "repo_id": r.repo_id,
                        "branch_ref": r.branch_ref,
                        "index_status": r.index_status,
                        "error": r.error,
                    }
                    for r in results
                ],
            }
        ),
        200,
    )


@api_v1_bp.route("/code-repos/webhook", methods=["POST"])
async def code_repo_webhook():
    """GitHub/Gitea push webhook: HMAC-verified against the repo's stored secret, then re-index."""
    raw_body = await request.get_data()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid JSON payload"}), 400

    clone_url = (payload.get("repository") or {}).get("clone_url")
    if not clone_url:
        return jsonify({"error": "missing repository.clone_url"}), 400

    def _fetch() -> Any:
        return db(db.code_repos.source_url == clone_url).select().first()

    row = await asyncio.to_thread(_fetch)
    if row is None:
        return jsonify({"error": "unknown repository"}), 404

    signature = request.headers.get("X-Hub-Signature-256", "")
    try:
        secret = decrypt_credential(row.webhook_secret) if row.webhook_secret else ""
    except ValueError:
        # A corrupted/un-decryptable stored secret must fail closed (never
        # 500) -- treated exactly like "no secret configured", which
        # verify_webhook_signature already rejects unconditionally.
        secret = ""
    if not verify_webhook_signature(raw_body, signature, secret):
        return jsonify({"error": "invalid signature"}), 401

    if not _coderag_enabled(row.org_id):
        return jsonify({"error": "coderag feature disabled"}), 404

    worker = create_coderag_worker(db)
    resolved = worker.handle_webhook(payload)
    if resolved is None:
        return jsonify({"error": "unrecognized payload"}), 400
    repo_id, branch = resolved

    try:
        asyncio.create_task(worker.index(repo_id, branch, trigger="webhook"))
    except RuntimeError:
        # No running event loop in this context (e.g. some test harnesses) -- skip.
        pass

    return jsonify({"status": "accepted", "repo_id": repo_id, "branch": branch}), 202
```

- [ ] **Step 5: Register the module (append-only import block)**

In `services/management/app/api/v1/__init__.py`:

```python
from . import (  # noqa: I001 -- append-only order, see comment above
    auth,
    cache_configs,
    cilium,
    keys,
    knowledge,
    llamacpp,
    memory_config,
    memory_scoping,
    ollama,
    ollama_models,
    organizations,
    providers,
    quotas,
    usage,
    users,
    webhooks,
    integrations,
    fleet,
    code_repos,
)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/management/test_code_repos_api.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Regenerate and lint the OpenAPI spec**

Run: `make generate-openapi && make openapi-lint`
Expected: `openapi/v1.yaml` gains the new `/api/v1/code-repos*` paths; `spectral lint` reports 0 errors.

- [ ] **Step 8: Run mypy --strict on the new module**

Run: `.venv/bin/python -m mypy --strict services/management/app/api/v1/code_repos.py shared/knowledge/coderag_backend.py shared/mcp/knowledge_adapter.py`
Expected: no errors (fix any missing annotations before proceeding).

- [ ] **Step 9: Commit**

```bash
git add services/management/app/api/v1/code_repos.py \
        services/management/app/api/v1/__init__.py \
        tests/unit/management/conftest.py \
        tests/unit/management/test_code_repos_api.py \
        openapi/v1.yaml
git commit -m "feat(coderag): /api/v1/code-repos registration, reindex, and webhook API"
```

---

### Task 10: Update `test_scope_authz.py` for the new scope + routes

**Files:**
- Modify: `tests/unit/management/test_scope_authz.py:67-115`

**Interfaces:**
- Consumes: `Permission.CODE_REPO_WRITE` (Task 2), the 4 new `require_scope`-gated routes from Task 9 (`POST /code-repos`, `DELETE /code-repos/<id>`, `POST /code-repos/<id>/reindex`, `POST /code-repos/reindex-all`).

- [ ] **Step 1: Run the scope-authz suite to see the current failure shape**

Run: `.venv/bin/python -m pytest tests/unit/management/test_scope_authz.py -v`
Expected: FAIL —
- `test_scoped_routes_use_only_migrated_scopes` fails: 4 routes require `code_repo:write`, not in `_MIGRATED_SCOPES`.
- `test_scoped_routes_match_audited_count` fails: route count is now `113 + 4 = 117`, not `113`.

- [ ] **Step 2: Update the scope sets and expected count**

```python
_B_TIER_SCOPES = {
    Permission.CACHE_CONFIG_WRITE.value,
    Permission.HOOK_RULE_ADMIN.value,
    Permission.HOOK_METRICS_READ.value,
    Permission.KNOWLEDGE_WRITE.value,
    Permission.MODEL_ALIAS_WRITE.value,
    Permission.QUOTA_LIST.value,
    Permission.QUOTA_UPDATE.value,
    Permission.ROUTING_ASSIGNMENT_WRITE.value,
    Permission.ROUTING_POLICY_WRITE.value,
    Permission.ROUTING_RULE_WRITE.value,
    Permission.SECURITY_BYPASS_GRANT_WRITE.value,
    Permission.USAGE_READ_BY_USER.value,
    Permission.USER_MANAGE.value,
    Permission.MODEL_ACCESS_POLICY_WRITE.value,
    # Not part of the OIDC-scope migration (new feature, CodeRAG core
    # completion) but enumerated by the same generic route scan below.
    Permission.CODE_REPO_WRITE.value,
}
```

And bump the count comment/value:

```python
_EXPECTED_ROUTE_COUNT = 117  # audited require_role call sites, both migration waves,
#   +3 net-new (not a require_role migration, model-access-policy CRUD)
#   +4 net-new (not a require_role migration, code_repos CRUD/reindex CRUD):
#   94 from the original OIDC migration, +16 from branches cut before it landed
#   (6 fleet, 9 hook_rules, 1 hook_metrics), +3 from the model-access-policy
#   CRUD blueprint (POST/PUT -> MODEL_ACCESS_POLICY_WRITE, DELETE ->
#   MODEL_ACCESS_POLICY_DELETE; GET/GET-by-id are require_auth only), +4 from
#   the code_repos blueprint (POST create, DELETE, POST reindex, POST
#   reindex-all -> CODE_REPO_WRITE; GET/GET-by-id are require_auth only).
```

- [ ] **Step 3: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/management/test_scope_authz.py -v`
Expected: PASS (all tests, including the parametrized `test_b_tier_scope_held_by_exactly_admin_and_resource_manager[code_repo:write]` case, picked up automatically since it iterates `sorted(_B_TIER_SCOPES)`)

- [ ] **Step 4: Commit**

```bash
git add tests/unit/management/test_scope_authz.py
git commit -m "test(coderag): register CODE_REPO_WRITE and the 4 new routes with the scope-authz suite"
```

---

### Task 11: Integration acceptance — SQL-scoping proof + end-to-end wiring smoke

**Files:**
- Modify: `tests/integration/test_knowledge_acceptance.py`

**Interfaces:**
- Consumes: `shared.knowledge.coderag_backend.PgCodeSearchBackend` (Task 4), `shared.mcp.knowledge_adapter.CodeRagKnowledgeService` (Task 6).

- [ ] **Step 1: Write the failing acceptance-layer SQL-scoping test**

Add to `tests/integration/test_knowledge_acceptance.py`, inside (or alongside) `class TestOrgIsolationAcrossStores`:

```python
class TestCodeSearchBackendSQLScoping:
    """(8) The real CodeSearchBackend scopes its SQL, not just the Python filter (§9.1/§9.7).

    Closes the audit gap: prior to this plan, isolation was a post-fetch
    scoping.is_visible() filter only -- an unscoped top-K query could starve
    the target repo's chunks out of the candidate set before Python ever
    saw them. This proves the WHERE clause itself carries org_id.
    """

    @pytest.mark.asyncio
    async def test_vector_search_sql_carries_org_id_in_where_clause(self) -> None:
        from shared.knowledge.coderag_backend import PgCodeSearchBackend
        from shared.knowledge.scoping import ScopeKey

        class _CapturingDB:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def executesql(self, sql: str, params) -> list:
                self.calls.append((sql, tuple(params)))
                return []  # repo resolution or search -- either way, no rows

        db = _CapturingDB()
        backend = PgCodeSearchBackend(db)
        scope = ScopeKey(org="42", repo="waddleai", branch="main")

        await backend.vector_search([0.0] * 768, scope, top_k=10)

        # Repo-resolution query ran first, org-scoped.
        resolve_sql, resolve_params = db.calls[0]
        assert "org_id = %s" in resolve_sql
        assert resolve_params[0] == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_knowledge_acceptance.py::TestCodeSearchBackendSQLScoping -v`
Expected: FAIL with `ModuleNotFoundError` (before Task 4 lands) or PASS immediately if run after Task 4 — run this task's tests only after Tasks 1-10 are complete; it is a final acceptance-layer confirmation, not new production code.

- [ ] **Step 3: Write the flag-on wiring smoke test**

Add alongside `class TestFlagOffAllSourcesNoOp`:

```python
class TestCodeRagFlagOnEndToEndSmoke:
    """(9) With the flag on and a real backend, search_code resolves instead of raising."""

    @pytest.mark.asyncio
    async def test_search_code_resolves_via_the_real_adapter(self, monkeypatch) -> None:
        from shared.knowledge.code_search import SearchResult
        from shared.knowledge.scoping import ScopeType, TrustTier, ScopedRecord
        from shared.mcp.knowledge_adapter import CodeRagKnowledgeService

        service = CodeRagKnowledgeService(db=object())

        async def _fake_search_code(query, caller, backend, top_k, *, embed_db=None):
            record = ScopedRecord(
                id="1",
                content="def f(): ...",
                scope_type=ScopeType.REPO,
                scope_ref="waddleai",
                trust_tier=TrustTier.DERIVED,
                author_user_id=None,
                org=caller.org,
                repo=caller.repo,
                branch=caller.branch,
            )
            return [
                SearchResult(
                    chunk_id="1", path="f.py", symbol="f", kind="function",
                    content="def f(): ...", score=1.0, record=record,
                )
            ]

        monkeypatch.setattr(
            "shared.mcp.knowledge_adapter.retriever_search_code", _fake_search_code
        )

        results = await service.search_code(org_id=42, query="f", repo="waddleai", branch="main")

        assert results == [
            {
                "chunk_id": "1", "path": "f.py", "symbol": "f", "kind": "function",
                "content": "def f(): ...", "score": 1.0,
            }
        ]
```

- [ ] **Step 4: Run the full acceptance suite**

Run: `.venv/bin/python -m pytest tests/integration/test_knowledge_acceptance.py -v`
Expected: PASS (all tests, including the two new classes)

- [ ] **Step 5: Run the complete affected test surface + coverage gate**

Run: `.venv/bin/python -m pytest tests/unit/knowledge/ tests/unit/mcp/ tests/unit/proxy/ tests/unit/management/test_code_repos_api.py tests/unit/management/test_migration_019.py tests/unit/management/test_scope_authz.py tests/unit/test_code_repo_write_scope.py tests/integration/test_knowledge_acceptance.py --cov=shared.knowledge --cov=shared.mcp.knowledge_adapter --cov=services.management.app.api.v1.code_repos --cov-fail-under=90 -v`
Expected: PASS, coverage ≥ 90% on every new module.

- [ ] **Step 6: Run bandit/mypy/ruff on all touched files**

```bash
.venv/bin/python -m bandit -r shared/knowledge/coderag_backend.py shared/mcp/knowledge_adapter.py services/management/app/api/v1/code_repos.py -ll
.venv/bin/python -m mypy --strict shared/knowledge/coderag_backend.py shared/mcp/knowledge_adapter.py services/management/app/api/v1/code_repos.py
.venv/bin/ruff check shared/knowledge/coderag_backend.py shared/mcp/knowledge_adapter.py services/management/app/api/v1/code_repos.py shared/knowledge/code_search.py shared/auth/rbac.py
```

Expected: 0 findings on all three.

- [ ] **Step 7: Commit**

```bash
git add tests/integration/test_knowledge_acceptance.py
git commit -m "test(coderag): acceptance-layer SQL-scoping proof + flag-on end-to-end smoke"
```

---

## Self-Review

**1. Spec coverage:**
- Real `CodeSearchBackend` (pgvector ivfflat cosine + Postgres FTS, RRF fusion, symbol-exact short-circuit) via penguin-dal → Task 4 (RRF/symbol-exact were already real in `code_search.py`; Task 4 supplies the missing DB-facing half).
- SQL-level `repo_id`/`org_id`/`branch_ref` scoping, `filter_visible` kept as defense-in-depth → Task 3 (Protocol) + Task 4 (implementation) + Task 11 (acceptance proof).
- Management REST API to register `code_repos` + trigger/schedule indexing → Task 9 (`POST /code-repos`, `POST /code-repos/<id>/reindex`, `POST /code-repos/reindex-all`).
- Webhook (GitHub/Gitea → `handle_webhook`) + cron (`run_scheduled`) wiring → Task 9 (`POST /code-repos/webhook`, `POST /code-repos/reindex-all`).
- Replace `NotWiredKnowledgeService`/`sources={}` so proxy `KnowledgeInjectStage` and MCP `search_code`/`get_symbol` resolve a live backend → Task 5 (proxy) + Task 6 (MCP) + Task 7 (wiring).
- Missing repo-vs-repo-same-org isolation test → Task 3, Step 1.
- Test asserting the SQL query itself is scoped (not just the Python filter) → Task 4 (unit) + Task 11 (acceptance).
- Wiring/acceptance tests → Task 7, Task 8, Task 11.
- Behind flag `waddleai.coderag` → every new route/adapter checks `_coderag_enabled()`/relies on the existing per-source flag check in `KnowledgeInjectStage`; no new code path bypasses it.

**2. Placeholder scan:** No "TBD"/"implement later"/"add appropriate error handling" strings anywhere in the plan; every step carries complete, runnable code. Confirmed by re-reading Tasks 1-11.

**3. Type consistency:** `CodeSearchBackend.vector_search(query_embedding, scope, top_k)` / `.fts_search(query_text, scope, top_k)` / `.fetch_records(chunk_ids, scope)` / `.symbol_exact(query_text, scope)` are introduced once in Task 3 and used with matching signatures in Task 4 (`PgCodeSearchBackend`), Task 5 (`CodeKnowledgeSourceAdapter` via `retriever_search_code`), and Task 6 (`CodeRagKnowledgeService` via `retriever_search_code`/`backend.symbol_exact`). `PgCodeSearchBackend(db)` constructor signature matches every call site (Task 5, Task 6). `build_code_knowledge_sources(db) -> dict[str, KnowledgeSourceBackend]` matches its Task 7 call site exactly. `Permission.CODE_REPO_WRITE` (Task 2) matches its usage in Task 9's `@require_scope(...)` decorators and Task 10's `_B_TIER_SCOPES` entry, string value `"code_repo:write"` consistent everywhere.

No gaps found; no changes made during self-review.

---

Plan complete and saved to `docs/superpowers/plans/2026-08-31-coderag-core-completion.md`.
