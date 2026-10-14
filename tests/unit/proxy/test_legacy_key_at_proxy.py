"""A legacy ``wa-{secret}`` virtual key reaching the proxy fails cleanly (issue #303 review focus).

Pre-fix management keys have no ``api_keys`` row, so the proxy can never authenticate them. The
requirement is a clean ``AuthenticationError`` (-> 401) -- never a 500, never a team context --
whatever shape the legacy secret takes (urlsafe base64 may or may not contain ``-``).
"""

from __future__ import annotations

import pytest

from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
from shared.auth.rbac import AuthenticationError, RBACManager

pytestmark = pytest.mark.asyncio

#: Secrets as ``secrets.token_urlsafe(32)`` yields them: one without ``-``, one with.
_NO_DASH = "wa-" + "A" * 43
_WITH_DASHES = "wa-Qx9-" + "B" * 20 + "-" + "C" * 20


@pytest.mark.parametrize("credential", [_NO_DASH, _WITH_DASHES, "wa-", "wa--x-y", "sk-abc"])
async def test_legacy_credential_is_rejected_with_authentication_error(
    team_world, credential
) -> None:
    """Every legacy/odd shape -> AuthenticationError (401), and nothing is cached as a hit."""
    cache = ApiKeyAuthCache(valkey=None)
    auth = ApiKeyAuthenticator(rbac=RBACManager(team_world.db), cache=cache)
    with pytest.raises(AuthenticationError):
        await auth.authenticate(credential)


async def test_legacy_key_cannot_borrow_a_real_keys_team(team_world) -> None:
    """A legacy credential whose first segment equals a REAL key_id still fails the secret check."""
    from passlib.hash import bcrypt

    w = team_world
    w.db.api_keys.insert(
        key_id="deadbeefdeadbeef", key_hash=bcrypt.hash("wa-deadbeefdeadbeef-realsecret"),
        user_id=w.u["mia"], organization_id=w.org["a"], name="k", enabled=True,
        team_id=w.t["a1"],
    )  # fmt: skip
    w.db.commit()
    auth = ApiKeyAuthenticator(rbac=RBACManager(w.db), cache=ApiKeyAuthCache(valkey=None))
    with pytest.raises(AuthenticationError):
        await auth.authenticate("wa-deadbeefdeadbeef-forgedsecret")
