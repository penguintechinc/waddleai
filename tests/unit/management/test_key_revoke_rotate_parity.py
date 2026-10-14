"""Revoke / disable / rotate on a management key must reach the proxy credential (issue #303).

Before 024 a management key never authenticated at the proxy, so revoking it was moot. Now it
does, so a revoked virtual key that stays usable at the proxy would be a security regression.
Real database (``team_world``); the proxy side is the real ``RBACManager``.
"""

from unittest.mock import MagicMock

import pytest

import services.management.app.extensions as ext_mod
from services.management.app.services.key_minting import mint_proxy_key
from shared.auth.rbac import AuthenticationError, RBACManager, auth_cache_key


@pytest.fixture
def redis(monkeypatch) -> MagicMock:
    """A recording stand-in for management's Valkey client."""
    client = MagicMock()
    monkeypatch.setattr(ext_mod, "redis_client", client)
    return client


def _owner_headers(mint_token, w, user: str = "mia") -> dict:
    """Bearer header for ``user`` (a plain org user who owns the key under test)."""
    token = mint_token(role="user", user_id=w.u[user], org_id=w.org["a"])
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _mint(w, user: str = "mia"):
    """Mint a linked key for ``user`` into their own team context."""
    return mint_proxy_key(
        w.db, user_id=w.u[user], org_id=w.org["a"], name="ci", owner_role="user",
        team_id=w.t["a1"], expires_at=None, allowed_models=None, allowed_providers=None,
        budget_limit_daily=None, budget_limit_monthly=None, tpm_limit=None, rpm_limit=None,
    )  # fmt: skip


async def test_delete_revokes_the_proxy_credential_and_invalidates_its_cache(
    team_world, real_db_client, mint_token, redis
) -> None:
    """DELETE /keys/<id>: the proxy rejects the credential and its cache entry is dropped."""
    w = team_world
    minted = _mint(w)
    assert RBACManager(w.db).authenticate_api_key(minted.api_key).user_id == w.u["mia"]

    resp = await real_db_client.delete(
        f"/api/v1/keys/{minted.virtual_key_id}", headers=_owner_headers(mint_token, w)
    )

    assert resp.status_code == 200
    with pytest.raises(AuthenticationError):
        RBACManager(w.db).authenticate_api_key(minted.api_key)
    redis.delete.assert_called_once_with(auth_cache_key(minted.key_id))


async def test_disable_then_enable_round_trips_on_the_proxy(
    team_world, real_db_client, mint_token, redis
) -> None:
    """PUT enabled=false locks the credential out; enabled=true restores it; both invalidate."""
    w = team_world
    minted = _mint(w)
    hdr = _owner_headers(mint_token, w)

    off = await real_db_client.put(
        f"/api/v1/keys/{minted.virtual_key_id}", json={"enabled": False}, headers=hdr
    )
    assert off.status_code == 200
    with pytest.raises(AuthenticationError):
        RBACManager(w.db).authenticate_api_key(minted.api_key)

    on = await real_db_client.put(
        f"/api/v1/keys/{minted.virtual_key_id}", json={"enabled": True}, headers=hdr
    )
    assert on.status_code == 200
    assert RBACManager(w.db).authenticate_api_key(minted.api_key).user_id == w.u["mia"]
    assert redis.delete.call_count == 2


async def test_non_enabled_update_does_not_touch_the_proxy_key(
    team_world, real_db_client, mint_token, redis
) -> None:
    """Renaming a key leaves the proxy credential and cache alone."""
    w = team_world
    minted = _mint(w)
    resp = await real_db_client.put(
        f"/api/v1/keys/{minted.virtual_key_id}", json={"name": "renamed"},
        headers=_owner_headers(mint_token, w),
    )  # fmt: skip
    assert resp.status_code == 200
    redis.delete.assert_not_called()
    assert RBACManager(w.db).authenticate_api_key(minted.api_key).user_id == w.u["mia"]


async def test_rotate_swaps_the_secret_on_the_proxy_credential(
    team_world, real_db_client, mint_token, redis
) -> None:
    """POST /rotate: same key_id/prefix, old secret dead at the proxy, new one authenticates."""
    w = team_world
    minted = _mint(w)

    resp = await real_db_client.post(
        f"/api/v1/keys/{minted.virtual_key_id}/rotate", headers=_owner_headers(mint_token, w)
    )

    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["api_key"] != minted.api_key
    assert body["key_prefix"] == minted.key_prefix
    rbac = RBACManager(w.db)
    assert rbac.authenticate_api_key(body["api_key"]).team_id == w.t["a1"]
    with pytest.raises(AuthenticationError):
        rbac.authenticate_api_key(minted.api_key)
    redis.delete.assert_called_once_with(auth_cache_key(minted.key_id))


async def test_legacy_unlinked_key_revoke_and_rotate_do_not_touch_the_cache(
    team_world, real_db_client, mint_token, redis
) -> None:
    """A pre-teams ``wa-{secret}`` virtual key (no link) revokes/rotates as before, no cache I/O."""
    w = team_world
    vid = w.db.virtual_keys.insert(
        user_id=w.u["mia"], organization_id=w.org["a"], name="legacy", key_hash="x",
        key_prefix="wa-abcdefgh...", enabled=True,
    )  # fmt: skip
    w.db.commit()
    hdr = _owner_headers(mint_token, w)

    rotated = await real_db_client.post(f"/api/v1/keys/{vid}/rotate", headers=hdr)
    revoked = await real_db_client.delete(f"/api/v1/keys/{vid}", headers=hdr)

    assert rotated.status_code == 200
    assert (await rotated.get_json())["api_key"].startswith("wa-")
    assert revoked.status_code == 200
    redis.delete.assert_not_called()


async def test_dangling_api_key_link_is_logged_not_silent(
    team_world, real_db_client, mint_token, redis, caplog
) -> None:
    """A virtual key whose ``api_key_id`` points at a missing row logs an error on revoke."""
    w = team_world
    minted = _mint(w)
    w.db(w.db.virtual_keys.id == minted.virtual_key_id).update(api_key_id=999999)
    w.db.commit()

    with caplog.at_level("ERROR"):
        resp = await real_db_client.delete(
            f"/api/v1/keys/{minted.virtual_key_id}", headers=_owner_headers(mint_token, w)
        )

    assert resp.status_code == 200
    assert "missing api_keys row" in caplog.text
    redis.delete.assert_not_called()
