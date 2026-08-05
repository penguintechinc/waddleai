# Memory Access Control (Personal vs Organizational Scope) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `org` (shared) memory scope alongside the existing locked-down personal scope on the mem0-compatible memory API, with an authoritative `scope_type` column, a `metadata.scope` mirror for backward compat, and a new `memory:moderate` permission for org-level moderation.

**Architecture:** Two scopes (`user` default, `org` shared-within-org) signaled by a top-level `scope` body field (fallback `metadata.scope`). Postgres/pgvector gets real columns via Alembic migration 006; ChromaDB/mem0-client backends use the metadata mirror. Enforcement lives in the proxy's mem0 handlers as pure, unit-testable decision functions; org-write is gated behind a PostHog feature flag (default OFF, env fallback).

**Tech Stack:** Quart (proxy), PyDAL `executesql` raw SQL (pgvector store), SQLAlchemy+Alembic (schema authority), chromadb, mem0 client, posthog (already pinned at 7.9.12 — no requirements change), pytest with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-07-14-memory-access-control-design.md` (approved). Read it for rationale; THIS plan is the implementation authority.

**Deliberate spec deviations (do not "fix" these):**
1. The spec says `WaddleAIMemoryManager` "gains scope passthrough". In the actual code the mem0 handlers call `manager.memory_store` directly, and internal manager callers construct `MemoryEntry` objects — the `scope_type="user"` dataclass default plus the store methods' `scope="user"` defaults already deliver the spec's observable intent (internal callers compile unchanged and stay personal). No manager signature changes are made (YAGNI).
2. The spec places the "org add visible to second user / personal invisible" isolation assertion under contract tests. The contract environment runs sqlite, where every pgvector SQL call fails closed (nothing is ever stored), so that assertion CANNOT be exercised there. It lives instead as a real-ChromaDB visibility test (Task 4) plus pgvector SQL-shape unit tests (Task 3); the contract suite covers the authz surface (400/403 responses, flag gate, moderation denial) which IS deterministic on sqlite.
3. Spec §4 says the metadata-only backends (ChromaDB, mem0-client) apply the Section-3 delete/clear decision by fetching entry metadata first. Not implemented in this branch: those backends' `delete_memory` has no scope/author check, and they implement neither `clear_memories` nor `get_conversation_history` (both are outside the `MemoryStore` ABC). This is latent — the proxy hardcodes `backend="pgvector"` (`proxy/apps/proxy_server/main.py`), which fully enforces §3. Tracked follow-up: bring metadata-backend delete/clear enforcement up to §4 before any non-pgvector backend is wired to the mem0 API.

## Global Constraints

- **Branch:** all work on `feature/memory-access-control`, created from `chore/consolidate-quart-k8s` (Task 1 creates it). Never commit to main or the consolidation branch.
- **Scope values are exactly** `"user"` and `"org"` on the wire. The merged-view sentinel `"all"` is internal-only (store method parameter) — never accepted from or returned to clients.
- **Exact error strings** (contract-tested): `"invalid scope"` (400), `"organization memory scope not enabled"` (403), `"not memory author"` (403), `"memory moderation permission required"` (403), `"memory not found"` (404). Existing strings `"organization mismatch"`, `"user mismatch"`, `"no valid organization"` must remain byte-identical.
- **PR #50 tenancy locks preserved verbatim:** org forced from token, org-0 → 403, request `user_id` ≠ token user → 403. Do not restructure those blocks.
- **Feature flag:** key `waddleai.memory-org-scope`, default OFF. Env override `WADDLEAI_FLAG_MEMORY_ORG_SCOPE`. Only org-*writes* are flag-gated; reads and deletes are not.
- **New permission:** `Permission.MEMORY_MODERATE = "memory:moderate"`, granted to `Role.ADMIN` and `Role.RESOURCE_MANAGER` bundles only. Checks test both the enum and the string value (claims-derived permission sets contain strings — see `shared/auth/rbac.py:236-238` pattern).
- **Snapshot discipline:** `tests/contract/conftest.py` and `tests/contract/snapshot.py` must NOT be modified. **No existing snapshot file may change** — the sqlite contract environment fails-closed on all pgvector SQL, so existing mem0 snapshots stay byte-identical; this plan only ADDS snapshot files. If an existing snapshot changes, the task is wrong — stop and report.
- **Alembic is sole schema authority**; next migration number is 006. `create_all()`/model changes mirror the migration, never replace it.
- **Auto-captured memory stays personal:** `MemoryEntry.scope_type` defaults to `"user"`; no internal caller passes `scope_type="org"`.
- **Style:** `python3` always; type hints on every new function; run `python3 -m flake8` and `python3 -m black --check` on changed files before each commit. Match surrounding code style (this codebase does not use `@dataclass(slots=True)` in `memory_integration.py` — follow the file's existing plain `@dataclass`).
- **Commits:** end every commit message with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **All test commands run from repo root** `.`.

---

### Task 1: Feature branch + `memory:moderate` permission

**Files:**
- Create: branch `feature/memory-access-control`
- Commit (already on disk, untracked): `docs/superpowers/specs/2026-07-14-memory-access-control-design.md`
- Modify: `shared/auth/rbac.py` (Permission enum ~line 64; ROLE_PERMISSIONS ~lines 83-144)
- Test: `tests/unit/test_memory_moderate_permission.py` (new)

**Interfaces:**
- Produces: `Permission.MEMORY_MODERATE` with value `"memory:moderate"`; present in `ROLE_PERMISSIONS[Role.ADMIN]` and `ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]`; absent from `Role.REPORTER` and `Role.USER`. Task 6 imports `Permission` in `mem0_api.py` and checks both `Permission.MEMORY_MODERATE in permissions` and `"memory:moderate" in permissions`.

- [ ] **Step 1: Create the branch and commit the spec**

```bash
cd .
git checkout chore/consolidate-quart-k8s
git checkout -b feature/memory-access-control
git add docs/superpowers/specs/2026-07-14-memory-access-control-design.md
git commit -m "docs: add memory access-control design spec (personal vs org scope)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 2: Write the failing test**

Create `tests/unit/test_memory_moderate_permission.py`:

```python
"""Unit tests for the memory:moderate permission (org memory moderation).

Part of the memory access-control feature: org-scoped (shared) memories can
be pruned by their author or by a holder of memory:moderate. See
docs/superpowers/specs/2026-07-14-memory-access-control-design.md Section 3.
"""

from shared.auth.penguin_auth import claims_dict_to_user_context, user_context_to_claims_dict
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext


def test_memory_moderate_permission_exists() -> None:
    assert Permission.MEMORY_MODERATE.value == "memory:moderate"


def test_memory_moderate_granted_to_admin_and_resource_manager() -> None:
    assert Permission.MEMORY_MODERATE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.MEMORY_MODERATE in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]


def test_memory_moderate_not_granted_to_reporter_or_user() -> None:
    assert Permission.MEMORY_MODERATE not in ROLE_PERMISSIONS[Role.REPORTER]
    assert Permission.MEMORY_MODERATE not in ROLE_PERMISSIONS[Role.USER]


def test_memory_moderate_survives_claims_round_trip() -> None:
    """The claims-dict path stores permissions as STRINGS — the middleware
    auth path must still see memory:moderate after round-tripping."""
    uc = UserContext(
        user_id=7,
        username="admin-user",
        role=Role.ADMIN,
        organization_id=3,
        managed_orgs=[],
        permissions=ROLE_PERMISSIONS[Role.ADMIN],
        api_key_id=None,
    )
    rebuilt = claims_dict_to_user_context(user_context_to_claims_dict(uc))
    assert "memory:moderate" in rebuilt.permissions
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/test_memory_moderate_permission.py -v --no-cov`
Expected: FAIL with `AttributeError: MEMORY_MODERATE` (first test errors at collection of the enum attribute).

- [ ] **Step 4: Implement**

In `shared/auth/rbac.py`, inside `class Permission(Enum)`, after the `PROXY_ROUTE = "proxy:route"` line (~line 66), add:

```python
    # Memory (org-scoped shared memory moderation)
    MEMORY_MODERATE = "memory:moderate"
```

In `ROLE_PERMISSIONS`, add `Permission.MEMORY_MODERATE,` to the `Role.ADMIN` set (after `Permission.PROXY_ROUTE,`) and to the `Role.RESOURCE_MANAGER` set (after `Permission.PROXY_USE,`). Do NOT add it to `Role.REPORTER` or `Role.USER`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_moderate_permission.py tests/unit/test_rbac.py tests/unit/test_rbac_additional.py -v --no-cov`
Expected: all PASS (existing rbac tests must not regress).

- [ ] **Step 6: Lint and commit**

```bash
python3 -m flake8 shared/auth/rbac.py tests/unit/test_memory_moderate_permission.py
python3 -m black --check shared/auth/rbac.py tests/unit/test_memory_moderate_permission.py
git add shared/auth/rbac.py tests/unit/test_memory_moderate_permission.py
git commit -m "feat(auth): add memory:moderate permission for org memory moderation

Granted to ADMIN and RESOURCE_MANAGER bundles only. Authorization checks
use the permission value, never role names.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Alembic migration 006 + SQLAlchemy model columns

**Files:**
- Create: `services/management/alembic/versions/006_add_memory_scope.py`
- Modify: `services/management/app/models_sqlalchemy.py:391-404` (`MemoryEmbedding`)
- Test: `tests/unit/management/test_migration_006_memory_scope.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `memory_embeddings.scope_type` (`String(20)`, NOT NULL, server_default `'user'`, indexed) and `memory_embeddings.author_user_id` (`Integer`, NOT NULL after backfill, indexed); composite index `idx_mememb_org_scope` on `(organization_id, scope_type)`. Task 3's pgvector SQL reads/writes these columns.

- [ ] **Step 1: Write the failing migration test**

The test isolates migration 006: it creates the 005-era `memory_embeddings` shape on a temp sqlite DB, stamps alembic at 005, upgrades to head, asserts backfill, then downgrades one step and asserts the columns are gone. (Running the whole 001→head chain on sqlite is not required and not attempted — earlier migrations may use Postgres-specific constructs.)

Create `tests/unit/management/test_migration_006_memory_scope.py`:

```python
"""Migration 006 round-trip test: memory scope columns + backfill.

Creates the pre-006 memory_embeddings shape on a scratch sqlite DB, stamps
alembic at 005, upgrades to head, and verifies the backfill
(scope_type='user', author_user_id=user_id). Then downgrades one revision
and verifies both columns are dropped.
"""

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ALEMBIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "services", "management", "alembic",
)


def _alembic_config(db_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.abspath(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "migration006.db"
    db_url = f"sqlite:///{db_path}"
    # env.py reads DATABASE_URL; point it at the scratch DB
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = sa.create_engine(db_url)
    with engine.begin() as conn:
        # Pre-006 shape of memory_embeddings (005-era)
        conn.execute(sa.text(
            "CREATE TABLE memory_embeddings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, "
            "organization_id INTEGER NOT NULL, "
            "session_id VARCHAR(255) NOT NULL, "
            "content TEXT NOT NULL, "
            "embedding_json TEXT, "
            "role VARCHAR(50) NOT NULL, "
            "created_at DATETIME, "
            "metadata JSON)"
        ))
        conn.execute(sa.text(
            "INSERT INTO memory_embeddings "
            "(user_id, organization_id, session_id, content, role) "
            "VALUES (42, 7, '', 'legacy personal memory', 'user')"
        ))
    yield db_url, engine
    engine.dispose()


def test_upgrade_backfills_scope_and_author(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "005_add_content_filter_tables")
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT scope_type, author_user_id, user_id FROM memory_embeddings"
        )).one()
    assert row.scope_type == "user"
    assert row.author_user_id == 42
    assert row.author_user_id == row.user_id


def test_downgrade_drops_columns(scratch_db):
    db_url, engine = scratch_db
    cfg = _alembic_config(db_url)
    command.stamp(cfg, "005_add_content_filter_tables")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    with engine.connect() as conn:
        cols = {
            r[1] for r in conn.execute(sa.text("PRAGMA table_info(memory_embeddings)"))
        }
    assert "scope_type" not in cols
    assert "author_user_id" not in cols
    # Original columns intact
    assert {"id", "user_id", "organization_id", "content"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_migration_006_memory_scope.py -v --no-cov`
Expected: FAIL — `command.upgrade(cfg, "head")` is a no-op past 005 (revision 006 does not exist), so `SELECT scope_type ...` raises `OperationalError: no such column`.

Note: if `env.py` errors because `DATABASE_URL` handling conflicts with `sqlalchemy.url`, the monkeypatched env var wins in `get_url()` — both point at the same scratch URL, so either source is correct.

- [ ] **Step 3: Write the migration**

Create `services/management/alembic/versions/006_add_memory_scope.py`:

```python
"""Add memory scope columns (personal vs organizational memory).

Adds scope_type ('user' | 'org') and author_user_id to memory_embeddings.
Backfills all existing rows to personal scope with author = owner, so no
memory changes visibility retroactively.

Field names follow the platform spec §9.7 (Memory Scoping & Trust) so the
v0.4.x scope expansion (session/project/repo, trust tiers) extends this
schema without renaming.

Revision ID: 006_add_memory_scope
Revises: 005_add_content_filter_tables
Create Date: 2026-07-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006_add_memory_scope"
down_revision: Union[str, None] = "005_add_content_filter_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with server_default is safe as a single step on both
    # PostgreSQL and SQLite: existing rows take the default.
    op.add_column(
        "memory_embeddings",
        sa.Column("scope_type", sa.String(20), nullable=False, server_default="user"),
    )
    # author_user_id backfills from user_id, so it is added nullable,
    # backfilled, then tightened (batch_alter_table for SQLite compat).
    op.add_column(
        "memory_embeddings",
        sa.Column("author_user_id", sa.Integer, nullable=True),
    )
    op.execute("UPDATE memory_embeddings SET author_user_id = user_id WHERE author_user_id IS NULL")
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.alter_column("author_user_id", existing_type=sa.Integer, nullable=False)

    op.create_index("idx_mememb_scope_type", "memory_embeddings", ["scope_type"])
    op.create_index("idx_mememb_author_user", "memory_embeddings", ["author_user_id"])
    # Composite index keeps the merged-view org branch
    # (organization_id, scope_type='org') cheap.
    op.create_index("idx_mememb_org_scope", "memory_embeddings", ["organization_id", "scope_type"])


def downgrade() -> None:
    op.drop_index("idx_mememb_org_scope", table_name="memory_embeddings")
    op.drop_index("idx_mememb_author_user", table_name="memory_embeddings")
    op.drop_index("idx_mememb_scope_type", table_name="memory_embeddings")
    with op.batch_alter_table("memory_embeddings") as batch_op:
        batch_op.drop_column("author_user_id")
        batch_op.drop_column("scope_type")
```

- [ ] **Step 4: Mirror the columns on the SQLAlchemy model**

In `services/management/app/models_sqlalchemy.py`, inside `class MemoryEmbedding(Base)` after the `metadata_` line (~line 404), add:

```python
    # Memory access-control scope: 'user' (personal, default) | 'org' (shared).
    # Spec §9.7 field names; v0.4 adds more scope values without renaming.
    scope_type = Column(String(20), nullable=False, default="user", server_default="user", index=True)
    author_user_id = Column(Integer, nullable=False, index=True)
```

(`author_user_id` has no default — application code always sets it. Alembic 006 is what backfills legacy rows.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/management/test_migration_006_memory_scope.py -v --no-cov`
Expected: 2 PASS.

- [ ] **Step 6: Lint and commit**

```bash
python3 -m flake8 services/management/alembic/versions/006_add_memory_scope.py services/management/app/models_sqlalchemy.py tests/unit/management/test_migration_006_memory_scope.py
python3 -m black --check services/management/alembic/versions/006_add_memory_scope.py tests/unit/management/test_migration_006_memory_scope.py
git add services/management/alembic/versions/006_add_memory_scope.py services/management/app/models_sqlalchemy.py tests/unit/management/test_migration_006_memory_scope.py
git commit -m "feat(db): migration 006 — memory scope_type + author_user_id with personal backfill

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: MemoryEntry fields + PgvectorMemoryStore scope support

**Files:**
- Modify: `shared/utils/memory_integration.py` — `MemoryEntry` (~line 31), `MemoryStore.search_memories` ABC (~line 72), `PgvectorMemoryStore.store_memory` (~line 1102), `.search_memories` (~line 1131), `.get_conversation_history` (~line 1210), `.clear_memories` (~line 1261)
- Test: `tests/unit/test_memory_scope_pgvector.py` (new)

**Interfaces:**
- Consumes: `memory_embeddings.scope_type` / `.author_user_id` columns (Task 2).
- Produces (Task 6 relies on these exact signatures):
  - `MemoryEntry` gains `scope_type: str = "user"` and `author_user_id: int = 0` (0 = "same as user_id", resolved at store time).
  - `search_memories(..., scope: str = "user")` — `scope` ∈ `{"user", "org", "all"}`; `"all"` = merged personal+org.
  - `get_conversation_history(..., scope: str = "user")` — same values.
  - `clear_memories(user_id, organization_id, session_id=None, scope="user", org_all=False)` — `scope="user"`: personal rows only; `scope="org", org_all=False`: org rows authored by `user_id`; `scope="org", org_all=True`: all org rows.
  - Stored `metadata` JSON always contains a `"scope"` mirror key.
  - `MemoryEntry` objects returned by search/history carry populated `scope_type` and `author_user_id`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_memory_scope_pgvector.py`:

```python
"""Unit tests for PgvectorMemoryStore scope support (personal vs org memory).

The store builds raw SQL against memory_embeddings; these tests capture the
SQL and parameters through a fake DAL and assert the scope branches, the
metadata scope mirror, and MemoryEntry field population.
"""

import json
from datetime import datetime
from typing import Any, List, Optional, Tuple

from shared.utils.memory_integration import MemoryEntry, PgvectorMemoryStore


class FakeDB:
    """Captures executesql calls; returns queued results."""

    def __init__(self, results: Optional[List[Any]] = None) -> None:
        self.calls: List[Tuple[str, tuple]] = []
        self._results = results or []

    def executesql(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, tuple(params) if params else ()))
        return self._results.pop(0) if self._results else []


class FakeEmbedder:
    def embed(self, text: str) -> List[float]:
        return [0.1, 0.2, 0.3]


def _store(results: Optional[List[Any]] = None) -> Tuple[PgvectorMemoryStore, FakeDB]:
    db = FakeDB(results)
    return PgvectorMemoryStore(write_db=db, embedding_manager=FakeEmbedder()), db


def _entry(scope_type: str = "user", author: int = 0) -> MemoryEntry:
    return MemoryEntry(
        id="",
        user_id=5,
        organization_id=3,
        session_id="s1",
        content="remember the deploy runbook",
        metadata={"role": "user"},
        embedding=None,
        created_at=datetime.utcnow(),
        scope_type=scope_type,
        author_user_id=author,
    )


# --- store_memory ----------------------------------------------------------

async def test_store_memory_defaults_to_personal_scope() -> None:
    store, db = _store()
    ok = await store.store_memory(_entry())
    assert ok is True
    sql, params = db.calls[0]
    assert "scope_type" in sql and "author_user_id" in sql
    # scope_type param
    assert "user" in params
    # author defaults to the entry's user_id when author_user_id == 0
    assert 5 in params


async def test_store_memory_org_scope_writes_column_and_metadata_mirror() -> None:
    store, db = _store()
    ok = await store.store_memory(_entry(scope_type="org", author=5))
    assert ok is True
    sql, params = db.calls[0]
    assert "org" in params
    meta_param = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(meta_param)["scope"] == "org"


async def test_store_memory_personal_metadata_mirror() -> None:
    store, db = _store()
    await store.store_memory(_entry())
    _, params = db.calls[0]
    meta_param = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(meta_param)["scope"] == "user"


# --- search_memories -------------------------------------------------------

def _search_row(scope_type: str = "user", author: int = 5) -> tuple:
    # id, user_id, organization_id, session_id, content, role,
    # created_at, metadata, scope_type, author_user_id, similarity
    return (
        11, 5, 3, "s1", "remembered", "user",
        datetime.utcnow(), json.dumps({"scope": scope_type}), scope_type, author, 0.91,
    )


async def test_search_scope_user_filters_to_owner() -> None:
    store, db = _store(results=[[_search_row()]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="user")
    sql, params = db.calls[0]
    assert "scope_type = 'user' AND user_id = %s" in sql
    assert "scope_type = 'org' OR" not in sql
    assert entries[0].scope_type == "user"
    assert entries[0].author_user_id == 5


async def test_search_scope_org_has_no_user_filter() -> None:
    store, db = _store(results=[[_search_row("org", 9)]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="org")
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "user_id = %s" not in sql
    assert entries[0].scope_type == "org"
    assert entries[0].author_user_id == 9


async def test_search_scope_all_is_merged_or_branch() -> None:
    store, db = _store(results=[[_search_row(), _search_row("org", 9)]])
    entries = await store.search_memories("q", user_id=5, organization_id=3, scope="all")
    sql, params = db.calls[0]
    assert "(scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))" in sql
    assert len(entries) == 2


async def test_search_default_scope_is_user() -> None:
    """Internal callers that never pass scope keep today's personal-only behavior."""
    store, db = _store(results=[[]])
    await store.search_memories("q", user_id=5, organization_id=3)
    sql, _ = db.calls[0]
    assert "scope_type = 'user' AND user_id = %s" in sql


# --- get_conversation_history ---------------------------------------------

async def test_history_scope_all_merged() -> None:
    store, db = _store(results=[[]])
    await store.get_conversation_history(user_id=5, organization_id=3, session_id="s1", scope="all")
    sql, params = db.calls[0]
    assert "(scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))" in sql
    assert "organization_id = %s" in sql


# --- clear_memories ---------------------------------------------------------

async def test_clear_default_personal_only() -> None:
    store, db = _store()
    ok = await store.clear_memories(user_id=5, organization_id=3)
    assert ok is True
    sql, params = db.calls[0]
    assert "scope_type = 'user'" in sql
    assert "user_id = %s" in sql


async def test_clear_org_author_only() -> None:
    store, db = _store()
    await store.clear_memories(user_id=5, organization_id=3, scope="org")
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "author_user_id = %s" in sql


async def test_clear_org_all_has_no_author_filter() -> None:
    store, db = _store()
    await store.clear_memories(user_id=5, organization_id=3, scope="org", org_all=True)
    sql, params = db.calls[0]
    assert "scope_type = 'org'" in sql
    assert "author_user_id" not in sql
    assert "user_id = %s" not in sql
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_memory_scope_pgvector.py -v --no-cov`
Expected: FAIL — `MemoryEntry.__init__() got an unexpected keyword argument 'scope_type'`.

- [ ] **Step 3: Implement — MemoryEntry + ABC**

In `shared/utils/memory_integration.py`, `MemoryEntry` (~line 31), add two fields after `relevance_score`:

```python
@dataclass
class MemoryEntry:
    """Memory entry structure"""

    id: str
    user_id: int
    organization_id: int
    session_id: Optional[str]
    content: str
    metadata: Dict[str, Any]
    embedding: Optional[List[float]]
    created_at: datetime
    relevance_score: float = 0.0
    # Access-control scope: 'user' (personal, default) | 'org' (shared).
    scope_type: str = "user"
    # Who wrote it; 0 means "same as user_id" (resolved at store time).
    author_user_id: int = 0
```

In the `MemoryStore` ABC, change `search_memories` signature (~line 72) to add the trailing parameter:

```python
    @abstractmethod
    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: Optional[str] = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> List[MemoryEntry]:
        """Search for relevant memories.

        scope: 'user' (caller's personal rows), 'org' (org-shared rows),
        'all' (merged personal + org, relevance-ranked).
        """
        pass
```

(Tasks 3 and 4 update all three implementations; do NOT commit between them in a broken state — Task 3 updates Pgvector, and adds the parameter as accepted-but-personal-only passthrough is not needed because Task 4 lands before any org caller exists. Within this task, also add `scope: str = "user"` to `Mem0MemoryStore.search_memories` and `ChromaDBMemoryStore.search_memories` signatures with no behavior change yet, so the ABC change doesn't break instantiation; Task 4 implements their behavior.)

- [ ] **Step 4: Implement — PgvectorMemoryStore**

Replace `store_memory` (~line 1102) body's INSERT block:

```python
    async def store_memory(self, entry: MemoryEntry) -> bool:
        """Store a memory entry, generating its embedding vector.

        Always writes to the primary (write_db). Writes the authoritative
        scope_type/author_user_id columns AND mirrors the scope into the
        metadata JSON so metadata-only backends/clients see it too.
        """
        try:
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(None, self.embedding_manager.embed, entry.content)
            embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

            author_id = entry.author_user_id or entry.user_id
            metadata = {**entry.metadata, "scope": entry.scope_type}

            self.write_db.executesql(
                "INSERT INTO memory_embeddings "
                "(user_id, organization_id, session_id, content, embedding, role, metadata, "
                "scope_type, author_user_id) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s, %s::jsonb, %s, %s)",
                (
                    entry.user_id,
                    entry.organization_id,
                    entry.session_id or "",
                    entry.content,
                    embedding_str,
                    entry.metadata.get("role", "user"),
                    json.dumps(metadata),
                    entry.scope_type,
                    author_id,
                ),
            )
            return True
        except Exception as exc:
            logger.error("PgvectorMemoryStore.store_memory failed: %s", exc)
            return False
```

Replace `search_memories` (~line 1131):

```python
    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: Optional[str] = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> List[MemoryEntry]:
        """Search for relevant memories using cosine similarity.

        scope='user' returns the caller's personal rows (default — preserves
        pre-scope behavior for internal callers); 'org' returns org-shared
        rows; 'all' returns the merged view in one indexed query, ranked
        purely by relevance.
        """
        try:
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(None, self.embedding_manager.embed, query)
            embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"

            read_db = self._read_db()

            params: list = [embedding_str, organization_id]
            if scope == "org":
                scope_filter = " AND scope_type = 'org'"
            elif scope == "all":
                scope_filter = " AND (scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))"
                params.append(user_id)
            else:
                scope_filter = " AND scope_type = 'user' AND user_id = %s"
                params.append(user_id)

            session_filter = ""
            if session_id:
                session_filter = " AND session_id = %s"
                params.append(session_id)

            params.extend([embedding_str, min_relevance, embedding_str, limit])

            sql = (
                "SELECT id, user_id, organization_id, session_id, content, role, "
                "created_at, metadata, scope_type, author_user_id, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM memory_embeddings "
                "WHERE organization_id = %s" + scope_filter + session_filter + " AND embedding IS NOT NULL "
                "AND 1 - (embedding <=> %s::vector) >= %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s"
            )

            rows = read_db.executesql(sql, params)
            if not rows:
                return []

            entries = []
            for row in rows:
                (
                    row_id, uid, org_id, sess_id, content, role,
                    created_at, metadata_raw, scope_type, author_uid, similarity,
                ) = row

                try:
                    meta = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
                except (json.JSONDecodeError, TypeError):
                    meta = {}

                meta["role"] = role
                entries.append(
                    MemoryEntry(
                        id=str(row_id),
                        user_id=uid,
                        organization_id=org_id,
                        session_id=sess_id,
                        content=content,
                        metadata=meta,
                        embedding=None,
                        created_at=created_at if isinstance(created_at, datetime) else datetime.utcnow(),
                        relevance_score=float(similarity),
                        scope_type=scope_type or "user",
                        author_user_id=int(author_uid or uid),
                    )
                )
            return entries

        except Exception as exc:
            logger.error("PgvectorMemoryStore.search_memories failed: %s", exc)
            return []
```

Replace `get_conversation_history` (~line 1210):

```python
    async def get_conversation_history(
        self,
        user_id: int,
        organization_id: int,
        session_id: str,
        limit: int = 20,
        scope: str = "user",
    ) -> List[MemoryEntry]:
        """Retrieve recent conversation history ordered by time (no vector search).

        scope semantics match search_memories ('user' default | 'org' | 'all').
        """
        try:
            read_db = self._read_db()

            params: list = [organization_id]
            if scope == "org":
                scope_filter = " AND scope_type = 'org'"
            elif scope == "all":
                scope_filter = " AND (scope_type = 'org' OR (scope_type = 'user' AND user_id = %s))"
                params.append(user_id)
            else:
                scope_filter = " AND scope_type = 'user' AND user_id = %s"
                params.append(user_id)

            params.extend([session_id, limit])

            rows = read_db.executesql(
                "SELECT id, user_id, organization_id, session_id, content, role, "
                "created_at, metadata, scope_type, author_user_id "
                "FROM memory_embeddings "
                "WHERE organization_id = %s" + scope_filter + " AND session_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                params,
            )
            if not rows:
                return []

            entries = []
            for row in rows:
                (
                    row_id, uid, org_id, sess_id, content, role,
                    created_at, metadata_raw, scope_type, author_uid,
                ) = row
                try:
                    meta = json.loads(metadata_raw) if isinstance(metadata_raw, str) else (metadata_raw or {})
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                meta["role"] = role
                entries.append(
                    MemoryEntry(
                        id=str(row_id),
                        user_id=uid,
                        organization_id=org_id,
                        session_id=sess_id,
                        content=content,
                        metadata=meta,
                        embedding=None,
                        created_at=created_at if isinstance(created_at, datetime) else datetime.utcnow(),
                        relevance_score=1.0,
                        scope_type=scope_type or "user",
                        author_user_id=int(author_uid or uid),
                    )
                )
            return entries

        except Exception as exc:
            logger.error("PgvectorMemoryStore.get_conversation_history failed: %s", exc)
            return []
```

Replace `clear_memories` (~line 1261):

```python
    async def clear_memories(
        self,
        user_id: int,
        organization_id: int,
        session_id: Optional[str] = None,
        scope: str = "user",
        org_all: bool = False,
    ) -> bool:
        """Delete memories. Always writes to primary.

        scope='user' (default): the caller's personal rows only — an
        unscoped clear must never remove shared org knowledge.
        scope='org', org_all=False: org rows AUTHORED by the caller.
        scope='org', org_all=True: all org rows (moderator-gated upstream).
        """
        try:
            if scope == "org" and org_all:
                where = "scope_type = 'org' AND organization_id = %s"
                params: list = [organization_id]
            elif scope == "org":
                where = "scope_type = 'org' AND author_user_id = %s AND organization_id = %s"
                params = [user_id, organization_id]
            else:
                where = "scope_type = 'user' AND user_id = %s AND organization_id = %s"
                params = [user_id, organization_id]

            if session_id:
                where += " AND session_id = %s"
                params.append(session_id)

            self.write_db.executesql(
                "DELETE FROM memory_embeddings WHERE " + where,
                params,
            )
            return True
        except Exception as exc:
            logger.error("PgvectorMemoryStore.clear_memories failed: %s", exc)
            return False
```

Also add `scope: str = "user"` (parameter only, no behavior change — Task 4 implements) to `Mem0MemoryStore.search_memories` (~line 157) and `ChromaDBMemoryStore.search_memories` (~line 392) signatures so all concrete classes still satisfy the ABC.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_scope_pgvector.py tests/unit/test_memory_integration.py -v --no-cov`
Expected: new tests PASS; `test_memory_integration.py` remains skipped or passing exactly as before this task (it module-skips on missing legacy names — pre-existing behavior, do not "fix" it here).

- [ ] **Step 6: Lint and commit**

```bash
python3 -m flake8 shared/utils/memory_integration.py tests/unit/test_memory_scope_pgvector.py
python3 -m black --check shared/utils/memory_integration.py tests/unit/test_memory_scope_pgvector.py
git add shared/utils/memory_integration.py tests/unit/test_memory_scope_pgvector.py
git commit -m "feat(memory): scope-aware pgvector store — columns, metadata mirror, merged view

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Metadata-only backends (ChromaDB + mem0 client) scope support

**Files:**
- Modify: `shared/utils/memory_integration.py` — `Mem0MemoryStore.store_memory` (~line 131), `.search_memories` (~line 157), `ChromaDBMemoryStore.store_memory` (~line 357), `.search_memories` (~line 392)
- Test: `tests/unit/test_memory_scope_metadata_backends.py` (new)

**Interfaces:**
- Consumes: `MemoryEntry.scope_type` / `.author_user_id` (Task 3); ABC `scope` parameter (Task 3).
- Produces: both backends honor `scope` ∈ `{"user","org","all"}` on `search_memories` via a **two-query merge** (personal query + org query, merged by relevance, truncated to `limit`). Stored metadata always carries `"scope"` and `"author_user_id"`. **Absent `scope` key in stored metadata = personal** (legacy entries need no backfill). Mem0 backend stores org entries under the synthetic mem0 user key `f"org-{organization_id}"` (the mem0 SaaS API is user-keyed; this is the only way org-wide retrieval works there).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_memory_scope_metadata_backends.py`:

```python
"""Scope support on the metadata-only memory backends (ChromaDB, mem0 client).

These backends have no schema: metadata['scope'] IS the scope, and an absent
key means personal (covers all legacy entries with zero backfill). Reads use
a two-query merge (personal + org) ranked by relevance.

Includes a real-ChromaDB (ephemeral, fake encoder) visibility test — the
core isolation regression: personal memories are invisible to another user
in the same org; org memories are visible.
# regression: same-org personal-memory isolation under org scope feature
"""

from datetime import datetime
from typing import List
from unittest.mock import Mock, patch

import pytest

from shared.utils.memory_integration import ChromaDBMemoryStore, MemoryEntry


def _entry(user_id: int, content: str, scope_type: str = "user", entry_id: str = "") -> MemoryEntry:
    return MemoryEntry(
        id=entry_id or f"m-{user_id}-{content[:8]}",
        user_id=user_id,
        organization_id=3,
        session_id="",
        content=content,
        metadata={"role": "user"},
        embedding=None,
        created_at=datetime.utcnow(),
        scope_type=scope_type,
        author_user_id=user_id,
    )


class FakeEncoder:
    """Deterministic 'embeddings' — identical vectors so every stored doc
    is a perfect match and visibility is decided purely by the where filters."""

    def encode(self, text: str, convert_to_tensor: bool = False) -> List[float]:
        return [1.0, 0.0, 0.0]


@pytest.fixture
def chroma_store(tmp_path):
    with patch("shared.utils.memory_integration.SentenceTransformer"):
        store = ChromaDBMemoryStore(
            persist_directory=str(tmp_path / "chroma"),
            collection_name="scope_test",
        )
    store.encoder = FakeEncoder()
    return store


# --- ChromaDB: real store/search visibility --------------------------------

async def test_chroma_store_writes_scope_and_author_metadata(chroma_store):
    await chroma_store.initialize()
    assert await chroma_store.store_memory(_entry(5, "org runbook", scope_type="org"))
    got = chroma_store.collection.get(include=["metadatas"])
    meta = got["metadatas"][0]
    assert meta["scope"] == "org"
    assert meta["author_user_id"] == 5


async def test_chroma_personal_invisible_to_other_user_org_visible(chroma_store):
    """User 5 stores one personal + one org memory. User 6 (same org) must
    see the org memory and must NOT see the personal one."""
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    merged = await chroma_store.search_memories(
        "anything", user_id=6, organization_id=3, min_relevance=0.0, scope="all"
    )
    contents = [m.content for m in merged]
    assert "team deploy runbook" in contents
    assert "my private note" not in contents
    org_entry = next(m for m in merged if m.content == "team deploy runbook")
    assert org_entry.scope_type == "org"
    assert org_entry.author_user_id == 5


async def test_chroma_owner_merged_view_no_duplicates(chroma_store):
    """The author matches both the personal and org buckets — the org row
    must not appear twice in the merged view."""
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    merged = await chroma_store.search_memories(
        "anything", user_id=5, organization_id=3, min_relevance=0.0, scope="all"
    )
    contents = sorted(m.content for m in merged)
    assert contents == ["my private note", "team deploy runbook"]


async def test_chroma_scope_user_excludes_org_rows(chroma_store):
    await chroma_store.initialize()
    await chroma_store.store_memory(_entry(5, "my private note", scope_type="user"))
    await chroma_store.store_memory(_entry(5, "team deploy runbook", scope_type="org"))

    personal = await chroma_store.search_memories(
        "anything", user_id=5, organization_id=3, min_relevance=0.0, scope="user"
    )
    assert [m.content for m in personal] == ["my private note"]


async def test_chroma_legacy_entry_without_scope_key_is_personal(chroma_store):
    """Entries stored before this feature have no metadata['scope'] key —
    they must behave as personal."""
    await chroma_store.initialize()
    chroma_store.collection.add(
        ids=["legacy-1"],
        documents=["pre-feature memory"],
        metadatas=[{
            "user_id": 5, "organization_id": 3, "session_id": "",
            "created_at": datetime.utcnow().isoformat(),
        }],
        embeddings=[[1.0, 0.0, 0.0]],
    )
    other = await chroma_store.search_memories(
        "anything", user_id=6, organization_id=3, min_relevance=0.0, scope="all"
    )
    assert "pre-feature memory" not in [m.content for m in other]

    owner = await chroma_store.search_memories(
        "anything", user_id=5, organization_id=3, min_relevance=0.0, scope="all"
    )
    got = next(m for m in owner if m.content == "pre-feature memory")
    assert got.scope_type == "user"


# --- Mem0MemoryStore: mocked client ----------------------------------------

def _mem0_store():
    from shared.utils.memory_integration import Mem0MemoryStore
    with patch("shared.utils.memory_integration.HAS_MEM0", True):
        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
    store.api_key = None
    store.org_id = None
    store.config = {}
    store.client = Mock()
    return store


async def test_mem0_org_entry_stored_under_synthetic_org_user():
    store = _mem0_store()
    await store.store_memory(_entry(5, "team runbook", scope_type="org"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "org-3"
    assert kwargs["metadata"]["scope"] == "org"
    assert kwargs["metadata"]["author_user_id"] == 5


async def test_mem0_personal_entry_stored_under_real_user():
    store = _mem0_store()
    await store.store_memory(_entry(5, "my note", scope_type="user"))
    _, kwargs = store.client.add.call_args
    assert kwargs["user_id"] == "5"
    assert kwargs["metadata"]["scope"] == "user"


async def test_mem0_search_all_queries_both_buckets_and_merges():
    store = _mem0_store()

    def fake_search(query, user_id, limit):
        if user_id == "5":
            return [{
                "id": "p1", "memory": "my note", "score": 0.8,
                "metadata": {"organization_id": 3, "session_id": "",
                             "created_at": datetime.utcnow().isoformat(),
                             "memory_id": "p1", "scope": "user", "author_user_id": 5},
            }]
        if user_id == "org-3":
            return [{
                "id": "o1", "memory": "team runbook", "score": 0.9,
                "metadata": {"organization_id": 3, "session_id": "",
                             "created_at": datetime.utcnow().isoformat(),
                             "memory_id": "o1", "scope": "org", "author_user_id": 9},
            }]
        return []

    store.client.search.side_effect = fake_search
    results = await store.search_memories("q", user_id=5, organization_id=3, min_relevance=0.0, scope="all")
    assert [m.content for m in results] == ["team runbook", "my note"]  # relevance-ranked
    assert store.client.search.call_count == 2
    assert results[0].scope_type == "org"
    assert results[0].author_user_id == 9


async def test_mem0_search_user_scope_single_query_excludes_org():
    store = _mem0_store()
    store.client.search.return_value = []
    await store.search_memories("q", user_id=5, organization_id=3, scope="user")
    assert store.client.search.call_count == 1
    _, kwargs = store.client.search.call_args
    assert kwargs["user_id"] == "5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_memory_scope_metadata_backends.py -v --no-cov`
Expected: FAIL — chroma metadata lacks `scope`/`author_user_id` keys; mem0 `add` called with `user_id="5"` for org entries; single-query search.

- [ ] **Step 3: Implement — ChromaDBMemoryStore**

In `ChromaDBMemoryStore.store_memory` (~line 357), replace the metadata block:

```python
            # Prepare metadata — 'scope' is the authoritative scope marker on
            # this schemaless backend (absent key == personal/legacy).
            metadata = {
                **entry.metadata,
                "user_id": entry.user_id,
                "organization_id": entry.organization_id,
                "session_id": entry.session_id or "",
                "created_at": entry.created_at.isoformat(),
                "content_length": len(entry.content),
                "scope": entry.scope_type,
                "author_user_id": entry.author_user_id or entry.user_id,
            }
```

Replace `ChromaDBMemoryStore.search_memories` (~line 392) with the two-query merge:

```python
    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: Optional[str] = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> List[MemoryEntry]:
        """Search for relevant memories.

        Two-query merge: the personal bucket (user_id match, post-filtered to
        exclude org rows) and the org bucket (scope=='org' within the org).
        scope='user' | 'org' selects one bucket; 'all' merges both by
        relevance and truncates to limit. Chroma's where-filter language
        cannot express "key absent or != value", so the org-row exclusion in
        the personal bucket is a Python post-filter.
        """
        try:
            if not self.collection:
                await self.initialize()

            query_embedding = self._generate_embedding(query)

            def _run_query(where_clause: dict) -> list:
                if session_id:
                    where_clause = {**where_clause, "session_id": session_id}
                if query_embedding:
                    return self.collection.query(
                        query_embeddings=[query_embedding],
                        where=where_clause,
                        n_results=limit,
                        include=["documents", "metadatas", "distances"],
                    )
                return self.collection.query(
                    query_texts=[query],
                    where=where_clause,
                    n_results=limit,
                    include=["documents", "metadatas", "distances"],
                )

            def _to_entries(results: dict, personal_bucket: bool) -> List[MemoryEntry]:
                memories: List[MemoryEntry] = []
                if not results or not results["documents"]:
                    return memories
                for i in range(len(results["documents"][0])):
                    metadata = results["metadatas"][0][i]
                    entry_scope = metadata.get("scope", "user")
                    if personal_bucket and entry_scope == "org":
                        # Author's own org rows come from the org bucket —
                        # skipping here prevents merged-view duplicates.
                        continue
                    distance = results["distances"][0][i] if results.get("distances") else 0.0
                    relevance_score = 1.0 - distance
                    if relevance_score < min_relevance:
                        continue
                    memories.append(
                        MemoryEntry(
                            id=results["ids"][0][i],
                            user_id=metadata["user_id"],
                            organization_id=metadata["organization_id"],
                            session_id=metadata.get("session_id"),
                            content=results["documents"][0][i],
                            metadata={
                                k: v
                                for k, v in metadata.items()
                                if k not in ["user_id", "organization_id", "session_id", "created_at"]
                            },
                            embedding=None,
                            created_at=datetime.fromisoformat(metadata["created_at"]),
                            relevance_score=relevance_score,
                            scope_type=entry_scope,
                            author_user_id=int(metadata.get("author_user_id", metadata["user_id"])),
                        )
                    )
                return memories

            memories: List[MemoryEntry] = []
            if scope in ("user", "all"):
                personal = _run_query({"user_id": user_id, "organization_id": organization_id})
                memories.extend(_to_entries(personal, personal_bucket=True))
            if scope in ("org", "all"):
                org = _run_query({"organization_id": organization_id, "scope": "org"})
                memories.extend(_to_entries(org, personal_bucket=False))

            memories.sort(key=lambda m: m.relevance_score, reverse=True)
            return memories[:limit]

        except Exception as e:
            logger.error(f"Failed to search memories: {e}")
            return []
```

Note: chroma multi-key `where` dicts follow the file's existing pattern (implicit AND) — if the installed chromadb version requires explicit `$and`, wrap the multi-key dicts the same way for BOTH queries and keep the tests green; do not change test assertions.

- [ ] **Step 4: Implement — Mem0MemoryStore**

In `Mem0MemoryStore.store_memory` (~line 131), replace the metadata + add call:

```python
            # Prepare metadata — 'scope' mirror + author for the schemaless backend
            metadata = {
                **entry.metadata,
                "user_id": entry.user_id,
                "organization_id": entry.organization_id,
                "session_id": entry.session_id or "",
                "created_at": entry.created_at.isoformat(),
                "memory_id": entry.id,
                "scope": entry.scope_type,
                "author_user_id": entry.author_user_id or entry.user_id,
            }

            # mem0's API is user-keyed: org-shared entries live under a
            # synthetic per-org user so any org member can retrieve them.
            mem0_user = f"org-{entry.organization_id}" if entry.scope_type == "org" else str(entry.user_id)
            self.client.add(entry.content, user_id=mem0_user, metadata=metadata)
```

Replace `Mem0MemoryStore.search_memories` (~line 157) with the two-query merge:

```python
    async def search_memories(
        self,
        query: str,
        user_id: int,
        organization_id: int,
        session_id: Optional[str] = None,
        limit: int = 10,
        min_relevance: float = 0.7,
        scope: str = "user",
    ) -> List[MemoryEntry]:
        """Search memories in mem0.

        mem0's search is user-keyed, so the merged view ('all') issues two
        queries — the caller's personal bucket and the synthetic org bucket
        ('org-{organization_id}') — and merges by relevance score.
        """
        try:
            if not self.client:
                await self.initialize()

            def _convert(results: list, personal_bucket: bool) -> List[MemoryEntry]:
                memories: List[MemoryEntry] = []
                for result in results:
                    metadata = result.get("metadata", {})
                    entry_scope = metadata.get("scope", "user")
                    if personal_bucket and entry_scope == "org":
                        continue  # org rows come from the org bucket only
                    if metadata.get("organization_id") != organization_id:
                        continue
                    if session_id and metadata.get("session_id") != session_id:
                        continue
                    relevance_score = result.get("score", 0.0)
                    if relevance_score < min_relevance:
                        continue
                    memories.append(
                        MemoryEntry(
                            id=metadata.get("memory_id", result.get("id", "")),
                            user_id=metadata.get("user_id", user_id),
                            organization_id=organization_id,
                            session_id=metadata.get("session_id"),
                            content=result.get("memory", ""),
                            metadata={
                                k: v
                                for k, v in metadata.items()
                                if k not in ["user_id", "organization_id", "session_id", "created_at", "memory_id"]
                            },
                            embedding=None,
                            created_at=datetime.fromisoformat(
                                metadata.get("created_at", datetime.utcnow().isoformat())
                            ),
                            relevance_score=relevance_score,
                            scope_type=entry_scope,
                            author_user_id=int(metadata.get("author_user_id", metadata.get("user_id", user_id))),
                        )
                    )
                return memories

            memories: List[MemoryEntry] = []
            if scope in ("user", "all"):
                personal = self.client.search(query, user_id=str(user_id), limit=limit)
                memories.extend(_convert(personal, personal_bucket=True))
            if scope in ("org", "all"):
                org = self.client.search(query, user_id=f"org-{organization_id}", limit=limit)
                memories.extend(_convert(org, personal_bucket=False))

            memories.sort(key=lambda m: m.relevance_score, reverse=True)
            return memories[:limit]

        except Exception as e:
            logger.error(f"Failed to search memories in mem0: {e}")
            return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_memory_scope_metadata_backends.py tests/unit/test_memory_scope_pgvector.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 6: Lint and commit**

```bash
python3 -m flake8 shared/utils/memory_integration.py tests/unit/test_memory_scope_metadata_backends.py
python3 -m black --check shared/utils/memory_integration.py tests/unit/test_memory_scope_metadata_backends.py
git add shared/utils/memory_integration.py tests/unit/test_memory_scope_metadata_backends.py
git commit -m "feat(memory): scope support on metadata backends — two-query merge, org bucket, legacy=personal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Feature-flag helper (`waddleai.memory-org-scope`)

**Files:**
- Create: `shared/utils/feature_flags.py`
- Test: `tests/unit/test_feature_flags.py` (new)

**Interfaces:**
- Consumes: nothing from other tasks. `posthog==7.9.12` is already in the pinned requirements (transitive) — no requirements change.
- Produces: `is_feature_enabled(flag_key: str, distinct_id: str = "server", default: bool = False) -> bool`. Task 6 calls `is_feature_enabled("waddleai.memory-org-scope", distinct_id=str(token_org))`. Env override name: `WADDLEAI_FLAG_` + flag key with the `waddleai.` product prefix stripped, `-`/`.` → `_`, uppercased → `WADDLEAI_FLAG_MEMORY_ORG_SCOPE`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_feature_flags.py`:

```python
"""Unit tests for the PostHog-backed feature flag helper.

House rule: every feature ships behind a flag, default OFF, with graceful
degradation — flag-server failure falls back to the default, never raises.
The env override (WADDLEAI_FLAG_*) is the test/alpha mechanism.
"""

from unittest.mock import Mock, patch

import shared.utils.feature_flags as ff
from shared.utils.feature_flags import is_feature_enabled


def setup_function() -> None:
    # Reset the cached client between tests
    ff._posthog_client = None


def test_default_off_when_no_env_and_no_posthog(monkeypatch) -> None:
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.delenv("POSTHOG_KEY", raising=False)
    assert is_feature_enabled("waddleai.memory-org-scope") is False


def test_env_override_on(monkeypatch) -> None:
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "1")
    assert is_feature_enabled("waddleai.memory-org-scope") is True


def test_env_override_off_beats_posthog(monkeypatch) -> None:
    monkeypatch.setenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", "false")
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    assert is_feature_enabled("waddleai.memory-org-scope") is False


def test_posthog_result_used_when_configured(monkeypatch) -> None:
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.return_value = True
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope", distinct_id="3") is True
    fake.feature_enabled.assert_called_once_with("waddleai.memory-org-scope", "3")


def test_posthog_failure_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("WADDLEAI_FLAG_MEMORY_ORG_SCOPE", raising=False)
    monkeypatch.setenv("POSTHOG_KEY", "phc_test")
    fake = Mock()
    fake.feature_enabled.side_effect = RuntimeError("posthog down")
    with patch.object(ff, "_get_posthog_client", return_value=fake):
        assert is_feature_enabled("waddleai.memory-org-scope") is False
        assert is_feature_enabled("waddleai.memory-org-scope", default=True) is True


def test_env_name_derivation() -> None:
    assert ff._env_var_name("waddleai.memory-org-scope") == "WADDLEAI_FLAG_MEMORY_ORG_SCOPE"
    assert ff._env_var_name("waddleai.security-v2") == "WADDLEAI_FLAG_SECURITY_V2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_feature_flags.py -v --no-cov`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.utils.feature_flags'`.

- [ ] **Step 3: Implement**

Create `shared/utils/feature_flags.py`:

```python
"""PostHog-backed feature flags with graceful degradation.

Evaluation order:
  1. Environment override ``WADDLEAI_FLAG_<NAME>`` ("1"/"true"/"yes"/"on"
     enables; anything else disables) — used by tests and alpha.
  2. PostHog, when ``POSTHOG_KEY`` is configured (host defaults to the
     centralized license server).
  3. The caller-supplied default (OFF for new flags, per house rules).

Any PostHog failure falls back to the default — flag evaluation must never
raise into request handling.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_posthog_client: Optional[object] = None

_TRUTHY = ("1", "true", "yes", "on")


def _env_var_name(flag_key: str) -> str:
    """waddleai.memory-org-scope -> WADDLEAI_FLAG_MEMORY_ORG_SCOPE."""
    suffix = flag_key.split(".", 1)[-1]
    return "WADDLEAI_FLAG_" + suffix.replace("-", "_").replace(".", "_").upper()


def _get_posthog_client() -> Optional[object]:
    """Lazily construct and cache the PostHog client (None if unconfigured)."""
    global _posthog_client
    api_key = os.getenv("POSTHOG_KEY")
    if not api_key:
        return None
    if _posthog_client is None:
        from posthog import Posthog

        _posthog_client = Posthog(
            api_key,
            host=os.getenv("POSTHOG_HOST", "https://license.penguintech.io"),
        )
    return _posthog_client


def is_feature_enabled(flag_key: str, distinct_id: str = "server", default: bool = False) -> bool:
    """Evaluate a feature flag. Never raises; falls back to ``default``."""
    env_val = os.getenv(_env_var_name(flag_key))
    if env_val is not None:
        return env_val.strip().lower() in _TRUTHY

    try:
        client = _get_posthog_client()
        if client is None:
            return default
        result = client.feature_enabled(flag_key, distinct_id)
        return default if result is None else bool(result)
    except Exception as exc:
        logger.warning("Feature flag %s evaluation failed, using default=%s: %s", flag_key, default, exc)
        return default
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_feature_flags.py -v --no-cov`
Expected: 6 PASS.

- [ ] **Step 5: Lint and commit**

```bash
python3 -m flake8 shared/utils/feature_flags.py tests/unit/test_feature_flags.py
python3 -m black --check shared/utils/feature_flags.py tests/unit/test_feature_flags.py
git add shared/utils/feature_flags.py tests/unit/test_feature_flags.py
git commit -m "feat(flags): PostHog feature-flag helper with env override + graceful degradation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: mem0 API — scope signaling, enforcement, moderation; second seeded contract user

**Files:**
- Modify: `proxy/apps/proxy_server/mem0_api.py` (all 5 handlers + module docstring + new helpers)
- Modify: `proxy/apps/proxy_server/main.py` — `_seed_contract_test_data` (~line 241) and `_contract_test_token` endpoint (~line 376)
- Test: `tests/unit/test_mem0_scope_helpers.py` (new), `tests/contract/test_proxy_contract.py` (append)

**Interfaces:**
- Consumes: `Permission.MEMORY_MODERATE` (Task 1); store `scope`/`org_all` parameters and `MemoryEntry.scope_type`/`author_user_id` (Task 3); `is_feature_enabled` (Task 5).
- Produces (client-visible API):
  - Write: top-level `"scope"` body field, fallback `metadata.scope`, default `"user"`; invalid → 400 `"invalid scope"`; `"org"` with flag OFF → 403 `"organization memory scope not enabled"`.
  - Read: optional `scope` filter (search body field / list+clear query param); absent → merged; search/list results gain `"scope"` and `"author_user_id"` fields.
  - Delete: fetch-then-decide; 404 `"memory not found"`; org rows deletable by author or moderator (403 `"not memory author"`), personal by owner (403 `"user mismatch"`).
  - Clear: default personal-only; `scope=org` → caller-authored org rows; `scope=org&all=true` → moderator-gated (403 `"memory moderation permission required"`).
  - Contract harness: `/_contract_test/token` response gains `"member_token"` (a second same-org user with role `"user"` — NOT a moderator). Existing keys unchanged.

- [ ] **Step 1: Write the failing helper unit tests**

Create `tests/unit/test_mem0_scope_helpers.py`:

```python
"""Unit tests for mem0_api pure scope/enforcement helpers.

The sqlite contract environment fails-closed on pgvector SQL, so row-level
delete/moderation decisions cannot be exercised end-to-end there — these
helpers carry that logic and are tested exhaustively here instead.
# regression: org memory delete rights — author or memory:moderate only
"""

from proxy.apps.proxy_server.mem0_api import (
    VALID_SCOPES,
    _delete_allowed,
    _is_moderator,
    _resolve_read_scope,
    _resolve_write_scope,
)
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext


def _user(role: Role, permissions) -> UserContext:
    return UserContext(
        user_id=5, username="u", role=role, organization_id=3,
        managed_orgs=[], permissions=permissions, api_key_id=None,
    )


# --- scope resolution -------------------------------------------------------

def test_valid_scopes_constant() -> None:
    assert VALID_SCOPES == ("user", "org")


def test_write_scope_defaults_to_user() -> None:
    assert _resolve_write_scope({}) == "user"
    assert _resolve_write_scope({"metadata": {}}) == "user"


def test_write_scope_top_level_field() -> None:
    assert _resolve_write_scope({"scope": "org"}) == "org"
    assert _resolve_write_scope({"scope": "user"}) == "user"


def test_write_scope_metadata_fallback() -> None:
    assert _resolve_write_scope({"metadata": {"scope": "org"}}) == "org"


def test_write_scope_top_level_wins_over_metadata() -> None:
    assert _resolve_write_scope({"scope": "user", "metadata": {"scope": "org"}}) == "user"


def test_write_scope_invalid_returns_none() -> None:
    assert _resolve_write_scope({"scope": "team"}) is None
    assert _resolve_write_scope({"metadata": {"scope": "shared"}}) is None


def test_read_scope_absent_means_merged_all() -> None:
    assert _resolve_read_scope(None) == "all"
    assert _resolve_read_scope("") == "all"


def test_read_scope_valid_values_pass_through() -> None:
    assert _resolve_read_scope("user") == "user"
    assert _resolve_read_scope("org") == "org"


def test_read_scope_invalid_returns_none() -> None:
    assert _resolve_read_scope("everything") is None
    assert _resolve_read_scope("all") is None  # 'all' is internal-only, not accepted on the wire


# --- moderation -------------------------------------------------------------

def test_admin_and_resource_manager_are_moderators() -> None:
    assert _is_moderator(_user(Role.ADMIN, ROLE_PERMISSIONS[Role.ADMIN])) is True
    assert _is_moderator(_user(Role.RESOURCE_MANAGER, ROLE_PERMISSIONS[Role.RESOURCE_MANAGER])) is True


def test_plain_user_is_not_moderator() -> None:
    assert _is_moderator(_user(Role.USER, ROLE_PERMISSIONS[Role.USER])) is False


def test_moderator_check_works_with_string_permissions() -> None:
    """Claims-derived UserContexts carry permission STRINGS, not enums."""
    assert _is_moderator(_user(Role.ADMIN, {"memory:moderate", "proxy:use"})) is True
    assert _is_moderator(_user(Role.USER, {"proxy:use"})) is False


# --- delete decision ---------------------------------------------------------

def test_delete_personal_owner_allowed() -> None:
    ok, err = _delete_allowed("user", author_user_id=5, row_user_id=5, token_user=5, moderator=False)
    assert ok is True and err == ""


def test_delete_personal_non_owner_denied_even_for_moderator() -> None:
    """memory:moderate governs SHARED memories only — it is not a skeleton
    key into someone's personal memory."""
    ok, err = _delete_allowed("user", author_user_id=9, row_user_id=9, token_user=5, moderator=True)
    assert ok is False and err == "user mismatch"


def test_delete_org_author_allowed() -> None:
    ok, err = _delete_allowed("org", author_user_id=5, row_user_id=5, token_user=5, moderator=False)
    assert ok is True


def test_delete_org_non_author_denied_without_moderate() -> None:
    ok, err = _delete_allowed("org", author_user_id=9, row_user_id=9, token_user=5, moderator=False)
    assert ok is False and err == "not memory author"


def test_delete_org_non_author_allowed_with_moderate() -> None:
    ok, err = _delete_allowed("org", author_user_id=9, row_user_id=9, token_user=5, moderator=True)
    assert ok is True


def test_delete_legacy_empty_scope_treated_as_personal() -> None:
    ok, err = _delete_allowed("", author_user_id=9, row_user_id=9, token_user=5, moderator=False)
    assert ok is False and err == "user mismatch"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/test_mem0_scope_helpers.py -v --no-cov`
Expected: FAIL with `ImportError: cannot import name 'VALID_SCOPES'`.

- [ ] **Step 3: Implement the helpers and rewire the handlers**

In `proxy/apps/proxy_server/mem0_api.py`:

**3a. Module docstring** — replace the paragraph beginning "Until organizational/shared memory-scope lands..." (lines 12-14) with:

```
Memories carry an access-control scope: "user" (personal — locked to the
authenticated user, the default) or "org" (shared with the caller's whole
organization). Writes signal scope via a top-level "scope" body field
(fallback: metadata.scope). Reads return the merged personal+org view by
default, relevance-ranked, with an optional scope filter. Org-scoped writes
are feature-flag gated (waddleai.memory-org-scope, default OFF). Shared
entries are deletable by their author or a holder of memory:moderate.
Cross-org access, cross-user personal access, and org-0 tokens remain 403.
```

**3b. Imports and helpers** — after the `logger = ...` line, add:

```python
from shared.auth.rbac import Permission, UserContext
from shared.utils.feature_flags import is_feature_enabled

# Wire-visible scope values. "all" (merged view) is internal-only.
VALID_SCOPES = ("user", "org")

MEMORY_ORG_SCOPE_FLAG = "waddleai.memory-org-scope"


def _resolve_write_scope(body: dict) -> "str | None":
    """Scope for a write: top-level 'scope' wins, metadata.scope is the
    fallback, absent means personal. Returns None for invalid values."""
    scope = body.get("scope")
    if scope is None:
        scope = (body.get("metadata") or {}).get("scope")
    if scope is None:
        return "user"
    return scope if scope in VALID_SCOPES else None


def _resolve_read_scope(raw: "str | None") -> "str | None":
    """Scope filter for reads: absent/empty means the merged view ('all',
    internal sentinel). Returns None for invalid values."""
    if not raw:
        return "all"
    return raw if raw in VALID_SCOPES else None


def _is_moderator(user: UserContext) -> bool:
    """True if the caller holds memory:moderate. Permission sets contain
    Permission enums (direct auth path) or plain strings (claims path) —
    check both, never role names."""
    perms = user.permissions or set()
    return Permission.MEMORY_MODERATE in perms or Permission.MEMORY_MODERATE.value in perms


def _delete_allowed(
    scope_type: str,
    author_user_id: int,
    row_user_id: int,
    token_user: int,
    moderator: bool,
) -> "tuple[bool, str]":
    """Row-level delete decision. Personal rows: owner only (moderation does
    not reach into personal memory). Org rows: author or moderator."""
    if scope_type == "org":
        if author_user_id == token_user or moderator:
            return True, ""
        return False, "not memory author"
    if row_user_id == token_user:
        return True, ""
    return False, "user mismatch"
```

**3c. `add_memories`** — after the existing user-mismatch check and the `user_id_int = token_user` / `org_id = token_org` lines, insert scope resolution BEFORE the message loop:

```python
    scope = _resolve_write_scope(body)
    if scope is None:
        return jsonify({"error": "invalid scope"}), 400
    if scope == "org" and not is_feature_enabled(MEMORY_ORG_SCOPE_FLAG, distinct_id=str(token_org)):
        return jsonify({"error": "organization memory scope not enabled"}), 403
```

and change the `MemoryEntry(...)` construction in the loop to pass the scope and author (metadata mirror is handled inside the stores):

```python
        entry = MemoryEntry(
            id="",
            user_id=user_id_int,
            organization_id=org_id,
            session_id=session_id,
            content=content,
            metadata={**metadata, "role": role},
            embedding=None,
            created_at=datetime.utcnow(),
            scope_type=scope,
            author_user_id=token_user,
        )
```

**3d. `search_memories`** — after the tenancy checks, resolve the filter and pass it through; extend the result projection:

```python
    scope = _resolve_read_scope(body.get("scope"))
    if scope is None:
        return jsonify({"error": "invalid scope"}), 400
```

```python
    entries = await manager.memory_store.search_memories(
        query=query,
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        limit=limit,
        min_relevance=threshold,
        scope=scope,
    )

    results = [
        {
            "id": entry.id,
            "memory": entry.content,
            "user_id": str(entry.user_id),
            "score": entry.relevance_score,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "scope": entry.scope_type,
            "author_user_id": str(entry.author_user_id or entry.user_id),
        }
        for entry in entries
    ]
```

**3e. `list_memories`** — same pattern with the query param:

```python
    scope = _resolve_read_scope(request.args.get("scope"))
    if scope is None:
        return jsonify({"error": "invalid scope"}), 400
```

pass `scope=scope` to `get_conversation_history(...)`, and add the same two fields (`"scope"`, `"author_user_id"`) to its result projection.

**3f. `delete_memory`** — replace the raw-SQL delete block (keep everything above `# Delete via raw SQL` unchanged) with fetch-then-decide:

```python
    # Fetch-then-decide: personal rows are owner-only; org rows are
    # deletable by their author or a memory:moderate holder.
    try:
        rows = await asyncio.to_thread(lambda: manager.memory_store.write_db.executesql(
            "SELECT scope_type, author_user_id, user_id FROM memory_embeddings "
            "WHERE id = %s AND organization_id = %s",
            (int(memory_id), org_id),
        ))
        if not rows:
            return jsonify({"error": "memory not found"}), 404

        scope_type, author_user_id, row_user_id = rows[0]
        allowed, err = _delete_allowed(
            scope_type or "user",
            int(author_user_id or row_user_id),
            int(row_user_id),
            token_user,
            _is_moderator(user),
        )
        if not allowed:
            return jsonify({"error": err}), 403

        await asyncio.to_thread(lambda: manager.memory_store.write_db.executesql(
            "DELETE FROM memory_embeddings WHERE id = %s AND organization_id = %s",
            (int(memory_id), org_id),
        ))
        return jsonify({"status": "deleted", "id": memory_id})
    except Exception as exc:
        logger.error("Failed to delete memory %s: %s", memory_id, exc)
        abort(500, description="Failed to delete memory")
```

(Note: the `to_thread` lambdas capture only locals, mirroring the existing pattern; on the sqlite contract environment the SELECT raises, is caught, and returns the same 500 as today — the existing `proxy_mem0_delete` snapshot must NOT change.)

**3g. `clear_memories`** — after the tenancy checks, resolve scope + the `all` param, gate moderation BEFORE any store call, and pass through:

```python
    raw_scope = request.args.get("scope")
    clear_scope = raw_scope if raw_scope in VALID_SCOPES else ("user" if not raw_scope else None)
    if clear_scope is None:
        return jsonify({"error": "invalid scope"}), 400

    org_all = request.args.get("all", "").strip().lower() in ("1", "true", "yes")
    if clear_scope == "org" and org_all and not _is_moderator(user):
        return jsonify({"error": "memory moderation permission required"}), 403
```

```python
    success = await manager.memory_store.clear_memories(
        user_id=user_id_int,
        organization_id=org_id,
        session_id=session_id,
        scope=clear_scope,
        org_all=org_all,
    )
```

- [ ] **Step 4: Run helper tests to verify they pass**

Run: `python3 -m pytest tests/unit/test_mem0_scope_helpers.py -v --no-cov`
Expected: all PASS.

- [ ] **Step 5: Seed a second (non-moderator) contract user**

In `proxy/apps/proxy_server/main.py`:

In `ProxyServer.__init__` (~line 119), after `self.contract_test_api_key = None`, add:

```python
        self.contract_test_member_token = None
```

In `_seed_contract_test_data` (~line 241), after the existing `self.contract_test_api_key = _TEST_API_KEY_SECRET` line, add:

```python
        # Second same-org user with role 'user' (NOT a moderator) — lets
        # contract tests prove org-moderation denials and personal isolation.
        member_id = self.db.users.insert(
            username="contract-test-member",
            email="contract-test-member@example.com",
            password_hash=bcrypt.hash("unused-not-a-real-login"),
            role="user",
            organization_id=org_id,
            token_quota_monthly=1000000,
            token_quota_daily=100000,
            enabled=True,
            created_at=datetime.utcnow(),
        )
        self.db.commit()
        member_context = UserContext(
            user_id=member_id,
            username="contract-test-member",
            role=Role.USER,
            organization_id=org_id,
            managed_orgs=[],
            permissions=ROLE_PERMISSIONS[Role.USER],
            api_key_id=None,
        )
        self.contract_test_member_token = issue_token(member_context, self.oidc_provider)
```

In the `_contract_test_token` endpoint (~line 381), add the key to the response dict:

```python
                "member_token": proxy_server.contract_test_member_token,
```

- [ ] **Step 6: Add the contract tests**

Append to `tests/contract/test_proxy_contract.py` (after the existing mem0 regression tests). Also add this helper next to `_bearer_headers`:

```python
def _member_headers(base):
    """Second seeded same-org user, role 'user' — no memory:moderate."""
    r = httpx.get(f"{base}/_contract_test/token")
    return {"Authorization": f"Bearer {r.json()['member_token']}"}
```

New tests:

```python
# ---------------------------------------------------------------------------
# Memory access-control scope (personal vs org) — see
# docs/superpowers/specs/2026-07-14-memory-access-control-design.md
# ---------------------------------------------------------------------------


def test_mem0_post_invalid_scope(proxy_url):
    """Unknown scope values are a hard 400, never coerced to personal."""
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers=_bearer_headers(proxy_url),
        json={"messages": [{"role": "user", "content": "hi"}], "scope": "team"},
    )
    assert_snapshot("proxy_mem0_post_invalid_scope", status=r.status_code, body=r.json())


def test_mem0_post_org_scope_flag_off(proxy_url):
    """Org-scoped writes are feature-flag gated (waddleai.memory-org-scope,
    default OFF) — with the flag unset, behavior is personal-only lockdown.
    # regression: org scope must be opt-in via flag, default OFF"""
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers=_bearer_headers(proxy_url),
        json={"messages": [{"role": "user", "content": "hi"}], "scope": "org"},
    )
    assert_snapshot("proxy_mem0_post_org_flag_off", status=r.status_code, body=r.json())


def test_mem0_post_metadata_scope_fallback_flag_off(proxy_url):
    """metadata.scope is honored as the fallback signal — same flag gate."""
    r = httpx.post(
        f"{proxy_url}/mem0/memories",
        headers=_bearer_headers(proxy_url),
        json={"messages": [{"role": "user", "content": "hi"}], "metadata": {"scope": "org"}},
    )
    assert_snapshot("proxy_mem0_post_metadata_org_flag_off", status=r.status_code, body=r.json())


def test_mem0_search_invalid_scope_filter(proxy_url):
    r = httpx.post(
        f"{proxy_url}/mem0/memories/search",
        headers=_bearer_headers(proxy_url),
        json={"query": "q", "scope": "everything"},
    )
    assert_snapshot("proxy_mem0_search_invalid_scope", status=r.status_code, body=r.json())


def test_mem0_search_scope_filter_org(proxy_url):
    """Reads are NOT flag-gated; org filter is accepted (empty on sqlite)."""
    r = httpx.post(
        f"{proxy_url}/mem0/memories/search",
        headers=_bearer_headers(proxy_url),
        json={"query": "q", "scope": "org"},
    )
    assert_snapshot("proxy_mem0_search_scope_org", status=r.status_code, body=r.json())


def test_mem0_list_scope_filter_user(proxy_url):
    r = httpx.get(
        f"{proxy_url}/mem0/memories",
        params={"scope": "user"},
        headers=_bearer_headers(proxy_url),
    )
    assert_snapshot("proxy_mem0_list_scope_user", status=r.status_code, body=r.json())


def test_mem0_list_invalid_scope_filter(proxy_url):
    r = httpx.get(
        f"{proxy_url}/mem0/memories",
        params={"scope": "all"},  # internal sentinel — not accepted on the wire
        headers=_bearer_headers(proxy_url),
    )
    assert_snapshot("proxy_mem0_list_invalid_scope", status=r.status_code, body=r.json())


def test_mem0_clear_org_all_requires_moderation(proxy_url):
    """Full org wipe is moderator-gated; the member token (role 'user') has
    no memory:moderate and must get 403 BEFORE any store call.
    # regression: org-wide memory wipe requires memory:moderate"""
    r = httpx.delete(
        f"{proxy_url}/mem0/memories",
        params={"scope": "org", "all": "true"},
        headers=_member_headers(proxy_url),
    )
    assert_snapshot("proxy_mem0_clear_org_all_member_denied", status=r.status_code, body=r.json())


def test_mem0_member_token_basic_access(proxy_url):
    """Sanity: the second seeded user authenticates and lists (empty)."""
    r = httpx.get(f"{proxy_url}/mem0/memories", headers=_member_headers(proxy_url))
    assert_snapshot("proxy_mem0_member_list", status=r.status_code, body=r.json())
```

- [ ] **Step 7: Run the proxy contract suite and record ONLY the new snapshots**

```bash
python3 -m pytest tests/contract/test_proxy_contract.py -v --no-cov 2>&1 | tail -45
```

Expected: `snapshot.py`'s `assert_snapshot` AUTO-RECORDS any missing snapshot (writes the file and passes), so the first run creates the 9 new snapshot files and passes. Run the suite a SECOND time to prove the new snapshots are stable (recorded == asserted). All PRE-EXISTING tests must pass with their snapshots byte-identical on both runs.

Then verify no existing snapshot changed:

```bash
git status --porcelain tests/contract/snapshots/
```

Expected: ONLY new (`??`) files — `proxy_mem0_post_invalid_scope.json`, `proxy_mem0_post_org_flag_off.json`, `proxy_mem0_post_metadata_org_flag_off.json`, `proxy_mem0_search_invalid_scope.json`, `proxy_mem0_search_scope_org.json`, `proxy_mem0_list_scope_user.json`, `proxy_mem0_list_invalid_scope.json`, `proxy_mem0_clear_org_all_member_denied.json`, `proxy_mem0_member_list.json`. Zero modified (`M`) snapshot files. If any existing snapshot shows as modified, STOP — the handler change broke wire compat; fix the handler, do not re-record.

Sanity-check the new snapshots' captured statuses: invalid-scope files must show 400; `org_flag_off`, `metadata_org_flag_off`, `clear_org_all_member_denied` must show 403; `search_scope_org`, `list_scope_user`, `member_list` must show 200 with empty results.

- [ ] **Step 8: Run the full contract gate**

```bash
make test-contract 2>&1 | tail -6
```

Expected: all contract tests pass (previous count 70 + 9 new = 79).

- [ ] **Step 9: Lint and commit**

```bash
python3 -m flake8 proxy/apps/proxy_server/mem0_api.py proxy/apps/proxy_server/main.py tests/unit/test_mem0_scope_helpers.py tests/contract/test_proxy_contract.py
python3 -m black --check proxy/apps/proxy_server/mem0_api.py tests/unit/test_mem0_scope_helpers.py
git add proxy/apps/proxy_server/mem0_api.py proxy/apps/proxy_server/main.py tests/unit/test_mem0_scope_helpers.py tests/contract/test_proxy_contract.py tests/contract/snapshots/
git commit -m "feat(proxy): personal vs org memory scope on mem0 API — flag-gated writes, merged reads, moderated deletes

Scope signaled via top-level 'scope' (fallback metadata.scope, default
user). Org writes gated behind waddleai.memory-org-scope (default OFF).
Reads return merged personal+org, relevance-ranked, with scope filter.
Org entries deletable by author or memory:moderate; org-wide clear is
moderator-only. All PR #50 tenancy locks unchanged.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Full-suite verification

**Files:**
- No source changes expected. Fix-forward only if a failure traces to this branch's commits.

**Interfaces:** none — this is the merge-gate check.

- [ ] **Step 1: Full unit suite**

```bash
python3 -m pytest tests/unit -v --no-cov 2>&1 | tail -15
```

Expected: everything passes (or is skipped exactly as on the base branch). Baseline on `chore/consolidate-quart-k8s` was 821 passed; this branch adds ~40 new unit tests.

- [ ] **Step 2: Full contract suite**

```bash
make test-contract 2>&1 | tail -6
```

Expected: green; 79 proxy+management contract tests including the 9 new mem0 scope tests.

- [ ] **Step 3: Snapshot drift audit**

```bash
git diff --stat chore/consolidate-quart-k8s -- tests/contract/snapshots/
```

Expected: only ADDED files (the 9 new mem0 scope snapshots). Any modified pre-existing snapshot is a defect — trace and fix the handler, never re-record.

- [ ] **Step 4: Lint the branch delta**

```bash
git diff --name-only chore/consolidate-quart-k8s -- '*.py' | xargs python3 -m flake8
```

Expected: clean.

- [ ] **Step 5: Report**

No commit in this task unless a fix was needed. Report: unit count, contract count, snapshot audit result, lint result.
