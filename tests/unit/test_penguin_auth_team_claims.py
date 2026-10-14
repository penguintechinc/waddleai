"""Team context in the JWT ``ext`` claim bag (teams spec D6).

penguin-aaa mints the token and hard-codes the top-level ``teams`` claim, so team context rides
in ``ext.team_id`` / ``ext.team_role`` -- no wire rename, no lib change. These tests pin the
round trip through every converter plus a real mint -> verify.
"""

import pytest

from shared.auth.penguin_auth import (
    claims_dict_to_user_context,
    claims_to_user_context,
    create_oidc_provider,
    issue_token,
    user_context_to_claims,
    user_context_to_claims_dict,
    verify_token,
)
from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    Role,
    UserContext,
    expand_team_permissions,
)


def _uc(team_id: int | None = 7, team_role: str | None = "admin", managed=None) -> UserContext:
    """A USER-role context acting in team 7 as admin (org 3), with the union scope set."""
    return UserContext(
        user_id=11, username="mia", role=Role.USER, organization_id=3,
        managed_orgs=managed or [],
        permissions=set(ROLE_PERMISSIONS[Role.USER]) | expand_team_permissions(team_role),
        team_id=team_id, team_role=team_role,
    )  # fmt: skip


def test_claims_carry_team_in_ext_and_leave_legacy_teams_claim_alone() -> None:
    """``ext.team_id``/``ext.team_role`` are set; ``teams`` is still just managed_orgs."""
    claims = user_context_to_claims(_uc(managed=[2, 3]))
    assert claims.ext["team_id"] == "7"
    assert claims.ext["team_role"] == "admin"
    assert claims.teams == ["2", "3"]


def test_teamless_context_emits_null_team_ext() -> None:
    """No acting team -> explicit None values (mirrors ``api_key_id``)."""
    ext = user_context_to_claims(_uc(None, None)).ext
    assert ext["team_id"] is None
    assert ext["team_role"] is None


def test_claims_round_trip_to_user_context() -> None:
    """Claims -> UserContext restores team_id as int and team_role as str."""
    back = claims_to_user_context(user_context_to_claims(_uc()))
    assert (back.team_id, back.team_role) == (7, "admin")


def test_dict_converters_round_trip() -> None:
    """The proxy's plain-dict claims path (``scope['state']['claims']``) round-trips too."""
    d = user_context_to_claims_dict(_uc())
    assert d["team_id"] == "7"
    assert d["team_role"] == "admin"
    back = claims_dict_to_user_context(d)
    assert (back.team_id, back.team_role) == (7, "admin")


@pytest.mark.parametrize("bad_id", ["abc", "", "-1", "7.5", None, 7])
def test_malformed_ext_team_id_is_treated_as_teamless(bad_id) -> None:
    """A non-digit / missing / non-string team id never raises and never invents a team."""
    claims = user_context_to_claims(_uc())
    claims.ext["team_id"] = bad_id
    back = claims_to_user_context(claims)
    assert back.team_id is None


@pytest.mark.parametrize("bad_role", [5, ["owner"], {"r": 1}])
def test_non_string_team_role_is_dropped(bad_role) -> None:
    """A non-string ``team_role`` in ext reads back as None (and so expands to no scopes)."""
    d = user_context_to_claims_dict(_uc())
    d["team_role"] = bad_role
    assert claims_dict_to_user_context(d).team_role is None


def test_mint_then_verify_round_trips_team_through_a_real_signed_token() -> None:
    """RS256 mint (penguin-aaa) -> verify_token: team context survives the real wire format."""
    provider = create_oidc_provider()
    token = issue_token(_uc(), provider)
    back = verify_token(token, provider)
    assert (back.team_id, back.team_role) == (7, "admin")
    assert back.organization_id == 3
    assert "team:member_manage" in back.permissions
