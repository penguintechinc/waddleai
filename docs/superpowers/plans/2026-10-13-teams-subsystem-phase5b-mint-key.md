# Teams Subsystem — Phase 5b: Minting a Key the Proxy Can Authenticate (Closes #303 at the Service Layer)

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §7, §8 (issue #303), D10, §11 ("Key path").
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 5a (`mint_wa_credential`, `resolve_key_team`, the `virtual_keys.api_key_id` column from Phase 1).

---

<!-- TASK 18 -->
## Task 18: `mint_proxy_key` -- one credential, two linked rows

**Files:**
- Modify: `services/management/app/services/key_minting.py`
- Test: `tests/unit/management/test_key_minting.py` (create)

**Interfaces:**
- Consumes: `mint_wa_credential`, `access_level_for_role`, `virtual_keys.api_key_id`.
- Produces: `MintedKey(virtual_key_id, api_key_row_id, key_id, api_key, key_prefix)`; `virtual_key_prefix(api_key) -> str` (`api_key[:11] + "..."`, pinned equal to `auth._virtual_key_prefix` by a test); `mint_proxy_key(database, *, user_id, org_id, name, owner_role, team_id, expires_at, allowed_models, allowed_providers, budget_limit_daily, budget_limit_monthly, tpm_limit, rpm_limit) -> MintedKey`; `mint_rotated_credential(database, virtual_key) -> (api_key, key_prefix) | None` (keeps the `key_id`, new secret; `None` for a legacy unlinked key); `sync_linked_proxy_key(database, virtual_key, *, enabled=None, key_hash=None) -> key_id | None` (mirrors a change onto the linked `api_keys` row and returns the `key_id` whose proxy cache entry the caller must invalidate).
- Design (spec deviation, flagged in the index): the spec says mint into `api_keys`; but the limits/metadata (`allowed_models`, budgets, `tpm_limit`...) and every other management key endpoint live on `virtual_keys`. So ONE plaintext is hashed once and written to BOTH rows -- exactly what the bootstrap admin key already does (`extensions.py`) -- linked by the new `virtual_keys.api_key_id`. Without the link a "revoked" management key would stay usable at the proxy (a security regression, because management keys now authenticate there).
- No cross-table transaction exists in penguin-dal, so if the `virtual_keys` insert fails the `api_keys` row is deleted before the error propagates -- never a half-minted key. `enabled` and `created_at` are written explicitly (a `create_all` schema has no server defaults).
- Regression (`# regression: gh-303`): the minted credential authenticates through the proxy's real `RBACManager.authenticate_api_key` (team intact) AND management's real `verify_api_key`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_minting.py --no-cov -q -x`
Expected: collection error `ImportError: cannot import name 'mint_proxy_key' from 'services.management.app.services.key_minting'`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/services/key_minting.py
+++ b/services/management/app/services/key_minting.py
@@ -6,18 +6,45 @@
 """

 import logging
+import secrets
 from collections.abc import Collection
-
+from dataclasses import dataclass
+from datetime import datetime
+from typing import Any
+
+from passlib.hash import bcrypt
 from penguin_dal.db import DB

 from shared.auth.rbac import (
     Permission,
+    access_level_for_role,
     expand_team_permissions,
     lookup_user_team,
+    mint_wa_credential,
 )
 from shared.auth.team_flags import teams_enabled

 logger = logging.getLogger(__name__)
+
+#: A management-verified virtual key is matched by its first 11 characters (``wa-`` + 8) plus
+#: ``...`` -- must stay equal to ``api.v1.auth._virtual_key_prefix`` (a test pins the pair).
+_VIRTUAL_KEY_PREFIX_LEN = 11
+
+
+@dataclass(slots=True)
+class MintedKey:
+    """A freshly minted proxy key: both row ids, the non-secret key_id, and the one-time secret."""
+
+    virtual_key_id: int
+    api_key_row_id: int
+    key_id: str
+    api_key: str
+    key_prefix: str
+
+
+def virtual_key_prefix(api_key: str) -> str:
+    """Return the ``virtual_keys.key_prefix`` a presented ``api_key`` must match."""
+    return f"{api_key[:_VIRTUAL_KEY_PREFIX_LEN]}..."


 class KeyTeamError(Exception):
@@ -92,3 +119,120 @@
     if _membership(database, requested_team_id, target_user_id) is None:
         raise KeyTeamError("Key owner is not a member of that team", 400)
     return int(requested_team_id)
+
+
+def mint_proxy_key(
+    database: DB,
+    *,
+    user_id: int,
+    org_id: int,
+    name: str,
+    owner_role: str,
+    team_id: int | None,
+    expires_at: datetime | None,
+    allowed_models: list[str] | None,
+    allowed_providers: list[str] | None,
+    budget_limit_daily: float | None,
+    budget_limit_monthly: float | None,
+    tpm_limit: int | None,
+    rpm_limit: int | None,
+) -> MintedKey:
+    """Mint one ``wa-{key_id}-{secret}`` credential into BOTH key tables (issue #303).
+
+    ``api_keys`` is what the proxy authenticates (carries ``team_id``, the acting team);
+    ``virtual_keys`` keeps the limits/metadata every other management endpoint reads, and links
+    back via ``api_key_id`` so revoke/rotate can reach the proxy credential. The same
+    plaintext is hashed once and stored in both rows -- mirroring the bootstrap admin key.
+    ``enabled`` and ``created_at`` are written explicitly: a SQLAlchemy-built schema has no
+    server-side defaults and auth filters on ``enabled == True``.
+
+    There is no cross-table transaction in penguin-dal, so if the second insert fails the first
+    row is deleted before the error propagates -- never a half-minted key.
+    """
+    key_id, api_key = mint_wa_credential()
+    key_hash = bcrypt.hash(api_key)
+    now = datetime.utcnow()
+
+    api_row_id = database.api_keys.insert(
+        key_id=key_id,
+        key_hash=key_hash,
+        user_id=user_id,
+        organization_id=org_id,
+        name=name,
+        rate_limit_rpm=rpm_limit,
+        enabled=True,
+        expires_at=expires_at,
+        created_at=now,
+        api_access_level=access_level_for_role(owner_role),
+        team_id=team_id,
+    )
+    prefix = virtual_key_prefix(api_key)
+    try:
+        virtual_row_id = database.virtual_keys.insert(
+            user_id=user_id,
+            organization_id=org_id,
+            name=name,
+            key_prefix=prefix,
+            key_hash=key_hash,
+            allowed_models=allowed_models,
+            allowed_providers=allowed_providers,
+            budget_limit_daily=budget_limit_daily,
+            budget_limit_monthly=budget_limit_monthly,
+            tpm_limit=tpm_limit,
+            rpm_limit=rpm_limit,
+            enabled=True,
+            expires_at=expires_at,
+            created_at=now,
+            api_key_id=api_row_id,
+        )
+    except Exception:
+        database(database.api_keys.id == api_row_id).delete()
+        database.commit()
+        raise
+    database.commit()
+    return MintedKey(
+        virtual_key_id=int(virtual_row_id),
+        api_key_row_id=int(api_row_id),
+        key_id=key_id,
+        api_key=api_key,
+        key_prefix=prefix,
+    )
+
+
+def mint_rotated_credential(database: DB, virtual_key: Any) -> tuple[str, str] | None:
+    """New ``(api_key, key_prefix)`` for a LINKED virtual key, keeping its ``key_id``; else None.
+
+    Rotation swaps only the secret half, so the proxy's ``key_id`` lookup and the virtual key's
+    prefix stay stable. ``None`` means a legacy unlinked key: the caller keeps its old format.
+    """
+    if virtual_key.api_key_id is None:
+        return None
+    row = database(database.api_keys.id == virtual_key.api_key_id).select().first()
+    if row is None:
+        return None
+    api_key = f"wa-{row.key_id}-{secrets.token_urlsafe(32)}"
+    return api_key, virtual_key_prefix(api_key)
+
+
+def sync_linked_proxy_key(
+    database: DB, virtual_key: Any, *, enabled: bool | None = None, key_hash: str | None = None
+) -> str | None:
+    """Mirror an ``enabled`` / ``key_hash`` change onto the linked ``api_keys`` row.
+
+    Returns the proxy ``key_id`` whose auth-cache entry the caller must now invalidate, or
+    ``None`` when the virtual key has no linked proxy credential (legacy) -- nothing to sync.
+    """
+    if virtual_key.api_key_id is None:
+        return None
+    row = database(database.api_keys.id == virtual_key.api_key_id).select().first()
+    if row is None:
+        return None
+    fields: dict[str, Any] = {}
+    if enabled is not None:
+        fields["enabled"] = enabled
+    if key_hash is not None:
+        fields["key_hash"] = key_hash
+    if fields:
+        database(database.api_keys.id == row.id).update(**fields)
+        database.commit()
+    return str(row.key_id)
```

- [ ] **Step 4: Run it**

Run: `.venv/bin/python -m pytest tests/unit/management/test_key_minting.py tests/unit/management/test_key_team_resolution.py --no-cov -q`
Expected: all pass (`10 passed` in the new file).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/key_minting.py tests/unit/management/test_key_minting.py
git commit -m "fix(keys): mint wa-{key_id}-{secret} into api_keys and virtual_keys, linked (gh-303)"
```

- [ ] **Phase 5b gate:** Step 4's command passes.
