# Teams Subsystem — Phase 6b: Team Service Roles, Owner Rules and Ownership Transfer

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §5 (role table: Owner = delete + transfer ownership), §9 ("an org-admin designates [an owner] afterward via the team API"), §10.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 6a.

---

<!-- TASK 22b -->
## Task 23: `set_member_role`, `remove_member`, `transfer_ownership`

**Files:**
- Modify: `services/management/app/services/teams.py`
- Test: `tests/unit/management/test_team_service_roles.py` (create)

**Interfaces:**
- Consumes: Task 22's helpers (`get_member`, `member_user_ids`) and `TeamError`.
- Produces: `set_member_role(database, team, user_id, new_role, *, can_manage_owner) -> list[int]`; `remove_member(database, team, user_id, *, can_manage_owner) -> list[int]`; `transfer_ownership(database, team, new_owner_id) -> list[int]`; private `_owner_ids`.
- Rules, each pinned by a test:
  - **Privilege-escalation guard:** granting, holding-then-editing, or removing `owner` needs `can_manage_owner` (the org admin or a live team Owner) -- a team Admin gets 403 for `set role -> owner`, editing the Owner, or removing the Owner.
  - **A team that has an owner always keeps one:** demoting or removing the **last** owner is a 409 ("transfer ownership instead"). The migration seats no owner (spec §9), so a team with *no* owner can have one designated by an authorized actor.
  - `remove_member` NULLs the keys that user had bound to this team (so re-adding later does not silently revive them); a non-member target is 404.
  - `transfer_ownership` makes the target the sole owner and demotes every previous owner to admin (the target must already be a member, else 404 and nothing changes); it reports every user it staled.

- [ ] **Step 1: Write the failing test**

```python
"""Team service layer, part 2 (teams spec §5): roles, owner rules, removal, ownership transfer.

Pure database logic over ``team_world`` (real schema, no mocks). The owner rules are the
privilege-escalation guard: only an owner-capable actor may grant, edit or remove ``owner``,
and a team that has an owner always keeps one.
"""

import pytest

from services.management.app.services.teams import (
    TeamError,
    get_member,
    remove_member,
    set_member_role,
    transfer_ownership,
)


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


# ---------------------------------------------------------------- roles / owners


def test_set_member_role_changes_the_role_and_reports_the_user(team_world) -> None:
    """Mia member -> admin."""
    w = team_world
    assert set_member_role(w.db, _team(w, "a1"), w.u["mia"], "admin", can_manage_owner=False) == [
        w.u["mia"]
    ]
    assert _role(w, "mia") == "admin"


def test_non_owner_actor_cannot_grant_owner_or_touch_an_owner(team_world) -> None:
    """Privilege escalation guard: team admin cannot mint an owner nor edit/remove the owner."""
    w = team_world
    for call in (
        lambda: set_member_role(w.db, _team(w, "a1"), w.u["mia"], "owner", can_manage_owner=False),
        lambda: set_member_role(
            w.db, _team(w, "a1"), w.u["olive"], "member", can_manage_owner=False
        ),
        lambda: remove_member(w.db, _team(w, "a1"), w.u["olive"], can_manage_owner=False),
    ):
        with pytest.raises(TeamError) as exc:
            call()
        assert exc.value.status == 403
    assert _role(w, "olive") == "owner"
    assert _role(w, "mia") == "member"


def test_the_last_owner_cannot_be_demoted_or_removed(team_world) -> None:
    """A team that has an owner always keeps one (use transfer-ownership instead)."""
    w = team_world
    for call in (
        lambda: set_member_role(w.db, _team(w, "a1"), w.u["olive"], "admin", can_manage_owner=True),
        lambda: remove_member(w.db, _team(w, "a1"), w.u["olive"], can_manage_owner=True),
    ):
        with pytest.raises(TeamError) as exc:
            call()
        assert exc.value.status == 409


def test_a_team_with_no_owner_can_have_one_designated(team_world) -> None:
    """The migration seats no owner; an authorized actor can designate one afterwards."""
    w = team_world
    set_member_role(w.db, _team(w, "a2"), w.u["nat"], "owner", can_manage_owner=True)
    assert _role(w, "nat") == "owner"


def test_set_role_on_a_non_member_is_404(team_world) -> None:
    """Changing the role of someone outside the team is a 404."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        set_member_role(w.db, _team(w, "a1"), w.u["nat"], "admin", can_manage_owner=True)
    assert exc.value.status == 404


def test_remove_member_unbinds_that_users_team_keys(team_world) -> None:
    """Removing mia NULLs her a1-bound key (so re-adding later does not silently revive it)."""
    w = team_world
    _add_key(w, "mia", "a1", "k-mia")
    assert remove_member(w.db, _team(w, "a1"), w.u["mia"], can_manage_owner=False) == [w.u["mia"]]
    assert get_member(w.db, w.t["a1"], w.u["mia"]) is None
    assert _key_team(w, "k-mia") is None


def test_transfer_ownership_swaps_owner_and_demotes_the_old_one(team_world) -> None:
    """Olive -> adam: adam is owner, olive becomes admin; both reported stale."""
    w = team_world
    stale = transfer_ownership(w.db, _team(w, "a1"), w.u["adam"])
    assert set(stale) == {w.u["olive"], w.u["adam"]}
    assert (_role(w, "adam"), _role(w, "olive")) == ("owner", "admin")


def test_transfer_to_a_non_member_is_404_and_changes_nothing(team_world) -> None:
    """The new owner must already be in the team."""
    w = team_world
    with pytest.raises(TeamError) as exc:
        transfer_ownership(w.db, _team(w, "a1"), w.u["nat"])
    assert exc.value.status == 404
    assert _role(w, "olive") == "owner"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_service_roles.py --no-cov -q -x`
Expected: collection error `ImportError: cannot import name 'remove_member' from 'services.management.app.services.teams'`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/services/teams.py
+++ b/services/management/app/services/teams.py
@@ -67,6 +67,14 @@
 def member_user_ids(database: DB, team_id: int) -> list[int]:
     """Return the user ids of every member of ``team_id``."""
     rows = database(database.team_members.team_id == team_id).select()
+    return [int(r.user_id) for r in rows]
+
+
+def _owner_ids(database: DB, team_id: int) -> list[int]:
+    """Return the user ids currently holding ``owner`` in ``team_id``."""
+    rows = database(
+        (database.team_members.team_id == team_id) & (database.team_members.team_role == "owner")
+    ).select()
     return [int(r.user_id) for r in rows]


@@ -173,3 +181,56 @@
     ).update(team_id=team.id)
     database.commit()
     return [user_id]
+
+
+def set_member_role(
+    database: DB, team: Any, user_id: int, new_role: object, *, can_manage_owner: bool
+) -> list[int]:
+    """Change a member's role; ``owner`` is granted/edited only by an owner-capable actor."""
+    if not isinstance(new_role, str) or new_role not in _ROLES:
+        raise TeamError(f"team_role must be one of {sorted(_ROLES)}", 400)
+    member = get_member(database, team.id, user_id)
+    if member is None:
+        raise TeamError("User is not a member of this team", 404)
+    touches_owner = new_role == "owner" or member.team_role == "owner"
+    if touches_owner and not can_manage_owner:
+        raise TeamError("Only a team owner or organization admin may grant or change owner", 403)
+    last_owner = len(_owner_ids(database, team.id)) <= 1
+    if member.team_role == "owner" and new_role != "owner" and last_owner:
+        raise TeamError("A team must keep an owner; transfer ownership instead", 409)
+    database(database.team_members.id == member.id).update(team_role=new_role)
+    database.commit()
+    return [user_id]
+
+
+def remove_member(database: DB, team: Any, user_id: int, *, can_manage_owner: bool) -> list[int]:
+    """Remove a member and unbind the keys they had bound to this team."""
+    member = get_member(database, team.id, user_id)
+    if member is None:
+        raise TeamError("User is not a member of this team", 404)
+    if member.team_role == "owner":
+        if not can_manage_owner:
+            raise TeamError("Only a team owner or organization admin may remove an owner", 403)
+        if len(_owner_ids(database, team.id)) <= 1:
+            raise TeamError("A team must keep an owner; transfer ownership instead", 409)
+    database(
+        (database.api_keys.user_id == user_id) & (database.api_keys.team_id == team.id)
+    ).update(team_id=None)
+    database(database.team_members.id == member.id).delete()
+    database.commit()
+    return [user_id]
+
+
+def transfer_ownership(database: DB, team: Any, new_owner_id: int) -> list[int]:
+    """Make ``new_owner_id`` the sole owner; every previous owner becomes admin."""
+    member = get_member(database, team.id, new_owner_id)
+    if member is None:
+        raise TeamError("New owner must already be a member of this team", 404)
+    previous = [u for u in _owner_ids(database, team.id) if u != new_owner_id]
+    for user_id in previous:
+        database(
+            (database.team_members.team_id == team.id) & (database.team_members.user_id == user_id)
+        ).update(team_role="admin")
+    database(database.team_members.id == member.id).update(team_role="owner")
+    database.commit()
+    return [new_owner_id, *previous]
```

- [ ] **Step 4: Run both service test files**

Run: `.venv/bin/python -m pytest tests/unit/management/test_team_service.py tests/unit/management/test_team_service_roles.py --no-cov -q`
Expected: `28 passed`.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/teams.py tests/unit/management/test_team_service_roles.py
git commit -m "feat(teams): role changes, owner rules, removal and ownership transfer"
```

- [ ] **Phase 6b gate:** Step 4 passes.
