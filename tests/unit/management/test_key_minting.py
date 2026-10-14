"""Dual-row proxy-key minting and revoke/rotate parity (issue #303, teams spec D10).

# regression: gh-303 -- a key created through the management plane never authenticated at the
proxy, because management minted ``wa-{secret}`` into ``virtual_keys`` while the proxy only
authenticates ``wa-{key_id}-{secret}`` rows in ``api_keys``. Real database (``team_world``);
BOTH planes' real authentication code verifies the SAME minted credential.
"""

from datetime import datetime

import pytest
from passlib.hash import bcrypt
from sqlalchemy.exc import StatementError

from services.management.app.api.v1 import auth as auth_mod
from services.management.app.services.key_minting import (
    mint_proxy_key,
    mint_rotated_credential,
    sync_linked_proxy_key,
    virtual_key_prefix,
)
from shared.auth.rbac import AuthenticationError, RBACManager, parse_wa_key_id


def _mint(w, user: str = "mia", team: str | None = "a1", **over):
    """Mint a key for ``user`` with defaults; override any keyword."""
    kwargs = {
        "user_id": w.u[user], "org_id": w.org["a"], "name": "ci", "owner_role": "user",
        "team_id": w.t[team] if team else None, "expires_at": None, "allowed_models": ["m1"],
        "allowed_providers": None, "budget_limit_daily": 5, "budget_limit_monthly": 50,
        "tpm_limit": 1000, "rpm_limit": 30,
    }  # fmt: skip
    kwargs.update(over)
    return mint_proxy_key(w.db, **kwargs)


def test_gh303_minted_key_authenticates_at_the_proxy_with_its_team(team_world) -> None:
    """# regression: gh-303 -- the minted credential passes the proxy's own auth, team intact."""
    w = team_world
    minted = _mint(w)

    ctx = RBACManager(w.db).authenticate_api_key(minted.api_key)

    assert ctx.user_id == w.u["mia"]
    assert (ctx.team_id, ctx.team_role) == (w.t["a1"], "member")
    assert parse_wa_key_id(minted.api_key) == minted.key_id


def test_gh303_same_credential_also_authenticates_at_management(team_world, monkeypatch) -> None:
    """The credential is honoured by management's own virtual-key verifier too (one secret)."""
    w = team_world
    monkeypatch.setattr(auth_mod, "db", w.db)
    minted = _mint(w)

    verified = auth_mod.verify_api_key(minted.api_key)

    assert verified is not None
    assert verified["user_id"] == w.u["mia"]
    assert verified["key_id"] == minted.virtual_key_id


def test_rows_are_linked_and_carry_the_requested_fields(team_world) -> None:
    """virtual_keys keeps limits; api_keys carries auth + team; each points at the other."""
    w = team_world
    minted = _mint(w)
    vk = w.db.virtual_keys[minted.virtual_key_id]
    ak = w.db.api_keys[minted.api_key_row_id]

    assert vk.api_key_id == ak.id
    assert (vk.allowed_models, vk.tpm_limit, vk.rpm_limit) == (["m1"], 1000, 30)
    assert vk.key_prefix == minted.key_prefix == virtual_key_prefix(minted.api_key)
    assert (ak.key_id, ak.team_id, ak.enabled, ak.rate_limit_rpm) == (
        minted.key_id, w.t["a1"], True, 30
    )  # fmt: skip
    assert ak.api_access_level == "proxy_api"
    assert bcrypt.verify(minted.api_key, ak.key_hash)


def test_virtual_key_prefix_matches_the_management_verifier_contract() -> None:
    """Our prefix must equal what ``auth._virtual_key_prefix`` expects, or lookups never match."""
    credential = "wa-0123456789abcdef-" + "s" * 43
    assert virtual_key_prefix(credential) == auth_mod._virtual_key_prefix(credential)


def test_owner_role_sets_the_access_level(team_world) -> None:
    """An admin-owned key is issued at admin_api (same mapping as RBACManager.create_api_key)."""
    minted = _mint(team_world, "boss", team=None, owner_role="admin")
    assert team_world.db.api_keys[minted.api_key_row_id].api_access_level == "admin_api"


def test_teamless_key_has_null_team(team_world) -> None:
    """team_id=None mints a teamless key that still authenticates."""
    minted = _mint(team_world, "loner", team=None)
    ctx = RBACManager(team_world.db).authenticate_api_key(minted.api_key)
    assert ctx.team_id is None


def test_failed_second_insert_leaves_no_orphan_api_key(team_world) -> None:
    """If the virtual_keys insert fails, the api_keys row is rolled back (no half-minted key)."""
    w = team_world
    before = w.db(w.db.api_keys.id > 0).count()
    with pytest.raises(StatementError):
        _mint(w, allowed_models=[object()])  # not JSON-serializable -> virtual_keys insert fails
    assert w.db(w.db.api_keys.id > 0).count() == before


def test_disable_propagates_to_the_linked_proxy_key(team_world) -> None:
    """Revoking the virtual key must disable the credential the proxy authenticates."""
    w = team_world
    minted = _mint(w)
    vk = w.db.virtual_keys[minted.virtual_key_id]

    key_id = sync_linked_proxy_key(w.db, vk, enabled=False)

    assert key_id == minted.key_id
    with pytest.raises(AuthenticationError):
        RBACManager(w.db).authenticate_api_key(minted.api_key)


def test_unlinked_legacy_key_syncs_to_nothing(team_world) -> None:
    """A pre-teams virtual key has no api_key_id: sync is a no-op and reports no key_id."""
    w = team_world
    vid = w.db.virtual_keys.insert(
        user_id=w.u["mia"], organization_id=w.org["a"], name="legacy", key_hash="x",
        key_prefix="wa-abcdefgh...", enabled=True, created_at=datetime.utcnow(),
    )  # fmt: skip
    w.db.commit()
    assert sync_linked_proxy_key(w.db, w.db.virtual_keys[vid], enabled=False) is None
    assert mint_rotated_credential(w.db, w.db.virtual_keys[vid]) is None


def test_rotation_keeps_the_key_id_and_swaps_the_secret_on_both_rows(team_world) -> None:
    """Rotate: same key_id/prefix, new secret; old credential dies, new one authenticates."""
    w = team_world
    minted = _mint(w)
    vk = w.db.virtual_keys[minted.virtual_key_id]

    rotated = mint_rotated_credential(w.db, vk)
    assert rotated is not None
    new_key, new_prefix = rotated
    assert parse_wa_key_id(new_key) == minted.key_id
    assert new_key != minted.api_key
    assert new_prefix == minted.key_prefix

    sync_linked_proxy_key(w.db, vk, key_hash=bcrypt.hash(new_key))
    w.db(w.db.virtual_keys.id == vk.id).update(key_hash=bcrypt.hash(new_key))
    w.db.commit()

    rbac = RBACManager(w.db)
    assert rbac.authenticate_api_key(new_key).user_id == w.u["mia"]
    with pytest.raises(AuthenticationError):
        rbac.authenticate_api_key(minted.api_key)
