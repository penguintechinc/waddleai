"""Credential minting shared by both planes + team-bound ``create_api_key`` (teams spec §7, D10).

``mint_wa_credential`` is the single place a ``wa-{key_id}-{secret}`` credential is born, so the
management endpoint and ``RBACManager.create_api_key`` cannot drift from what the proxy parses.
"""

import re

import pytest

from shared.auth.rbac import (
    ROLE_PERMISSIONS,
    RBACManager,
    Role,
    UserContext,
    access_level_for_role,
    mint_wa_credential,
    parse_wa_key_id,
)

_FORMAT = re.compile(r"^wa-[0-9a-f]{16}-[A-Za-z0-9_-]{43}$")


def test_minted_credential_has_the_proxy_parsable_shape() -> None:
    """``wa-{16 hex key_id}-{43-char urlsafe secret}``; the proxy parser recovers the key_id."""
    for _ in range(50):
        key_id, api_key = mint_wa_credential()
        assert _FORMAT.match(api_key), api_key
        assert parse_wa_key_id(api_key) == key_id
        assert "-" not in key_id


def test_minted_credentials_are_unique() -> None:
    """No collisions across a batch (key_id is the unique api_keys column)."""
    ids = {mint_wa_credential()[0] for _ in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize(
    ("role", "level"),
    [("admin", "admin_api"), ("resource_manager", "management_api"),
     ("reporter", "proxy_api"), ("user", "proxy_api"), ("bogus", "proxy_api")],
)  # fmt: skip
def test_access_level_for_role(role, level) -> None:
    """Admin -> admin_api, resource_manager -> management_api, everything else proxy_api."""
    assert access_level_for_role(role) == level


def test_create_api_key_binds_the_team_and_authenticates_with_it(team_world) -> None:
    """A key created for mia with team_id=a1 authenticates at the proxy as a1/member."""
    w = team_world
    owner = UserContext(
        user_id=w.u["mia"], username="mia", role=Role.USER, organization_id=w.org["a"],
        managed_orgs=[], permissions=ROLE_PERMISSIONS[Role.USER],
    )  # fmt: skip
    rbac = RBACManager(w.db)
    api_key, row_id = rbac.create_api_key(owner, name="ci", team_id=w.t["a1"])
    w.db.commit()

    assert w.db.api_keys[row_id].team_id == w.t["a1"]
    ctx = rbac.authenticate_api_key(api_key)
    assert (ctx.team_id, ctx.team_role) == (w.t["a1"], "member")


def test_create_api_key_without_team_stays_teamless(team_world) -> None:
    """Default behaviour unchanged: no team_id -> NULL column -> teamless context."""
    w = team_world
    owner = UserContext(
        user_id=w.u["mia"], username="mia", role=Role.USER, organization_id=w.org["a"],
        managed_orgs=[], permissions=ROLE_PERMISSIONS[Role.USER],
    )  # fmt: skip
    rbac = RBACManager(w.db)
    api_key, row_id = rbac.create_api_key(owner, name="ci")
    w.db.commit()
    assert w.db.api_keys[row_id].team_id is None
    assert rbac.authenticate_api_key(api_key).team_id is None
