"""Management-plane team authorization (teams spec D9): ``authorize_team``.

The management plane re-derives team permissions from the LIVE ``team_members`` row on every
call -- never from the (up to token-TTL stale) scope claim -- so a demotion or removal takes
effect immediately. Runs against ``team_world`` (real schema, no mocks).
"""

import pytest
from quart import g

from services.management.app.api.v1._team_authz import authorize_team
from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role

P = Permission


def _as(world, name: str, org: str = "a", role: str = "user", scope=None) -> dict:
    """Build the ``g.user`` dict a JWT for ``name`` would yield (scope = org bundle by default)."""
    bundle = {p.value for p in ROLE_PERMISSIONS[Role(role)]}
    return {
        "user_id": world.u[name],
        "organization_id": world.org[org],
        "role": role,
        "scope": sorted(scope if scope is not None else bundle),
    }


async def _authorize(app, world, who: dict, team_key: str, perm: Permission):
    """Run ``authorize_team`` inside a request context carrying ``who`` as ``g.user``."""
    async with app.test_request_context("/"):
        g.user = who
        return await authorize_team(world.db, world.t[team_key], perm)


async def _status(app, world, who, team_key, perm) -> int | None:
    """Return the HTTP status of a refusal, or None when access was granted."""
    access, err = await _authorize(app, world, who, team_key, perm)
    if err is None:
        assert access is not None
        return None
    return int(err[1])


async def test_org_admin_may_act_on_any_team_in_their_tenant(flask_app, team_world) -> None:
    """``team:org_admin`` is the org-layer scope: boss passes without being a member."""
    who = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, who, "a2", P.TEAM_DELETE) is None


async def test_org_admin_cannot_reach_another_tenants_team(flask_app, team_world) -> None:
    """Tenant isolation is a hard boundary: a team in org b is a 404 to org a's admin."""
    who = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, who, "b1", P.TEAM_READ) == 404


async def test_member_reads_own_team_but_cannot_administer_it(flask_app, team_world) -> None:
    """Mia (member of a1): TEAM_READ ok, TEAM_ADMIN refused with 403."""
    who = _as(team_world, "mia")
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) is None
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_ADMIN) == 403


async def test_team_admin_manages_members_but_cannot_delete(flask_app, team_world) -> None:
    """Adam (team admin): TEAM_MEMBER_MANAGE ok; TEAM_DELETE is Owner-only."""
    who = _as(team_world, "adam")
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_MEMBER_MANAGE) is None
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_DELETE) == 403


async def test_cross_team_access_is_refused(flask_app, team_world) -> None:
    """Nat (member of a2) and loner (no team) get 403 on a1, even for the weakest permission."""
    for name in ("nat", "loner"):
        who = _as(team_world, name)
        assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) == 403, name


async def test_unknown_team_is_404(flask_app, team_world) -> None:
    """A team id that does not exist is a 404, not a 403 (and not a crash)."""
    async with flask_app.test_request_context("/"):
        g.user = _as(team_world, "boss", role="admin")
        _access, err = await authorize_team(team_world.db, 99999, P.TEAM_READ)
    assert err is not None
    assert err[1] == 404


async def test_live_role_beats_a_stale_scope_claim(flask_app, team_world) -> None:
    """Vic is a viewer in the DB; a token still carrying team:admin must not grant it."""
    stale = _as(team_world, "vic", scope={P.TEAM_ADMIN.value, P.TEAM_READ.value})
    assert await _status(flask_app, team_world, stale, "a1", P.TEAM_ADMIN) == 403


async def test_removed_member_with_a_valid_token_is_refused(flask_app, team_world) -> None:
    """Membership change is effective at once: delete mia's row, her old token loses access."""
    db = team_world.db
    db(db.team_members.user_id == team_world.u["mia"]).delete()
    db.commit()
    who = _as(team_world, "mia", scope={P.TEAM_READ.value, P.TEAM_USE.value})
    assert await _status(flask_app, team_world, who, "a1", P.TEAM_READ) == 403


async def test_disabled_team_is_refused_to_members_but_not_the_org_admin(
    flask_app, team_world
) -> None:
    """A disabled team locks out its own members; the org admin can still manage/re-enable it."""
    db = team_world.db
    db(db.teams.id == team_world.t["a1"]).update(enabled=False)
    db.commit()
    assert await _status(flask_app, team_world, _as(team_world, "olive"), "a1", P.TEAM_READ) == 403
    boss = _as(team_world, "boss", role="admin")
    assert await _status(flask_app, team_world, boss, "a1", P.TEAM_ADMIN) is None


@pytest.mark.parametrize("name", ["boss", "olive"])
async def test_kill_switch_makes_every_team_route_404(
    flask_app, team_world, monkeypatch, name
) -> None:
    """``waddleai.disable-teams`` ON: the team API is off for everyone, admins included."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    role = "admin" if name == "boss" else "user"
    assert (
        await _status(flask_app, team_world, _as(team_world, name, role=role), "a1", P.TEAM_READ)
        == 404
    )
