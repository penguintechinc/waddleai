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
