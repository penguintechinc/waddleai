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
