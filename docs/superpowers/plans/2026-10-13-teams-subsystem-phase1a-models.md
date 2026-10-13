# Teams Subsystem — Phase 1a: SQLAlchemy and PyDAL Definitions

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §4 (data model).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q` (targeted runs skip the coverage addopts; the 90% coverage gate runs in Task 28).
**Branch:** work in a `feature/teams-subsystem` worktree cut from `release/v0.2.X`; one venv per worktree (`make venv`).

---

<!-- TASK 1 -->
## Task 1: SQLAlchemy models (`teams`, `team_members`, `api_keys.team_id`, `virtual_keys.api_key_id`)

**Files:**
- Modify: `services/management/app/models_sqlalchemy.py` (schema authority)
- Modify: `tests/conftest.py` (add the `real_schema_db` fixture)
- Test: `tests/unit/management/test_team_models.py` (create)

**Interfaces:**
- Produces: ORM classes `Team`, `TeamMember`; columns `APIKey.team_id` (nullable FK, `ON DELETE SET NULL`) and `VirtualKey.api_key_id` (nullable FK to `api_keys.id` -- the revoke-parity link, see the index's "Spec decisions" list); `UNIQUE(organization_id, name)` on teams; `UNIQUE(user_id, organization_id)` and `UNIQUE(team_id, user_id)` on team_members.
- Produces: pytest fixture `real_schema_db` -- a real penguin-dal handle (`DB`) over a SQLite file built by `Base.metadata.create_all`. **Every later task's DB tests build on it.** Note: a `create_all` schema has NO server-side defaults, so tests must insert `enabled=True` explicitly where auth filters on it.
- Consumes: nothing.

- [ ] **Step 1: Write the failing test and the fixture**

Append the fixture to the end of `tests/conftest.py` (the diff below is exactly this task's change):

```diff
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -264,3 +264,23 @@
     transformer_mock = Mock()
     transformer_mock.encode = Mock(return_value=[0.1, 0.2, 0.3, 0.4])
     return transformer_mock
+
+
+@pytest.fixture
+def real_schema_db(tmp_path):
+    """A real penguin-dal handle over the SQLAlchemy ``create_all`` schema (SQLite, no mocks).
+
+    SQLAlchemy models are the schema authority, so building the file from
+    ``Base.metadata`` and letting penguin-dal reflect it exercises the same
+    tables/constraints production has. Used by the teams-subsystem tests.
+    """
+    import sqlalchemy as sa
+
+    from services.management.app.models_sqlalchemy import Base
+    from shared.database.models import get_db
+
+    url = f"sqlite:///{tmp_path}/real_schema.db"
+    engine = sa.create_engine(url)
+    Base.metadata.create_all(engine)
+    engine.dispose()
+    return get_db(url)
```

Create `tests/unit/management/test_team_models.py`:

```python
"""Teams schema (spec §4): SQLAlchemy model shape and the D2 uniqueness guarantee.

Runs against ``real_schema_db`` -- the SQLAlchemy ``create_all`` schema reflected by
penguin-dal, no mocks -- so the UNIQUE constraints are enforced by a real engine.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from services.management.app.models_sqlalchemy import APIKey, Team, TeamMember, VirtualKey

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


def _cols(model) -> set[str]:
    """Return the column-name set of a declarative model's table."""
    return {c.name for c in model.__table__.columns}


def _org(db, name: str) -> int:
    """Insert an organization and return its id."""
    org_id = db.organizations.insert(name=name)
    db.commit()
    return int(org_id)


def _user(db, name: str, org_id: int) -> int:
    """Insert a minimal user in ``org_id`` and return its id."""
    user_id = db.users.insert(
        username=name, email=f"{name}@example.com", password_hash=_PLACEHOLDER_HASH, role="user",
        organization_id=org_id,
    )  # fmt: skip
    db.commit()
    return int(user_id)


def test_team_tables_have_exactly_the_spec_columns() -> None:
    """``teams``/``team_members`` carry the spec §4 columns and nothing else."""
    assert _cols(Team) == {"id", "organization_id", "name", "enabled", "created_at"}
    assert _cols(TeamMember) == {
        "id", "team_id", "user_id", "organization_id", "team_role", "created_at",
    }  # fmt: skip


def test_api_keys_gain_team_id_and_virtual_keys_gain_api_key_id() -> None:
    """``api_keys.team_id`` (spec §4) and ``virtual_keys.api_key_id`` (revoke-parity link)."""
    assert "team_id" in _cols(APIKey)
    assert "api_key_id" in _cols(VirtualKey)


def test_second_team_in_same_org_is_rejected(real_schema_db) -> None:
    """D2: UNIQUE(user_id, organization_id) blocks a second team for a user in one tenant."""
    db = real_schema_db
    org = _org(db, "acme")
    user = _user(db, "alice", org)
    t1 = db.teams.insert(organization_id=org, name="Alpha")
    t2 = db.teams.insert(organization_id=org, name="Beta")
    db.team_members.insert(team_id=t1, user_id=user, organization_id=org, team_role="member")
    db.commit()
    with pytest.raises(IntegrityError):
        db.team_members.insert(team_id=t2, user_id=user, organization_id=org, team_role="member")


def test_user_may_have_one_team_in_each_of_two_tenants(real_schema_db) -> None:
    """D2 is per (user, tenant): a user managing two orgs may join one team in each."""
    db = real_schema_db
    org_a, org_b = _org(db, "acme"), _org(db, "globex")
    user = _user(db, "mgr", org_a)
    ta = db.teams.insert(organization_id=org_a, name="Alpha")
    tb = db.teams.insert(organization_id=org_b, name="Alpha")
    db.team_members.insert(team_id=ta, user_id=user, organization_id=org_a, team_role="member")
    db.team_members.insert(team_id=tb, user_id=user, organization_id=org_b, team_role="member")
    db.commit()
    assert db(db.team_members.user_id == user).count() == 2


def test_team_name_is_unique_per_org_not_globally(real_schema_db) -> None:
    """UNIQUE(organization_id, name): same name in two orgs is fine, twice in one is not."""
    db = real_schema_db
    org_a, org_b = _org(db, "acme"), _org(db, "globex")
    db.teams.insert(organization_id=org_a, name="Default")
    db.teams.insert(organization_id=org_b, name="Default")
    db.commit()
    with pytest.raises(IntegrityError):
        db.teams.insert(organization_id=org_a, name="Default")
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_models.py --no-cov -q`
Expected: collection error `ImportError: cannot import name 'Team' from 'services.management.app.models_sqlalchemy'`.

- [ ] **Step 3: Add the models and columns**

```diff
--- a/services/management/app/models_sqlalchemy.py
+++ b/services/management/app/models_sqlalchemy.py
@@ -97,6 +97,40 @@
     service_kind = Column(String(50), nullable=True)


+class Team(Base):
+    """A team nested under one organization (tenant): the authz tier between org and user."""
+
+    __tablename__ = "teams"
+    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_teams_org_name"),)
+
+    id = Column(Integer, primary_key=True, autoincrement=True)
+    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
+    name = Column(String(255), nullable=False)
+    enabled = Column(Boolean, default=True)
+    created_at = Column(DateTime, default=datetime.utcnow)
+
+
+class TeamMember(Base):
+    """A user's membership and role (owner/admin/member/viewer) in exactly one team per tenant.
+
+    ``organization_id`` is denormalized from the team purely so
+    ``UNIQUE(user_id, organization_id)`` can enforce "one team per (user, tenant)" (D2).
+    """
+
+    __tablename__ = "team_members"
+    __table_args__ = (
+        UniqueConstraint("user_id", "organization_id", name="uq_team_members_user_org"),
+        UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),
+    )
+
+    id = Column(Integer, primary_key=True, autoincrement=True)
+    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
+    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
+    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
+    team_role = Column(String(20), nullable=False)  # owner | admin | member | viewer
+    created_at = Column(DateTime, default=datetime.utcnow)
+
+
 class APIKey(Base):
     """A hashed API credential issued to a user, carrying its own quotas, rate limit, and scopes."""

@@ -123,6 +157,9 @@
     # embedding_cache/schema_dedup). NULL = feature defaults apply.
     # See shared/memory/config.py::resolve_proxy_memory_config.
     proxy_memory = Column(JSON, nullable=True)
+    # Acting team (teams spec D3): the team a request authenticated by this key acts
+    # in. NULL = teamless (legacy/unassigned). Backfilled by migration 024_teams.
+    team_id = Column(Integer, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True)


 class AIProvider(Base):
@@ -341,6 +378,10 @@
     expires_at = Column(DateTime)
     last_used = Column(DateTime)
     created_at = Column(DateTime, default=datetime.utcnow)
+    # The proxy-auth ``api_keys`` row minted alongside this key (teams spec D10 /
+    # issue #303) so revoke/rotate/disable here can reach the credential the proxy
+    # actually authenticates. NULL = legacy key that never authenticated at the proxy.
+    api_key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)


 class TokenUsage(Base):
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_models.py --no-cov -q`
Expected: `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/models_sqlalchemy.py tests/conftest.py tests/unit/management/test_team_models.py
git commit -m "feat(teams): SQLAlchemy models for teams, team_members, key team links"
```

---

<!-- TASK 2 -->
## Task 2: PyDAL definitions for the proxy runtime

**Files:**
- Modify: `shared/database/models.py` (`define_tables`)
- Test: `tests/unit/test_team_pydal_definitions.py` (create)

**Interfaces:**
- Consumes: `Team`, `TeamMember`, `APIKey` from Task 1.
- Produces: `db.teams`, `db.team_members`, `db.api_keys.team_id` in the proxy's `get_db()` schema (spec §4: the proxy reads `team_members` at auth time). The composite UNIQUE constraints are NOT expressible in PyDAL `Field`s; they live only in SQLAlchemy/Alembic (same as other composite constraints).

- [ ] **Step 1: Write the failing test**

```python
"""PyDAL (proxy runtime) definitions for the teams tables must match SQLAlchemy (schema authority).

Real PyDAL<->SQLAlchemy drift already shipped once (migrations 020/023); this is the guard that
``teams``, ``team_members`` and ``api_keys.team_id`` cannot drift the same way (spec §4).
"""

import pytest

from services.management.app.models_sqlalchemy import APIKey, Team, TeamMember
from shared.database.models import get_db

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


@pytest.fixture
def pydal_db():
    """A fresh in-memory PyDAL schema (``migrate=True``), as the contract harness builds it."""
    return get_db(db_uri="sqlite://", migrate=True)


def _pydal_cols(db, table: str) -> set[str]:
    """Return the column-name set penguin-dal holds for ``table``."""
    return set(getattr(db, table).table.columns.keys())


@pytest.mark.parametrize(("table", "model"), [("teams", Team), ("team_members", TeamMember)])
def test_pydal_columns_match_sqlalchemy(pydal_db, table, model) -> None:
    """The proxy's PyDAL table has exactly the SQLAlchemy model's columns."""
    assert _pydal_cols(pydal_db, table) == {c.name for c in model.__table__.columns}


def test_pydal_api_keys_has_team_id(pydal_db) -> None:
    """``api_keys.team_id`` exists on the proxy's PyDAL definition (the proxy reads it)."""
    assert "team_id" in _pydal_cols(pydal_db, "api_keys")
    assert {c.name for c in APIKey.__table__.columns} == _pydal_cols(pydal_db, "api_keys") | {
        "proxy_memory"
    }, "api_keys drift beyond the known pre-existing proxy_memory column"


def test_team_member_round_trips_through_pydal(pydal_db) -> None:
    """A team + membership written through the proxy's definitions reads back intact."""
    db = pydal_db
    org = db.organizations.insert(name="acme")
    user = db.users.insert(
        username="a",
        email="a@example.com",
        password_hash=_PLACEHOLDER_HASH,
        role="user",
        organization_id=org,
    )
    team = db.teams.insert(organization_id=org, name="Default")
    db.team_members.insert(team_id=team, user_id=user, organization_id=org, team_role="admin")
    db.commit()
    row = db(db.team_members.user_id == user).select().first()
    assert (row.team_id, row.team_role, row.organization_id) == (team, "admin", org)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_team_pydal_definitions.py --no-cov -q`
Expected: FAIL with `penguin_dal.exceptions.TableNotFoundError: Table 'teams' not found in database`.

- [ ] **Step 3: Add the PyDAL tables and `api_keys.team_id`**

`teams` must be defined before `api_keys` (it is referenced), and `team_members` after `users`.

```diff
--- a/shared/database/models.py
+++ b/shared/database/models.py
@@ -159,6 +159,28 @@
         Field("enabled", "boolean", default=True),
     )

+    # Teams (teams subsystem spec §4). The proxy reads ``team_members`` at auth time to
+    # resolve ``UserContext.team_role`` and ``api_keys.team_id`` for the acting team, so
+    # both need a runtime definition here. SQLAlchemy (models_sqlalchemy.py) stays the schema
+    # authority; the composite UNIQUE constraints live only there and in Alembic 024_teams.
+    _define_table_if_absent(
+        db,
+        "teams",
+        Field("organization_id", "reference organizations", notnull=True),
+        Field("name", "string", notnull=True),
+        Field("enabled", "boolean", default=True),
+        Field("created_at", "datetime", default=datetime.utcnow),
+    )
+    _define_table_if_absent(
+        db,
+        "team_members",
+        Field("team_id", "reference teams", notnull=True),
+        Field("user_id", "reference users", notnull=True),
+        Field("organization_id", "reference organizations", notnull=True),
+        Field("team_role", "string", notnull=True),  # owner, admin, member, viewer
+        Field("created_at", "datetime", default=datetime.utcnow),
+    )
+
     # API Keys with Usage Limits
     _define_table_if_absent(
         db,
@@ -179,6 +201,7 @@
         Field("permissions", "json"),  # Scoped permissions
         Field("allowed_endpoints", "list:string"),  # Endpoint restrictions
         Field("api_access_level", "string"),  # admin_api, management_api, proxy_api
+        Field("team_id", "reference teams"),  # Acting team (spec D3); NULL = teamless
     )

     # Connection Links (LLM Providers)
```

- [ ] **Step 4: Run it and confirm it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_team_pydal_definitions.py tests/unit/test_rbac_additional.py --no-cov -q`
Expected: all pass (the existing PyDAL-backed rbac tests still build their schema).

- [ ] **Step 5: Commit**

```bash
git add shared/database/models.py tests/unit/test_team_pydal_definitions.py
git commit -m "feat(teams): PyDAL runtime definitions for teams and api_keys.team_id"
```
