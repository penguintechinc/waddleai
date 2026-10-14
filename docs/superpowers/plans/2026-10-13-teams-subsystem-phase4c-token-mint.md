# Teams Subsystem — Phase 4c: Management Token Mint and Decode Carry the Team

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §6 ("Management `_decode_token` ... extend it"), §3 D6/D7, §11 (Auth).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 4a-4b (`lookup_user_team`, the `ext` converters).

---

<!-- TASK 13 -->
## Task 13: Login/refresh/exchange mint the live team; `g.user` carries it

**Files:**
- Modify: `services/management/app/api/v1/auth.py`
- Modify: `tests/unit/management/conftest.py` (`make_token` gains team args; `mint_token`, `real_db_client` fixtures; an autouse guard for the legacy mock-DB route tests)
- Test: `tests/unit/management/test_auth_team_claims.py` (create)

**Interfaces:**
- Consumes: `lookup_user_team`, `expand_team_permissions`, fixtures `team_world`.
- Produces: `issue_access_token(user_id, username, role, organization_id, expires_hours=None, team_id=None, team_role=None)` (team scopes unioned in, pair in `ext`); `async _acting_team(user_id, organization_id) -> (team_id, team_role)` (off-loop live lookup, called at all four mint sites: login, token exchange, refresh, change-password) so **a refresh re-resolves membership instead of carrying a stale team forward**; `_decode_token` adds `team_id`/`team_role` to the payload that becomes `g.user`.
- Produces test fixtures: `make_token(..., team_id=None, team_role=None)` (conftest), `mint_token` (factory fixture -- tokens must be signed by the same cached OIDC keypair `flask_app` patches in, so tests take the fixture rather than importing `make_token`), `real_db_client` (Quart test client whose route modules -- `auth`, `users`, `organizations`, `keys`, `proxy_keys` -- talk to `team_world`'s REAL database; Phase 6 adds `teams` to its list).
- The legacy route tests wire one canned row to every `db(...).select().first()`, so the new live lookup would read that row as a `team_members` row. The autouse fixture `_teamless_tokens_for_mock_db_tests` stubs `auth._acting_team` to `(None, None)` for every test that does NOT request `team_world`.

- [ ] **Step 1: Write the failing test and the fixtures**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -10,13 +10,13 @@
 from datetime import datetime, timedelta
 from functools import lru_cache
 from typing import Any, cast
-from unittest.mock import MagicMock, patch
+from unittest.mock import AsyncMock, MagicMock, patch

 import jwt as _jwt
 import pytest

 from shared.auth.penguin_auth import create_oidc_provider, issue_token
-from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext
+from shared.auth.rbac import ROLE_PERMISSIONS, Role, UserContext, expand_team_permissions

 # Ensure management service is importable
 sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../services/management"))
@@ -442,8 +442,14 @@
     username: str = "testuser",
     secret: str = TEST_ONLY_JWT_SECRET,  # unused, kept for call-site compat
     expires_hours: int = 1,
+    team_id: int | None = None,
+    team_role: str | None = None,
 ) -> str:
-    """Encode a JWT via penguin-aaa (RS256), or a raw expired token when expires_hours<=0."""
+    """Encode a JWT via penguin-aaa (RS256), or a raw expired token when expires_hours<=0.
+
+    ``team_id``/``team_role`` mint the teams-subsystem acting-team context into ``ext`` and
+    union the team role's scopes into the token, exactly as a real login would.
+    """
     from datetime import UTC

     provider = _test_oidc_provider()
@@ -470,7 +476,7 @@
     # UserContext.permissions is `set[Permission]`; downstream
     # user_context_to_claims() extracts `.value` from each member itself, so
     # pass the enums through unconverted rather than pre-stringifying here.
-    permissions = ROLE_PERMISSIONS.get(role_enum, set())
+    permissions = set(ROLE_PERMISSIONS.get(role_enum, set())) | expand_team_permissions(team_role)
     user_context = UserContext(
         user_id=user_id,
         username=username,
@@ -478,8 +484,20 @@
         organization_id=org_id,
         managed_orgs=[],
         permissions=permissions,
+        team_id=team_id,
+        team_role=team_role,
     )
     return issue_token(user_context, provider)
+
+
+@pytest.fixture
+def mint_token():
+    """Factory fixture: ``mint_token(role=, user_id=, org_id=, team_id=, team_role=)``.
+
+    A fixture (not an import of ``make_token``) so the token is signed by the same cached OIDC
+    keypair ``flask_app`` patched into the app -- see ``rm_org2_auth_headers`` for why.
+    """
+    return make_token


 @pytest.fixture
@@ -799,3 +817,51 @@
     from shared.auth.rbac import RBACManager

     return RBACManager(mock_db)
+
+
+@pytest.fixture(autouse=True)
+def _teamless_tokens_for_mock_db_tests(request, monkeypatch):
+    """Keep token mints teamless in the legacy MagicMock-DB route tests.
+
+    Those tests wire ONE canned row to every ``db(...).select().first()`` call, so the new
+    live team lookup in ``auth._acting_team`` would read that row as a ``team_members`` row.
+    Tests that opt in to a real database (the ``team_world`` fixture) keep the real lookup.
+    """
+    if "team_world" in request.fixturenames:
+        return
+    monkeypatch.setattr(
+        "services.management.app.api.v1.auth._acting_team",
+        AsyncMock(return_value=(None, None)),
+    )
+
+
+#: Route modules whose module-level ``db`` the real-database fixture below repoints.
+_REAL_DB_ROUTE_MODULES = ("auth", "users", "organizations", "keys", "proxy_keys")
+
+
+@pytest.fixture
+async def real_db_client(flask_app, team_world, monkeypatch) -> AsyncGenerator:
+    """A Quart test client whose route modules talk to ``team_world``'s REAL database.
+
+    ``flask_app`` patches every route module's ``db`` with one shared MagicMock; this repoints
+    the modules the teams tests exercise at the real SQLAlchemy-schema database instead, and
+    resets the process-wide auth singletons (throttle, rate limiter, denylist) so a lockout in
+    one test cannot leak into the next.
+    """
+    import importlib
+
+    from services.management.app.services.login_throttle import reset_login_throttle
+    from services.management.app.services.rate_limiter import reset_auth_rate_limiter
+    from services.management.app.services.token_denylist import reset_token_denylist
+
+    for name in _REAL_DB_ROUTE_MODULES:
+        module = importlib.import_module(f"services.management.app.api.v1.{name}")
+        monkeypatch.setattr(module, "db", team_world.db)
+    reset_login_throttle()
+    reset_auth_rate_limiter()
+    reset_token_denylist()
+    async with flask_app.test_client() as client:
+        yield client
+    reset_login_throttle()
+    reset_auth_rate_limiter()
+    reset_token_denylist()
```

```python
"""Management token mint/decode carry the acting team (teams spec D3/D6/D7).

Real database (``team_world``), real signed tokens: login, refresh and key-exchange each
re-resolve the live team, and ``_decode_token`` hands ``g.user`` the team context.
"""

import jwt as _jwt
from passlib.hash import bcrypt

from services.management.app.api.v1 import auth as auth_mod
from shared.auth.rbac import Permission

P = Permission


def _claims(token: str) -> dict:
    """Decode a token's claims without verifying (the signature is checked by the app)."""
    return _jwt.decode(token, options={"verify_signature": False})


def _login_user(world, name: str, org: str, team_key: str | None, team_role: str | None) -> None:
    """Insert a user with a real bcrypt password (and optional team membership)."""
    db = world.db
    uid = db.users.insert(
        username=name, email=f"{name}@example.com", password_hash=bcrypt.hash("pw-12345-xyz"),
        role="user", enabled=True, organization_id=world.org[org],
    )  # fmt: skip
    if team_key:
        db.team_members.insert(
            team_id=world.t[team_key], user_id=uid, organization_id=world.org[org],
            team_role=team_role,
        )  # fmt: skip
    db.commit()


async def _login(client, name: str) -> dict:
    """POST /auth/login and return the JSON body (asserting success)."""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": name, "password": "pw-12345-xyz"}
    )
    assert resp.status_code == 200, await resp.get_data()
    return await resp.get_json()


def test_issue_access_token_unions_team_scopes_and_sets_ext() -> None:
    """A team role adds its scopes and rides in ``ext``; the org bundle is unchanged."""
    issued = auth_mod.issue_access_token(
        user_id=1, username="u", role="user", organization_id=1, team_id=7, team_role="admin"
    )
    claims = _claims(issued.access_token)
    assert claims["ext"]["team_id"] == "7"
    assert claims["ext"]["team_role"] == "admin"
    assert {P.TEAM_ADMIN.value, P.TEAM_MEMBER_MANAGE.value, P.TEAM_USE.value} <= set(
        claims["scope"]
    )
    assert P.TEAM_DELETE.value not in claims["scope"]


def test_teamless_token_has_no_team_scopes() -> None:
    """No acting team -> no ``team:*`` scope and null ext team fields."""
    claims = _claims(auth_mod.issue_access_token(1, "u", "user", 1).access_token)
    assert claims["ext"]["team_id"] is None
    assert not [s for s in claims["scope"] if s.startswith("team:")]


def test_decode_token_exposes_team_context() -> None:
    """``g.user`` (built from ``_decode_token``) carries team_id/team_role for downstream use."""
    token = auth_mod.issue_access_token(1, "u", "user", 1, team_id=7, team_role="viewer")
    payload = auth_mod._decode_token(token.access_token)
    assert payload is not None
    assert (payload["team_id"], payload["team_role"]) == (7, "viewer")


async def test_acting_team_resolves_live_membership(team_world, real_db_client) -> None:
    """``_acting_team`` reads the DB: mia -> (a1, member); loner -> teamless."""
    org = team_world.org["a"]
    assert await auth_mod._acting_team(team_world.u["mia"], org) == (team_world.t["a1"], "member")
    assert await auth_mod._acting_team(team_world.u["loner"], org) == (None, None)


async def test_login_mints_the_users_team_into_the_token(team_world, real_db_client) -> None:
    """A team member's login token carries ext.team_* and the team role's scopes."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    body = await _login(real_db_client, "tina")
    claims = _claims(body["access_token"])
    assert claims["ext"]["team_id"] == str(team_world.t["a1"])
    assert claims["ext"]["team_role"] == "admin"
    assert P.TEAM_MEMBER_MANAGE.value in claims["scope"]


async def test_refresh_picks_up_a_membership_change(team_world, real_db_client) -> None:
    """Review focus: a refresh after a demotion re-resolves the live role, not the old token's."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    body = await _login(real_db_client, "tina")
    db = team_world.db
    uid = db(db.users.username == "tina").select().first().id
    db(db.team_members.user_id == uid).update(team_role="viewer")
    db.commit()

    resp = await real_db_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert resp.status_code == 200
    claims = _claims((await resp.get_json())["access_token"])
    assert claims["ext"]["team_role"] == "viewer"
    assert P.TEAM_MEMBER_MANAGE.value not in claims["scope"]
    assert P.TEAM_READ.value in claims["scope"]


async def test_kill_switch_login_is_teamless(team_world, real_db_client, monkeypatch) -> None:
    """``waddleai.disable-teams`` ON: login still works and mints no team context."""
    _login_user(team_world, "tina", "a", "a1", "admin")
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    claims = _claims((await _login(real_db_client, "tina"))["access_token"])
    assert claims["ext"]["team_id"] is None
    assert not [s for s in claims["scope"] if s.startswith("team:")]
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_team_claims.py --no-cov -q -x`
Expected: FAIL `TypeError: issue_access_token() got an unexpected keyword argument 'team_id'`.

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/api/v1/auth.py
+++ b/services/management/app/api/v1/auth.py
@@ -18,7 +18,14 @@

 from shared.auth.penguin_auth import create_oidc_provider, issue_token
 from shared.auth.penguin_auth import verify_token as _aaa_verify_token
-from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role, UserContext
+from shared.auth.rbac import (
+    ROLE_PERMISSIONS,
+    Permission,
+    Role,
+    UserContext,
+    expand_team_permissions,
+    lookup_user_team,
+)

 from ...extensions import db
 from ...services.login_throttle import ThrottleDecision, account_key, get_login_throttle
@@ -356,8 +363,13 @@
     role: str,
     organization_id: int,
     expires_hours: int | None = None,
+    team_id: int | None = None,
+    team_role: str | None = None,
 ) -> IssuedToken:
     """Issue an RS256 access token and report the lifetime actually applied.
+
+    ``team_id``/``team_role`` are the caller's resolved acting team (teams spec D3/D6): the
+    team role's scopes are unioned into the token and the pair rides in the ``ext`` claim.

     Returning the real TTL alongside the token is the point: the previous code
     advertised `expires_in: 86400` on every login while penguin-aaa signed a
@@ -373,7 +385,8 @@
         role_enum = Role(role)
     except ValueError:
         role_enum = Role.USER
-    permissions: set[Permission] = ROLE_PERMISSIONS.get(role_enum, set())
+    permissions: set[Permission] = set(ROLE_PERMISSIONS.get(role_enum, set()))
+    permissions |= expand_team_permissions(team_role)
     user_context = UserContext(
         user_id=user_id,
         username=username,
@@ -381,9 +394,20 @@
         organization_id=organization_id,
         managed_orgs=[],
         permissions=permissions,
+        team_id=team_id,
+        team_role=team_role,
     )
     token = issue_token(user_context, _provider_for_ttl(hours))
     return IssuedToken(access_token=token, expires_in=hours * 3600)
+
+
+async def _acting_team(user_id: int, organization_id: int) -> tuple[int | None, str | None]:
+    """Resolve the user's live ``(team_id, team_role)`` off the event loop for a token mint.
+
+    Re-resolved on every mint (login, key exchange, refresh, password change) so a refresh
+    picks up a membership change instead of carrying a stale team forward.
+    """
+    return await asyncio.to_thread(lookup_user_team, _db(), user_id, organization_id)


 def create_token(
@@ -423,6 +447,8 @@
         "scope": sorted(user_context.permissions),
         "jti": raw.get("jti"),
         "exp": raw.get("exp"),
+        "team_id": user_context.team_id,
+        "team_role": user_context.team_role,
     }


@@ -810,11 +836,14 @@
     await asyncio.to_thread(_update_login)

     # Create token
+    team_id, team_role = await _acting_team(user.id, user.organization_id)
     issued = issue_access_token(
         user_id=user.id,
         username=user.username,
         role=user.role,
         organization_id=user.organization_id,
+        team_id=team_id,
+        team_role=team_role,
     )

     # Additionally hand the browser an HttpOnly cookie carrying the same token
@@ -890,11 +919,14 @@
         logger.info("auth: token exchange refused for an invalid or disabled API key")
         return jsonify({"error": "Invalid or expired token"}), 401

+    team_id, team_role = await _acting_team(user_ctx["user_id"], user_ctx["organization_id"])
     issued = issue_access_token(
         user_id=user_ctx["user_id"],
         username=user_ctx["username"],
         role=user_ctx["role"],
         organization_id=user_ctx["organization_id"],
+        team_id=team_id,
+        team_role=team_role,
     )

     return {
@@ -965,11 +997,14 @@

     await _revoke_presented_token()

+    team_id, team_role = await _acting_team(user["user_id"], user["organization_id"])
     issued = issue_access_token(
         user_id=user["user_id"],
         username=user["username"],
         role=user["role"],
         organization_id=user["organization_id"],
+        team_id=team_id,
+        team_role=team_role,
     )

     # Browser clients authenticate from the HttpOnly cookie, which still holds
@@ -1073,11 +1108,14 @@
     # authenticated with the now-revoked bearer token re-authenticate with the
     # new password, which is the expected post-change behaviour.
     await _revoke_presented_token()
+    team_id, team_role = await _acting_team(user.id, user.organization_id)
     reissued = issue_access_token(
         user_id=user.id,
         username=user.username,
         role=user.role,
         organization_id=user.organization_id,
+        team_id=team_id,
+        team_role=team_role,
     )
     _set_access_cookie(reissued.access_token, reissued.expires_in)
```

- [ ] **Step 4: Run it and every existing auth route suite**

Run: `.venv/bin/python -m pytest tests/unit/management/test_auth_team_claims.py tests/unit/management/test_auth_routes.py tests/unit/management/test_auth_cookie.py --no-cov -q`
Expected: all pass (`7 passed` in the new file; the legacy suites still pass because of the autouse stub).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/auth.py tests/unit/management/conftest.py tests/unit/management/test_auth_team_claims.py
git commit -m "feat(teams): management tokens carry the live acting team in ext and scopes"
```

- [ ] **Phase 4c gate:** Step 4 passes.
