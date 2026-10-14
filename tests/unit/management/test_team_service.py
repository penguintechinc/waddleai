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
