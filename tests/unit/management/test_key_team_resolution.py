"""Which team does a newly created key act in? (teams spec §7).

The key must be bound to a team the creating principal is authorized for. Real database via
``team_world``.
"""

import pytest

from services.management.app.services.key_minting import KeyTeamError, resolve_key_team
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role

_USER_SCOPES = {p.value for p in ROLE_PERMISSIONS[Role.USER]}
_ADMIN_SCOPES = {p.value for p in ROLE_PERMISSIONS[Role.ADMIN]}


def _resolve(w, *, caller: str, target: str | None = None, team: str | None = None, org="a"):
    """Call ``resolve_key_team`` as ``caller`` creating a key for ``target`` (default: self)."""
    scopes = _ADMIN_SCOPES if caller == "boss" else _USER_SCOPES
    return resolve_key_team(
        w.db,
        org_id=w.org[org],
        target_user_id=w.u[target or caller],
        requested_team_id=w.t[team] if team else None,
        caller_user_id=w.u[caller],
        caller_scopes=scopes,
    )


def test_default_is_the_target_users_own_team(team_world) -> None:
    """No team requested: bind the key to the owner's single team."""
    assert _resolve(team_world, caller="mia") == team_world.t["a1"]


def test_default_requires_the_owner_to_be_in_a_team(team_world) -> None:
    """An owner in no team cannot get a key: 409 (every key acts in a team)."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="loner")
    assert exc.value.status == 409


def test_default_for_another_user_uses_that_users_team(team_world) -> None:
    """An org admin minting for mia without naming a team binds mia's team, not the admin's."""
    assert _resolve(team_world, caller="boss", target="mia") == team_world.t["a1"]


def test_member_may_bind_their_own_key_to_their_team(team_world) -> None:
    """Explicit team = own team: Member holds team:key_create."""
    assert _resolve(team_world, caller="mia", team="a1") == team_world.t["a1"]


def test_viewer_cannot_explicitly_bind_a_key(team_world) -> None:
    """Viewer has no team:key_create -> 403 (no spend)."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="vic", team="a1")
    assert exc.value.status == 403


def test_member_cannot_issue_for_a_teammate(team_world) -> None:
    """Issuing for ANOTHER user needs team:key_admin (Admin+) -> member gets 403."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="mia", target="vic", team="a1")
    assert exc.value.status == 403


def test_team_admin_may_issue_for_a_teammate(team_world) -> None:
    """Adam (team admin) binds a key for vic to a1."""
    assert _resolve(team_world, caller="adam", target="vic", team="a1") == team_world.t["a1"]


def test_cross_team_request_is_forbidden(team_world) -> None:
    """Nat (member of a2) asking for a1 is refused -- authorization precedes membership checks."""
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="nat", team="a1")
    assert exc.value.status == 403


def test_org_admin_may_bind_any_member_but_not_a_non_member(team_world) -> None:
    """Boss may name a1 for mia; for loner (not in a1) the key would be teamless -> 400."""
    assert _resolve(team_world, caller="boss", target="mia", team="a1") == team_world.t["a1"]
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="boss", target="loner", team="a1")
    assert exc.value.status == 400


def test_team_in_another_tenant_or_missing_is_404(team_world) -> None:
    """A team id outside the caller's tenant (or nonexistent) is a 404 -- no existence leak."""
    w = team_world
    with pytest.raises(KeyTeamError) as exc:
        resolve_key_team(
            w.db, org_id=w.org["a"], target_user_id=w.u["mia"], requested_team_id=w.t["b1"],
            caller_user_id=w.u["boss"], caller_scopes=_ADMIN_SCOPES,
        )  # fmt: skip
    assert exc.value.status == 404
    with pytest.raises(KeyTeamError) as exc2:
        resolve_key_team(
            w.db, org_id=w.org["a"], target_user_id=w.u["mia"], requested_team_id=99999,
            caller_user_id=w.u["boss"], caller_scopes=_ADMIN_SCOPES,
        )  # fmt: skip
    assert exc2.value.status == 404


def test_disabled_team_cannot_receive_new_keys(team_world) -> None:
    """A disabled team is refused with 409 even for the org admin."""
    db = team_world.db
    db(db.teams.id == team_world.t["a1"]).update(enabled=False)
    db.commit()
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="boss", target="mia", team="a1")
    assert exc.value.status == 409


def test_kill_switch_ignores_default_and_rejects_explicit(team_world, monkeypatch) -> None:
    """``waddleai.disable-teams`` ON: default is teamless; naming a team is a 404."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    assert _resolve(team_world, caller="mia") is None
    with pytest.raises(KeyTeamError) as exc:
        _resolve(team_world, caller="mia", team="a1")
    assert exc.value.status == 404


def test_permission_constants_used_are_the_team_key_pair() -> None:
    """Guard: the two permissions this policy keys on exist with their spec names."""
    assert Permission.TEAM_KEY_CREATE.value == "team:key_create"
    assert Permission.TEAM_KEY_ADMIN.value == "team:key_admin"
