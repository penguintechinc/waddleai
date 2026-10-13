# Teams Subsystem — Phase 6a: Team Service Layer (CRUD, Membership, D2)

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §3 D1-D3, §5 (Membership), §10, §11 (Unit).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-5 (`team_world`, `teams`/`team_members`, key binding).

The route handlers (Phases 6c-6d) authorize first, then call these pure functions. Keeping the rules out of the HTTP layer makes them testable against a real database with no Quart app (the route tests then only prove authorization and wiring).

---

<!-- TASK 22 -->
## Task 22: `services/teams.py` -- create/update/delete a team and add a member

**Files:**
- Create: `services/management/app/services/teams.py`
- Test: `tests/unit/management/test_team_service.py` (create)

**Interfaces:**
- Consumes: `TeamRole` (role validation), fixture `team_world`.
- Produces: `TeamError(message, status)`; `create_team(database, org_id, name) -> int`; `update_team(database, team, *, name, enabled) -> list[int]`; `delete_team(database, team) -> list[int]`; `add_member(database, team, user_id, team_role, *, can_manage_owner=False) -> list[int]`; helpers `get_member(database, team_id, user_id)` and `member_user_ids(database, team_id)`. Every mutator returns the **user ids whose proxy auth-cache entries it staled** (D7) so the caller can pass them to `invalidate_user_auth_cache`.
- Rules, each pinned by a test:
  - **Names:** 1-255 chars after trimming (400); a duplicate in the same org is 409, the same name in another org is fine; renaming into a clash is 409.
  - **D2 (Review Focus: a user already in a team added to a second team in the same org):** `add_member` returns a clear 409 naming the clash; the `UNIQUE(user_id, organization_id)` constraint still backstops a concurrent add (an `IntegrityError` is mapped to the same 409).
  - **Tenancy:** the user must belong to the team's org or manage it (`users.managed_orgs`: a resource manager may hold one team in each managed org); a disabled user is 400; unknown user 404; a role outside owner/admin/member/viewer is 400.
  - **D3 key binding:** joining a team adopts the user's unbound (`team_id IS NULL`) keys in that tenant -- never one already bound elsewhere; deleting a team NULLs the keys that pointed at it.
  - **Owner guard:** granting `owner` at add time needs `can_manage_owner` (403 otherwise) -- a team Admin must not be able to mint an owner.
  - Disabling/enabling a team stales every member's cache entry; a plain rename stales nobody.

- [ ] **Step 1: Write the failing test**

```python
"""Team service layer, part 1 (teams spec §5, §10): CRUD, membership, D2, key binding.

Pure database logic over ``team_world`` (real schema, no mocks). Every mutator returns the user
ids whose proxy auth-cache entries it staled, so the route layer can invalidate them (D7).
"""

import pytest

from services.management.app.services.teams import (
    TeamError,
    add_member,
    create_team,
    delete_team,
    update_team,
)

_PLACEHOLDER_HASH = "x"  # noqa: S105 -- placeholder, never a loginable hash


def _team(w, key: str):
    """Fetch a team row by the fixture key."""
    return w.db.teams[w.t[key]]


def _role(w, user: str) -> str | None:
    """Return a user's current team role (or None when they are in no team)."""
    row = w.db(w.db.team_members.user_id == w.u[user]).select().first()
    return row.team_role if row else None


def _add_key(w, user: str, team_key: str | None, key_id: str) -> None:
    """Insert an api_keys row for ``user`` bound to ``team_key`` (or NULL)."""
    w.db.api_keys.insert(
        key_id=key_id, key_hash="h", user_id=w.u[user],
        organization_id=w.db.users[w.u[user]].organization_id, name=key_id, enabled=True,
        team_id=w.t[team_key] if team_key else None,
    )  # fmt: skip
    w.db.commit()


def _key_team(w, key_id: str):
    """Return ``api_keys.team_id`` for ``key_id``."""
    return w.db(w.db.api_keys.key_id == key_id).select().first().team_id


# ---------------------------------------------------------------- create / update / delete


def test_create_team_trims_and_returns_id(team_world) -> None:
    """A valid name creates an enabled team in the given org."""
    w = team_world
    team_id = create_team(w.db, w.org["a"], "  Ops  ")
    row = w.db.teams[team_id]
    assert (row.name, row.organization_id, row.enabled) == ("Ops", w.org["a"], True)


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 256])
def test_create_team_rejects_blank_or_oversized_names(team_world, bad) -> None:
    """Names are 1-255 chars after trimming."""
    with pytest.raises(TeamError) as exc:
        create_team(team_world.db, team_world.org["a"], bad)
    assert exc.value.status == 400


def test_duplicate_team_name_in_one_org_is_409_but_fine_across_orgs(team_world) -> None:
    """UNIQUE(org, name): 'Alpha' exists in org a (409); the same name in org b is allowed."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        create_team(w.db, w.org["a"], "Alpha")
    assert exc.value.status == 409
    assert create_team(w.db, w.org["b"], "Alpha")


def test_rename_conflict_is_409(team_world) -> None:
    """Renaming a1 to a2's name collides within the org."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        update_team(w.db, _team(w, "a1"), name="Beta", enabled=None)
    assert exc.value.status == 409


def test_enabling_flag_change_stales_every_members_cache(team_world) -> None:
    """Disabling a team changes every member's resolved acting team -> all member ids returned."""
    w = team_world
    stale = update_team(w.db, _team(w, "a1"), name=None, enabled=False)
    assert set(stale) == {w.u[n] for n in ("olive", "adam", "mia", "vic")}
    assert update_team(w.db, _team(w, "a1"), name="Renamed", enabled=None) == []


def test_delete_team_unbinds_keys_and_reports_members(team_world) -> None:
    """Deleting a team removes memberships, NULLs keys that pointed at it, reports who staled."""
    w = team_world
    _add_key(w, "mia", "a1", "k-mia")
    _add_key(w, "nat", "a2", "k-nat")
    stale = delete_team(w.db, _team(w, "a1"))
    assert set(stale) == {w.u[n] for n in ("olive", "adam", "mia", "vic")}
    assert w.db.teams[w.t["a1"]] is None
    assert w.db(w.db.team_members.team_id == w.t["a1"]).count() == 0
    assert _key_team(w, "k-mia") is None
    assert _key_team(w, "k-nat") == w.t["a2"]


# ---------------------------------------------------------------- add_member (D2)


def test_add_member_creates_membership_and_binds_unbound_keys(team_world) -> None:
    """Loner joins a2 as member; their NULL-team key now acts in a2 (D3)."""
    w = team_world
    _add_key(w, "loner", None, "k-loner")
    assert add_member(w.db, _team(w, "a2"), w.u["loner"], "member") == [w.u["loner"]]
    assert _role(w, "loner") == "member"
    assert _key_team(w, "k-loner") == w.t["a2"]


def test_add_member_does_not_rebind_a_key_already_bound_elsewhere(team_world) -> None:
    """Only NULL-team keys are adopted; a key pinned to another team is left alone."""
    w = team_world
    _add_key(w, "loner", "a1", "k-pinned")
    add_member(w.db, _team(w, "a2"), w.u["loner"], "member")
    assert _key_team(w, "k-pinned") == w.t["a1"]


def test_user_already_in_a_team_cannot_join_a_second_in_the_same_org(team_world) -> None:
    """D2 / review focus: mia (a1) added to a2 (same org) is a 409 naming the clash."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        add_member(w.db, _team(w, "a2"), w.u["mia"], "member")
    assert exc.value.status == 409
    assert "already" in exc.value.message
    assert _role(w, "mia") == "member"
    assert w.db(w.db.team_members.user_id == w.u["mia"]).count() == 1


def test_add_member_from_another_org_is_rejected(team_world) -> None:
    """Carl (org b) cannot be added to org a's team -- tenant boundary."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        add_member(w.db, _team(w, "a2"), w.u["carl"], "member")
    assert exc.value.status == 400


def test_resource_manager_may_join_one_team_in_a_managed_org(team_world) -> None:
    """D2 is per (user, tenant): a user managing org b can hold a team there too."""
    w = team_world
    rm = w.db.users.insert(
        username="rm",
        email="rm@example.com",
        password_hash=_PLACEHOLDER_HASH,
        role="resource_manager",
        enabled=True,
        organization_id=w.org["a"],
        managed_orgs=[w.org["b"]],
    )
    w.db.commit()
    add_member(w.db, _team(w, "a2"), rm, "member")
    assert add_member(w.db, w.db.teams[w.t["b1"]], rm, "viewer") == [rm]


@pytest.mark.parametrize("bad", ["", "OWNER", "root", None])
def test_add_member_rejects_unknown_roles(team_world, bad) -> None:
    """Only exact owner/admin/member/viewer are accepted."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        add_member(w.db, _team(w, "a2"), w.u["loner"], bad)
    assert exc.value.status == 400


def test_adding_a_member_as_owner_needs_an_owner_capable_actor(team_world) -> None:
    """A team admin cannot mint an owner at add time either (same guard as role edits)."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        add_member(w.db, _team(w, "a2"), w.u["loner"], "owner")
    assert exc.value.status == 403
    assert add_member(w.db, _team(w, "a2"), w.u["loner"], "owner", can_manage_owner=True)
    assert _role(w, "loner") == "owner"


def test_add_unknown_or_disabled_user_is_rejected(team_world) -> None:
    """A missing user is 404; a disabled one is 400."""
    w = team_world
    with pytest.raises(TeamError) as missing:
        add_member(w.db, _team(w, "a2"), 99999, "member")
    assert missing.value.status == 404
    w.db(w.db.users.id == w.u["loner"]).update(enabled=False)
    w.db.commit()
    with pytest.raises(TeamError) as disabled:
        add_member(w.db, _team(w, "a2"), w.u["loner"], "member")
    assert disabled.value.status == 400
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_service.py --no-cov -q -x`
Expected: collection error `ModuleNotFoundError: No module named 'services.management.app.services.teams'`.

- [ ] **Step 3: Implement**

```python
"""Team and membership business rules (teams spec §5, §10; D2/D3/D7).

Pure database logic with no HTTP: the routes in ``api/v1/teams.py`` authorize first, then call
these. Every mutator that changes who acts in which team returns the user ids whose proxy
auth-cache entries it staled, so the caller can invalidate them (D7).

Decisions encoded here (each has a test in ``test_team_service.py``):

* D2 -- one team per (user, tenant): checked here for a clear 409 and enforced again by the
  ``UNIQUE(user_id, organization_id)`` constraint, which also closes a concurrent-add race.
* D3 -- joining a team adopts the user's unbound (NULL-team) keys; leaving or deleting unbinds.
* A team that has an owner always keeps one; only an owner (or org admin) may grant, hold, edit
  or remove ``owner`` -- a team Admin cannot escalate.
"""

from datetime import datetime
from typing import Any

from penguin_dal.db import DB
from sqlalchemy.exc import IntegrityError

from shared.auth.rbac import TeamRole

_ROLES = frozenset(r.value for r in TeamRole)
_MAX_NAME = 255


class TeamError(Exception):
    """A team rule violation; carries the HTTP status the route should answer with."""

    def __init__(self, message: str, status: int) -> None:
        """Record the user-facing message and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _clean_name(name: object) -> str:
    """Trim a team name and enforce 1-255 characters."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned or len(cleaned) > _MAX_NAME:
        raise TeamError(f"name must be 1-{_MAX_NAME} characters", 400)
    return cleaned


def _managed_orgs(user: Any) -> list[int]:
    """Normalize ``users.managed_orgs`` (JSON list, CSV string or None) to ``list[int]``."""
    raw = user.managed_orgs
    if not raw:
        return []
    if isinstance(raw, str):
        return [int(x) for x in raw.replace("|", ",").split(",") if x.strip().isdigit()]
    return [int(x) for x in raw]


def get_member(database: DB, team_id: int, user_id: int) -> Any:
    """Return the ``team_members`` row for (team, user), or ``None``."""
    return (
        database(
            (database.team_members.team_id == team_id) & (database.team_members.user_id == user_id)
        )
        .select()
        .first()
    )


def member_user_ids(database: DB, team_id: int) -> list[int]:
    """Return the user ids of every member of ``team_id``."""
    rows = database(database.team_members.team_id == team_id).select()
    return [int(r.user_id) for r in rows]


def _name_taken(database: DB, org_id: int, name: str, exclude_id: int | None = None) -> bool:
    """True if another team in ``org_id`` already uses ``name``."""
    query = (database.teams.organization_id == org_id) & (database.teams.name == name)
    if exclude_id is not None:
        query = query & (database.teams.id != exclude_id)
    return bool(database(query).count())


def create_team(database: DB, org_id: int, name: object) -> int:
    """Create an enabled team in ``org_id`` and return its id (409 on a duplicate name)."""
    cleaned = _clean_name(name)
    if _name_taken(database, org_id, cleaned):
        raise TeamError("A team with that name already exists in this organization", 409)
    try:
        team_id = database.teams.insert(
            organization_id=org_id, name=cleaned, enabled=True, created_at=datetime.utcnow()
        )
    except IntegrityError as exc:
        raise TeamError("A team with that name already exists in this organization", 409) from exc
    database.commit()
    return int(team_id)


def update_team(database: DB, team: Any, *, name: object | None, enabled: bool | None) -> list[int]:
    """Rename and/or enable/disable ``team``; return members staled by an enabled change."""
    fields: dict[str, Any] = {}
    if name is not None:
        cleaned = _clean_name(name)
        if cleaned != team.name and _name_taken(database, team.organization_id, cleaned, team.id):
            raise TeamError("A team with that name already exists in this organization", 409)
        fields["name"] = cleaned
    was_enabled = team.enabled is not False
    if enabled is not None and enabled != was_enabled:
        fields["enabled"] = enabled
    if fields:
        database(database.teams.id == team.id).update(**fields)
        database.commit()
    return member_user_ids(database, team.id) if "enabled" in fields else []


def delete_team(database: DB, team: Any) -> list[int]:
    """Delete ``team``: unbind its keys, drop memberships; return the members it staled."""
    stale = member_user_ids(database, team.id)
    database(database.api_keys.team_id == team.id).update(team_id=None)
    database(database.team_members.team_id == team.id).delete()
    database(database.teams.id == team.id).delete()
    database.commit()
    return stale


def add_member(
    database: DB, team: Any, user_id: int, team_role: object, *, can_manage_owner: bool = False
) -> list[int]:
    """Add ``user_id`` to ``team`` (D2: 409 if they already have a team in this tenant).

    Granting ``owner`` needs an owner-capable actor (``can_manage_owner``), like role edits.
    """
    if not isinstance(team_role, str) or team_role not in _ROLES:
        raise TeamError(f"team_role must be one of {sorted(_ROLES)}", 400)
    if team_role == "owner" and not can_manage_owner:
        raise TeamError("Only a team owner or organization admin may grant owner", 403)
    user = database(database.users.id == user_id).select().first()
    if user is None:
        raise TeamError("User not found", 404)
    if user.enabled is False:
        raise TeamError("User is disabled", 400)
    if user.organization_id != team.organization_id and team.organization_id not in _managed_orgs(
        user
    ):
        raise TeamError("User does not belong to this team's organization", 400)

    clash = (
        database(
            (database.team_members.user_id == user_id)
            & (database.team_members.organization_id == team.organization_id)
        )
        .select()
        .first()
    )
    if clash is not None:
        raise TeamError(
            f"User is already a member of team {clash.team_id} in this organization; "
            "remove them from it first (one team per user per organization)",
            409,
        )
    try:
        database.team_members.insert(
            team_id=team.id,
            user_id=user_id,
            organization_id=team.organization_id,
            team_role=team_role,
            created_at=datetime.utcnow(),
        )
    except IntegrityError as exc:  # lost a concurrent-add race to the UNIQUE constraint
        raise TeamError("User is already a member of a team in this organization", 409) from exc
    # D3: keys the user already owns in this tenant with no acting team now act in this one.
    database(
        (database.api_keys.user_id == user_id)
        & (database.api_keys.organization_id == team.organization_id)
        & (database.api_keys.team_id == None)  # noqa: E711 -- SQL IS NULL, not a Python compare
    ).update(team_id=team.id)
    database.commit()
    return [user_id]
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_service.py --no-cov -q`
Expected: `20 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/teams.py tests/unit/management/test_team_service.py
git commit -m "feat(teams): team service layer for CRUD and membership (one team per user per org)"
```

- [ ] **Phase 6a gate:** Step 4 passes.
