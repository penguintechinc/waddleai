# Teams Subsystem — Phase 5d: Revoke/Rotate Parity and the Legacy-Key Guard

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §8 (issue #303 "carries its regression test"), §12 (cache coherence).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 5c (management keys now authenticate at the proxy).

Before this work a management key never authenticated at the proxy, so revoking it was moot. Now it does: a revoked/disabled/rotated virtual key that stayed usable at the proxy would be a **security regression**. Task 20 closes that; Task 21 pins the other half of the Review Focus -- a legacy key still fails cleanly.

---

<!-- TASK 20 -->
## Task 20: Revoke, disable and rotate reach the proxy credential and its cache

**Files:**
- Modify: `services/management/app/api/v1/keys.py` (`update_key`, `delete_key`, `rotate_key`)
- Modify: `tests/unit/management/conftest.py` and `tests/unit/management/route_conftest.py` (`make_mock_key` rows are legacy unlinked: `api_key_id = None`)
- Test: `tests/unit/management/test_key_revoke_rotate_parity.py` (create)

**Interfaces:**
- Consumes: `sync_linked_proxy_key`, `mint_rotated_credential`, `invalidate_key_ids`, `_db()` (inside the thread closures use `database = _db()`, not the module-level `db`, which mypy types as `DB | None`).
- Produces: `DELETE /keys/<id>` disables the linked `api_keys` row; `PUT /keys/<id>` with `enabled` mirrors it (other field edits touch nothing on the proxy side); `POST /keys/<id>/rotate` keeps the `key_id` and prefix, swaps the secret on BOTH rows, and returns the new `wa-{key_id}-{secret}`. Each then invalidates that one proxy cache entry. A legacy unlinked key (`api_key_id` is `NULL`) revokes/rotates exactly as before with **no cache I/O**.
- Scope note (index "Open items"): rotating a legacy key does not upgrade it to a proxy credential; create a new key.

- [ ] **Step 1: Write the failing test and update the mock rows**

```diff
--- a/tests/unit/management/route_conftest.py
+++ b/tests/unit/management/route_conftest.py
@@ -320,6 +320,7 @@
     key.expires_at = None
     key.last_used = None
     key.created_at = datetime(2025, 1, 1, 12, 0, 0)
+    key.api_key_id = None  # legacy virtual key: no linked proxy credential to sync
     return key
```

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -662,6 +662,7 @@
     key.expires_at = None
     key.last_used = None
     key.created_at = datetime(2025, 1, 1, 12, 0, 0)
+    key.api_key_id = None  # legacy virtual key: no linked proxy credential to sync
     return key
```

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_revoke_rotate_parity.py --no-cov -q -x`
Expected: FAIL `Failed: DID NOT RAISE <class 'shared.auth.rbac.AuthenticationError'>` -- the revoked key still authenticates at the proxy.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/api/v1/keys.py
+++ b/services/management/app/api/v1/keys.py
@@ -15,10 +15,13 @@
 from shared.auth.rbac import Permission

 from ...extensions import db
+from ...services.auth_cache_invalidation import invalidate_key_ids
 from ...services.key_minting import (
     KeyTeamError,
     mint_proxy_key,
+    mint_rotated_credential,
     resolve_key_team,
+    sync_linked_proxy_key,
 )
 from . import api_v1_bp
 from ._pagination import PageRequest
@@ -611,11 +614,18 @@

     if update_fields:

-        def _update():
-            db(db.virtual_keys.id == key_id).update(**update_fields)
-            db.commit()
-
-        await asyncio.to_thread(_update)
+        def _update() -> str | None:
+            database = _db()
+            database(database.virtual_keys.id == key_id).update(**update_fields)
+            database.commit()
+            # Disable/enable must reach the credential the proxy authenticates (issue #303).
+            if "enabled" in update_fields:
+                return sync_linked_proxy_key(database, key, enabled=update_fields["enabled"])
+            return None
+
+        linked_key_id = await asyncio.to_thread(_update)
+        if linked_key_id:
+            await invalidate_key_ids([linked_key_id])

     return {"message": "Key updated successfully."}

@@ -651,11 +661,16 @@
             return jsonify({"error": "Access denied"}), 403

     # Soft delete by disabling
-    def _disable():
-        db(db.virtual_keys.id == key_id).update(enabled=False)
-        db.commit()
-
-    await asyncio.to_thread(_disable)
+    def _disable() -> str | None:
+        database = _db()
+        database(database.virtual_keys.id == key_id).update(enabled=False)
+        database.commit()
+        # A revoked key must stop authenticating at the proxy too, not just in management.
+        return sync_linked_proxy_key(database, key, enabled=False)
+
+    linked_key_id = await asyncio.to_thread(_disable)
+    if linked_key_id:
+        await invalidate_key_ids([linked_key_id])

     return {"message": "Key revoked successfully"}

@@ -684,18 +699,28 @@
         elif user_role not in ["resource_manager"] and key.user_id != user_id:
             return jsonify({"error": "Access denied"}), 403

-    # Generate new key
-    key_secret = secrets.token_urlsafe(32)
-    new_api_key = f"wa-{key_secret}"
-    key_prefix = f"wa-{key_secret[:8]}..."
-
-    def _rotate():
-        db(db.virtual_keys.id == key_id).update(
-            key_hash=bcrypt.hash(new_api_key), key_prefix=key_prefix
+    # A key linked to a proxy credential keeps its key_id and gets a fresh secret in the
+    # ``wa-{key_id}-{secret}`` format; a legacy unlinked key keeps its old ``wa-{secret}`` shape.
+    rotated = await asyncio.to_thread(mint_rotated_credential, _db(), key)
+    if rotated is not None:
+        new_api_key, key_prefix = rotated
+    else:
+        key_secret = secrets.token_urlsafe(32)
+        new_api_key = f"wa-{key_secret}"
+        key_prefix = f"wa-{key_secret[:8]}..."
+
+    def _rotate() -> str | None:
+        database = _db()
+        new_hash = bcrypt.hash(new_api_key)
+        database(database.virtual_keys.id == key_id).update(
+            key_hash=new_hash, key_prefix=key_prefix
         )
-        db.commit()
-
-    await asyncio.to_thread(_rotate)
+        database.commit()
+        return sync_linked_proxy_key(database, key, key_hash=new_hash)
+
+    linked_key_id = await asyncio.to_thread(_rotate)
+    if linked_key_id:
+        await invalidate_key_ids([linked_key_id])

     return {
         "id": key_id,
```

- [ ] **Step 4: Run it and the existing key route suites**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_revoke_rotate_parity.py tests/unit/management/test_key_routes.py tests/unit/management/test_security_review_2026_07_26.py tests/unit/management/test_cache_config_api.py tests/unit/management/test_usage_routes.py --no-cov -q`
Expected: all pass. (`test_user_can_still_disable_own_key` asserts `update` is called once; it passes only because `make_mock_key` rows now carry `api_key_id = None`.)

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/keys.py tests/unit/management/conftest.py tests/unit/management/route_conftest.py tests/unit/management/test_key_revoke_rotate_parity.py
git commit -m "fix(keys): revoke, disable and rotate reach the proxy credential and its cache"
```

---

<!-- TASK 21 -->
## Task 21: A legacy `wa-{secret}` key at the proxy is a clean 401

**Files:**
- Test: `tests/unit/proxy/test_legacy_key_at_proxy.py` (create) -- a regression guard; it passes on creation because the proxy already rejects these shapes, and fails if anyone makes the parser lenient or the lookup crash.

**Interfaces:**
- Consumes: `ApiKeyAuthenticator`, `ApiKeyAuthCache`, `RBACManager`, `AuthenticationError`.
- Pins (Review Focus: "a `wa-{secret}` legacy `virtual_keys` key hitting the proxy"): every legacy shape -- a secret with no `-` (parser raises `Invalid API key format`), a secret with `-` (the first segment becomes a bogus `key_id` -> miss), `wa-`, `wa--x-y`, `sk-abc` -- raises `AuthenticationError` (the OIDC middleware turns it into a 401), never a 500 and never a team context; and a credential reusing a REAL `key_id` with a forged secret still fails the bcrypt check, so it can never borrow that key's team.

- [ ] **Step 1: Write the test**

```python
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
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_legacy_key_at_proxy.py --no-cov -q`
Expected: `6 passed`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/proxy/test_legacy_key_at_proxy.py
git commit -m "test(keys): a legacy wa-{secret} key at the proxy fails with AuthenticationError"
```

- [ ] **Phase 5 gate:** `.venv/bin/python -m pytest tests/unit/management/test_key_minting.py tests/unit/management/test_create_key_teams.py tests/unit/management/test_key_revoke_rotate_parity.py tests/unit/proxy/test_legacy_key_at_proxy.py --no-cov -q` -- all pass.
