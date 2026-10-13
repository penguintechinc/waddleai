# Teams Subsystem — Phase 7d: Keeping Every File Under the 25k-Character Limit

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Rule:** `CLAUDE.md` -- maximum **25,000 characters** per code or markdown file; split large files into modules.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Tasks 1-30 (all `rbac.py` and `conftest.py` growth has landed; this is a pure refactor, so it runs last to move finished code verbatim).

Before this task `shared/auth/rbac.py` is ~31k characters (it was 22.7k): Tasks 6, 8, 10 and 16 add the team permissions, role bundles, the Team branch of `check_permission`, acting-team resolution and credential minting. The sections below split it and the teams test fixtures.

---

<!-- TASK n4 -->
## Task 31: Split `rbac.py` and the management `conftest.py` so no touched file crosses 25k

**Files:**
- Create: `shared/auth/permissions.py` (the `Role` and `Permission` enums, moved verbatim)
- Create: `shared/auth/team_rbac.py` (`TeamRole`, the role bundles, `expand_team_permissions`, the acting-team lookups, plus new `team_permission_allowed`)
- Modify: `shared/auth/rbac.py` (re-exports; `check_permission` calls `team_permission_allowed`)
- Create: `tests/unit/management/team_fixtures.py` and Modify `tests/unit/management/conftest.py` (teams fixtures moved out and re-imported)
- Test: `tests/unit/test_auth_module_split.py` (create)

**Interfaces:**
- Produces: `shared.auth.permissions.{Role, Permission}` (a leaf module -- it imports nothing from `shared.auth`, which is what breaks the cycle: `team_rbac` needs `Permission`, `rbac` needs `team_rbac`); `shared.auth.team_rbac.{TeamRole, TEAM_ROLE_PERMISSIONS, TEAM_SCOPED_PERMISSIONS, expand_team_permissions, lookup_key_team, lookup_user_team, team_permission_allowed}`. `team_permission_allowed(permission, *, team_id, team_role, resource_team_id) -> bool` is the Team branch of `check_permission` lifted out unchanged: `True` for non-team permissions, otherwise own-team AND a granting role.
- **Backward compatible:** `shared.auth.rbac` re-exports every moved name (as the *same objects* -- a test asserts identity), so no other import in the repo changes; `from shared.auth.rbac import Permission, Role, TeamRole, ...` keeps working.
- Size guard: the new test fails if any module the teams feature creates or shrinks reaches 25,000 characters.
- Verified sizes (characters). New/changed modules: `rbac.py` 22,685 -> **20,973**, `permissions.py` **6,669**, `team_rbac.py` **5,279**, `auth_cache.py` 21,631 -> 22,710, `penguin_auth.py` 17,641 -> 18,804, every new teams module < 14,000. **Files this feature edits that were already over the limit before it** (none newly crosses; each grows modestly and a split of these legacy files is out of scope): `models_sqlalchemy.py` 67,976 -> 70,093, `api/v1/auth.py` 41,624 -> 43,140, `api/v1/keys.py` 25,845 -> 28,535 (the key logic lives in `services/key_minting.py`), `shared/database/models.py` 25,872 -> 27,040 (PyDAL must define the tables the proxy reads), `tests/unit/management/conftest.py` 27,010 -> 28,059 (after this task moves ~6k of teams fixtures out), `tests/unit/test_rbac_additional.py` 27,542 -> 27,629, `tests/unit/proxy/test_auth_cache.py` 25,252 -> 25,441.

- [ ] **Step 1: Write the failing test**

```python
"""``rbac.py`` split (CLAUDE.md: no code file over 25,000 characters).

Role/Permission live in ``permissions.py`` and the team role bundles + acting-team resolution in
``team_rbac.py``; ``rbac.py`` re-exports them so every existing import keeps working. These tests
pin the re-exports as the *same objects*, keep the new modules cycle-free, and fail if any module
this feature adds or splits grows past the limit.
"""

import ast
from pathlib import Path
from typing import Any

import pytest

import shared.auth.permissions as permissions
import shared.auth.rbac as rbac
import shared.auth.team_rbac as team_rbac

REPO = Path(__file__).resolve().parents[2]
SIZE_LIMIT = 25_000

#: Modules the teams feature creates or shrinks; each must stay under the limit.
_LIMITED = [
    "shared/auth/rbac.py",
    "shared/auth/permissions.py",
    "shared/auth/team_rbac.py",
    "shared/auth/team_flags.py",
    "shared/auth/penguin_auth.py",
    "proxy/apps/proxy_server/auth_cache.py",
    "services/management/app/api/v1/teams.py",
    "services/management/app/api/v1/_team_authz.py",
    "services/management/app/services/teams.py",
    "services/management/app/services/key_minting.py",
    "services/management/app/services/auth_cache_invalidation.py",
    "services/management/alembic/versions/024_teams.py",
]


@pytest.mark.parametrize("path", _LIMITED)
def test_module_is_under_the_size_limit(path: str) -> None:
    """No module the teams feature touches that was under 25k may cross it."""
    assert len((REPO / path).read_text()) < SIZE_LIMIT, path


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (permissions, "Permission"),
        (permissions, "Role"),
        (team_rbac, "TeamRole"),
        (team_rbac, "TEAM_ROLE_PERMISSIONS"),
        (team_rbac, "TEAM_SCOPED_PERMISSIONS"),
        (team_rbac, "expand_team_permissions"),
        (team_rbac, "lookup_key_team"),
        (team_rbac, "lookup_user_team"),
    ],
)
def test_rbac_reexports_the_split_out_names_as_the_same_objects(owner, name: str) -> None:
    """``shared.auth.rbac.<name>`` is the very object defined in the new module."""
    assert getattr(rbac, name) is getattr(owner, name)


@pytest.mark.parametrize("module", [permissions, team_rbac])
def test_split_modules_never_import_rbac(module) -> None:
    """The new modules are import-cycle-free: nothing in them imports ``shared.auth.rbac``."""
    tree = ast.parse(Path(module.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert "shared.auth.rbac" not in imported


P = permissions.Permission


def _allowed(perm, **kw) -> bool:
    """Call ``team_permission_allowed`` with defaults for the team fields."""
    kwargs: dict[str, Any] = {"team_id": 10, "team_role": "member", "resource_team_id": 10}
    kwargs.update(kw)
    return team_rbac.team_permission_allowed(perm, **kwargs)


def test_non_team_permissions_are_not_decided_here() -> None:
    """Org-layer and unknown permissions pass through (``check_permission`` decides them)."""
    assert _allowed(P.APIKEY_READ, resource_team_id=None)
    assert _allowed("not:a-permission")
    assert _allowed(P.TEAM_ORG_ADMIN, resource_team_id=None)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, True),
        ({"resource_team_id": 11}, False),
        ({"resource_team_id": None}, False),
        ({"team_id": None}, False),
        ({"team_role": "viewer"}, False),
        ({"team_role": None}, False),
    ],
)
def test_team_layer_permission_needs_own_team_and_a_granting_role(kw, expected) -> None:
    """``team:use``: own team + a role that grants it, else denied (fail closed)."""
    assert _allowed(P.TEAM_USE, **kw) is expected
    assert _allowed("team:use", **kw) is expected
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_auth_module_split.py --no-cov -q`
Expected: collection error `ModuleNotFoundError: No module named 'shared.auth.permissions'`.

- [ ] **Step 3: Split `rbac.py` with a one-off script**

Save as `split_rbac_once.py` at the repo root, run it, then **delete it** (do not commit it). It moves the code verbatim -- no retyping -- and writes the two small new pieces (the module docstrings and `team_permission_allowed`):

```python
"""One-off refactor: move Role/Permission and the team code out of shared/auth/rbac.py.

Run from the repo root, then ``ruff format shared && ruff check --fix shared``. Everything is
moved verbatim except the new ``team_permission_allowed`` and the two module docstrings; the
script is deleted afterwards (it is not committed).
"""

from pathlib import Path

RBAC = Path("shared/auth/rbac.py")
text = RBAC.read_text()

i_role = text.index("class Role(Enum):")
i_teamrole = text.index("class TeamRole(Enum):")
i_user = text.index("@dataclass\nclass UserContext:")
i_teambundles = text.index("_TEAM_VIEWER = ")
i_valkey = text.index("#: Valkey key prefix")

enums = text[i_role:i_teamrole].rstrip() + "\n"  # Role + Permission
team_role_enum = text[i_teamrole:i_user].rstrip() + "\n"  # TeamRole
team_logic = text[i_teambundles:i_valkey].rstrip() + "\n"  # bundles, expand, lookups

Path("shared/auth/permissions.py").write_text(
    '"""Role and Permission enums, split out of ``rbac.py`` (a leaf module: no auth imports).\n\n'
    "``shared.auth.rbac`` re-exports both names, so ``from shared.auth.rbac import Permission, Role``\n"
    "keeps working everywhere. The split lets the team role bundles (``team_rbac``) import\n"
    "``Permission`` without a circular import and keeps every auth module under 25k characters.\n"
    '"""\n\nfrom enum import Enum\n\n\n' + enums
)

Path("shared/auth/team_rbac.py").write_text(
    '"""Team roles as scope bundles and acting-team resolution (teams spec §5, D2/D3/D5).\n\n'
    "Split out of ``rbac.py``; ``shared.auth.rbac`` re-exports every public name here, so existing\n"
    "imports are unaffected. Depends only on ``shared.auth.permissions`` and the kill switch --\n"
    'never on ``rbac``.\n"""\n\n'
    "from enum import Enum\n\n"
    "from shared.auth.permissions import Permission\n"
    "from shared.auth.team_flags import teams_enabled\n\n\n"
    + team_role_enum
    + "\n\n"
    + team_logic
    + '''

def team_permission_allowed(
    permission: object,
    *,
    team_id: int | None,
    team_role: str | None,
    resource_team_id: int | None,
) -> bool:
    """Apply the Team branch of ``check_permission`` (teams spec D5/D9).

    Returns ``True`` for anything that is not a team-layer permission (the org-layer checks
    decide those). A team-layer permission is allowed only if ``resource_team_id`` names the
    caller's own acting team -- no team named means deny -- AND the caller's resolved
    ``team_role`` grants it: a stale or forged scope claim can never exceed the role.
    """
    value = permission.value if isinstance(permission, Permission) else permission
    try:
        perm = Permission(value)
    except ValueError:
        return True
    if perm not in TEAM_SCOPED_PERMISSIONS:
        return True
    if resource_team_id is None or team_id != resource_team_id:
        return False
    return perm in expand_team_permissions(team_role)
'''
)

rest = text[:i_role] + text[i_user:i_teambundles] + text[i_valkey:]
rest = rest.replace(
    "from shared.auth.team_flags import teams_enabled\n",
    "from shared.auth.permissions import Permission, Role\n"
    "from shared.auth.team_rbac import (\n"
    "    TEAM_ROLE_PERMISSIONS,  # noqa: F401 -- re-exported\n"
    "    TEAM_SCOPED_PERMISSIONS,  # noqa: F401 -- re-exported\n"
    "    TeamRole,  # noqa: F401 -- re-exported\n"
    "    expand_team_permissions,\n"
    "    lookup_key_team,\n"
    "    lookup_user_team,  # noqa: F401 -- re-exported\n"
    "    team_permission_allowed,\n"
    ")\n",
)
start = rest.index("        # Team branch: team-layer permissions")
end = rest.index("        # Resource-specific checks")
rest = (
    rest[:start]
    + "        # Team branch: team-layer permissions are pinned to the caller's own team and role.\n"
    "        if not team_permission_allowed(\n"
    "            perm_value,\n"
    "            team_id=user_context.team_id,\n"
    "            team_role=user_context.team_role,\n"
    "            resource_team_id=resource_team_id,\n"
    "        ):\n"
    "            return False\n\n" + rest[end:]
)
RBAC.write_text(rest)
```

```bash
python3 split_rbac_once.py && rm split_rbac_once.py
ruff format shared && ruff check --fix shared
```

The re-export block it leaves at the top of `rbac.py` and the lifted Team branch inside `check_permission` (after ruff) are:

```python
from shared.auth.permissions import Permission, Role
from shared.auth.team_rbac import (
    TEAM_ROLE_PERMISSIONS,  # noqa: F401 -- re-exported
    TEAM_SCOPED_PERMISSIONS,  # noqa: F401 -- re-exported
    TeamRole,  # noqa: F401 -- re-exported
    expand_team_permissions,
    lookup_key_team,
    lookup_user_team,  # noqa: F401 -- re-exported
    team_permission_allowed,
)
```

```python
        # Team branch: team-layer permissions are pinned to the caller's own team and role.
        if not team_permission_allowed(
            perm_value,
            team_id=user_context.team_id,
            team_role=user_context.team_role,
            resource_team_id=resource_team_id,
        ):
            return False
```

- [ ] **Step 4: Move the teams fixtures out of `conftest.py`**

Same procedure (`move_fixtures_once.py`, run, delete, not committed). It moves everything the teams tasks added after the `make_token` change -- the autouse mock-DB guard, `real_db_client`, `team_headers`, `SharedValkey`/`shared_valkey` and `redis_spy` -- into `team_fixtures.py` and imports the names back so pytest still registers them:

```python
"""One-off refactor: move the teams fixtures out of tests/unit/management/conftest.py.

Run from the repo root, then ``ruff format tests && ruff check --fix tests``. The moved code is
byte-for-byte what Tasks 13, 24, 25 and 27 added; the script is not committed.
"""

from pathlib import Path

CONFTEST = Path("tests/unit/management/conftest.py")
text = CONFTEST.read_text()

# The guard fixture (Task 13) comes first; everything from it to EOF is teams fixtures.
start_guard = text.index("@pytest.fixture(autouse=True)\ndef _teamless_tokens_for_mock_db_tests")
start_routes = text.index("#: Route modules whose module-level ``db`` the real-database fixture")
guard = text[start_guard:start_routes].rstrip() + "\n"
moved = text[start_routes:].rstrip() + "\n"

Path("tests/unit/management/team_fixtures.py").write_text(
    '"""Teams-subsystem fixtures for the management tests (moved out of ``conftest.py``).\n\n'
    "``conftest.py`` imports these names so pytest registers them; they live here so that file\n"
    "stays smaller. They use only fixtures defined elsewhere by name (``flask_app``,\n"
    '``team_world``, ``mint_token``).\n"""\n\n'
    "from collections.abc import AsyncGenerator\n"
    "from unittest.mock import AsyncMock, MagicMock\n\n"
    "import pytest\n\n\n" + moved + "\n\n" + guard
)

anchor = "from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext, expand_team_permissions\n"
text = text[:start_guard].rstrip() + "\n"
text = text.replace(
    anchor,
    anchor + "from tests.unit.management.team_fixtures import (  # noqa: F401 -- registers fixtures\n"
    "    SharedValkey,\n"
    "    _teamless_tokens_for_mock_db_tests,\n"
    "    real_db_client,\n"
    "    redis_spy,\n"
    "    shared_valkey,\n"
    "    team_headers,\n"
    ")\n",
)
CONFTEST.write_text(text)
```

```bash
python3 move_fixtures_once.py && rm move_fixtures_once.py
ruff format tests && ruff check --fix tests
```

Resulting import in `conftest.py`:

```python
from tests.unit.management.team_fixtures import (  # noqa: F401 -- registers fixtures
    SharedValkey,
    _teamless_tokens_for_mock_db_tests,
    real_db_client,
    redis_spy,
    shared_valkey,
    team_headers,
)
```

- [ ] **Step 5: Run the split suite and every suite that touches RBAC or the fixtures**

Run: `.venv/bin/python -m pytest tests/unit/test_auth_module_split.py tests/unit/test_team_roles.py tests/unit/test_check_permission_team.py tests/unit/test_rbac_team_resolution.py tests/unit/test_rbac.py tests/unit/test_rbac_additional.py tests/unit/management/test_scope_authz.py tests/unit/management/test_teams_routes.py tests/unit/management/test_teams_e2e.py tests/unit/management/test_key_routes.py --no-cov -q`
Expected: all pass (`29 passed` in the new file). Then `wc -c shared/auth/rbac.py` must print a number below 25000.

- [ ] **Step 6: Commit**

```bash
git add shared/auth/permissions.py shared/auth/team_rbac.py shared/auth/rbac.py tests/unit/management/team_fixtures.py tests/unit/management/conftest.py tests/unit/test_auth_module_split.py
git commit -m "refactor(auth): split rbac.py and the teams fixtures to stay under 25k characters"
```

- [ ] **Phase 7d gate:** Step 5's command passes.
