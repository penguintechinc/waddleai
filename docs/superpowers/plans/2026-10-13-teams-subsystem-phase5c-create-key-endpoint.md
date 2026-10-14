# Teams Subsystem — Phase 5c: `POST /api/v1/keys` Mints a Team-Bound, Proxy-Authenticable Key

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §7 ("Key-create path fix"), §10 ("Key-create gains a `team_id` selection validated against the caller's authz"), §11 ("Key path").
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 5b (`mint_proxy_key`), Phase 4c fixtures (`mint_token`, `real_db_client`).

---

<!-- TASK 19 -->
## Task 19: Rewire `create_key` onto `resolve_key_team` + `mint_proxy_key`

**Files:**
- Modify: `services/management/app/api/v1/keys.py` (`create_key`, `CreateKeyRequest`, `CreateKeyResponse`)
- Modify: `tests/unit/management/conftest.py` (extend the autouse mock-DB guard)
- Test: `tests/unit/management/test_create_key_teams.py` (create)

**Interfaces:**
- Consumes: `resolve_key_team`, `KeyTeamError`, `mint_proxy_key`, fixtures `real_db_client`, `mint_token`, `team_world`.
- Produces: `CreateKeyRequest.team_id: int | None = None`; `CreateKeyResponse.team_id: int | None = None` (the key's resolved acting team); the endpoint now returns the **`wa-{key_id}-{secret}`** credential (`key_prefix` = its first 11 chars + `...`, unchanged shape).
- Behaviour decisions, each tested: (1) team resolution runs **before** the target-user lookup, so an unauthorized caller gets the same 403 whether or not the target id exists (no user-id oracle); (2) a plain user may issue a key for a teammate only by naming the team explicitly in their own tenant -- `resolve_key_team` then demands a live `team:key_admin` (spec §5: team Admin = key issuance), while the old "only admin/resource_manager may create for others" gate still applies to every other case; (3) `owner_role` for the access level comes from the target user's role.
- The legacy mock-DB route tests keep passing because the autouse fixture now also stubs `keys.resolve_key_team` to `None` for tests that do not request `team_world`.
- Regression (`# regression: gh-303`): a key created through the HTTP endpoint authenticates at the proxy's real `RBACManager`, carrying its team.

- [ ] **Step 1: Write the failing test and extend the guard**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -833,6 +833,9 @@
         "services.management.app.api.v1.auth._acting_team",
         AsyncMock(return_value=(None, None)),
     )
+    monkeypatch.setattr(
+        "services.management.app.api.v1.keys.resolve_key_team", lambda *a, **kw: None
+    )


 #: Route modules whose module-level ``db`` the real-database fixture below repoints.
```

```python
"""POST /api/v1/keys mints a proxy-authenticable, team-bound key (issue #303; teams spec §7).

HTTP-level against a real database (``team_world`` + ``real_db_client``): the response's
credential is then fed to the proxy's REAL ``RBACManager.authenticate_api_key``.
"""

import pytest

from shared.auth.rbac import RBACManager


def _hdr(mint_token, w, user: str, role: str = "user", org: str = "a", team: str | None = None):
    """Authorization header for ``user`` (token carries their team context when ``team`` set)."""
    team_role = {
        "olive": "owner", "adam": "admin", "mia": "member", "vic": "viewer", "nat": "member",
    }.get(user)  # fmt: skip
    token = mint_token(
        role=role, user_id=w.u[user], org_id=w.org[org], team_id=w.t[team] if team else None,
        team_role=team_role if team else None,
    )  # fmt: skip
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def test_gh303_created_key_authenticates_at_the_proxy_with_the_default_team(
    team_world, real_db_client, mint_token
) -> None:
    """# regression: gh-303 -- a key from the management endpoint works at the proxy, in a1."""
    resp = await real_db_client.post(
        "/api/v1/keys", json={"name": "ci"}, headers=_hdr(mint_token, team_world, "mia", team="a1")
    )
    assert resp.status_code == 201
    body = await resp.get_json()
    assert body["team_id"] == team_world.t["a1"]
    assert body["api_key"].startswith("wa-")
    assert body["key_prefix"] == body["api_key"][:11] + "..."

    ctx = RBACManager(team_world.db).authenticate_api_key(body["api_key"])
    assert ctx.user_id == team_world.u["mia"]
    assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")


async def test_teamless_user_gets_a_teamless_but_working_key(
    team_world, real_db_client, mint_token
) -> None:
    """Loner has no team: the key is created teamless and still authenticates."""
    resp = await real_db_client.post(
        "/api/v1/keys", json={"name": "ci"}, headers=_hdr(mint_token, team_world, "loner")
    )
    assert resp.status_code == 201
    body = await resp.get_json()
    assert body["team_id"] is None
    assert RBACManager(team_world.db).authenticate_api_key(body["api_key"]).team_id is None


@pytest.mark.parametrize(
    ("caller", "caller_team", "target", "team", "status"),
    [
        ("mia", "a1", None, "a1", 201),     # member, own key, own team
        ("vic", "a1", None, "a1", 403),     # viewer: no team:key_create
        ("nat", "a2", None, "a1", 403),     # cross-team: member of a2 asking for a1
        ("mia", "a1", "vic", "a1", 403),    # member issuing for a teammate: needs key_admin
        ("adam", "a1", "vic", "a1", 201),   # team admin issuing for a teammate
        ("adam", "a1", "nat", "a1", 400),   # target not a member of the named team
        ("mia", "a1", None, "b1", 404),     # another tenant's team: no existence leak
    ],
)  # fmt: skip
async def test_explicit_team_authorization_matrix(
    team_world, real_db_client, mint_token, caller, caller_team, target, team, status
) -> None:
    """Who may bind a key to which team (spec §5 role bundles, enforced live)."""
    w = team_world
    payload = {"name": "k", "team_id": w.t[team]}
    if target:
        payload["user_id"] = w.u[target]
        payload["organization_id"] = w.org["a"]
    resp = await real_db_client.post(
        "/api/v1/keys", json=payload, headers=_hdr(mint_token, w, caller, team=caller_team)
    )
    assert resp.status_code == status, await resp.get_data()


async def test_org_admin_may_bind_any_members_key(team_world, real_db_client, mint_token) -> None:
    """Boss (org admin) issues a key for mia into a1 without being a member."""
    w = team_world
    resp = await real_db_client.post(
        "/api/v1/keys",
        json={"name": "k", "user_id": w.u["mia"], "organization_id": w.org["a"],
              "team_id": w.t["a1"]},
        headers=_hdr(mint_token, w, "boss", role="admin"),
    )  # fmt: skip
    assert resp.status_code == 201
    assert (await resp.get_json())["team_id"] == w.t["a1"]


async def test_unauthorized_caller_cannot_probe_user_ids(
    team_world, real_db_client, mint_token
) -> None:
    """A plain member naming a team gets 403 for a real AND a nonexistent target (no oracle)."""
    w = team_world
    codes = set()
    for target in (w.u["carl"], 99999):
        resp = await real_db_client.post(
            "/api/v1/keys",
            json={"name": "k", "user_id": target, "organization_id": w.org["a"],
                  "team_id": w.t["a1"]},
            headers=_hdr(mint_token, w, "mia", team="a1"),
        )  # fmt: skip
        codes.add(resp.status_code)
    assert codes == {403}


async def test_kill_switch_rejects_an_explicit_team_and_mints_teamless(
    team_world, real_db_client, mint_token, monkeypatch
) -> None:
    """``waddleai.disable-teams`` ON: naming a team is a 404; default keys carry no team."""
    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    hdr = _hdr(mint_token, team_world, "mia")
    bad = await real_db_client.post(
        "/api/v1/keys", json={"name": "k", "team_id": team_world.t["a1"]}, headers=hdr
    )
    ok = await real_db_client.post("/api/v1/keys", json={"name": "k"}, headers=hdr)
    assert bad.status_code == 404
    assert ok.status_code == 201
    assert (await ok.get_json())["team_id"] is None
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_create_key_teams.py --no-cov -q -x`
Expected: FAIL -- the response has no `team_id` (`KeyError: 'team_id'`), and the proxy rejects the old `wa-{secret}` credential (`AuthenticationError: Invalid API key format`).

- [ ] **Step 3: Implement**

```diff
--- a/services/management/app/api/v1/keys.py
+++ b/services/management/app/api/v1/keys.py
@@ -15,6 +15,11 @@
 from shared.auth.rbac import Permission

 from ...extensions import db
+from ...services.key_minting import (
+    KeyTeamError,
+    mint_proxy_key,
+    resolve_key_team,
+)
 from . import api_v1_bp
 from ._pagination import PageRequest
 from .auth import require_auth
@@ -155,6 +160,8 @@
     budget_limit_monthly: float | None = None
     tpm_limit: int | None = 10000
     rpm_limit: int | None = 60
+    #: Acting team for the key (teams spec D3). Omitted = the key owner's own team, if any.
+    team_id: int | None = None


 @dataclass(slots=True)
@@ -254,6 +261,7 @@
     key_prefix: str
     expires_at: str | None
     message: str
+    team_id: int | None = None


 @dataclass(slots=True)
@@ -441,20 +449,40 @@
     target_user_id = data.user_id if data.user_id is not None else user_id
     target_org_id = data.organization_id if data.organization_id is not None else org_id

-    # Permission check
+    # Permission check. A plain user may also issue a key for a teammate, but only by naming the
+    # team explicitly in their own tenant -- ``resolve_key_team`` below then demands a live
+    # ``team:key_admin`` membership in that team (teams spec §5: Admin = team key issuance).
     if target_user_id != user_id or target_org_id != org_id:
-        if user_role not in ["admin", "resource_manager"]:
+        team_issuance = data.team_id is not None and target_org_id == org_id
+        if user_role not in ["admin", "resource_manager"] and not team_issuance:
             return jsonify({"error": "Cannot create keys for other users"}), 403
         if user_role == "resource_manager" and target_org_id != org_id:
             return jsonify({"error": "Cannot create keys for other organizations"}), 403
+
+    # Acting team the key will act in (teams spec D3): validated against what THIS caller may
+    # issue, never trusted from the body. Resolved BEFORE the target-user lookup below so an
+    # unauthorized caller gets the same 403 whether or not the target exists (no id probing).
+    try:
+        team_id = await asyncio.to_thread(
+            resolve_key_team,
+            _db(),
+            org_id=target_org_id,
+            target_user_id=target_user_id,
+            requested_team_id=data.team_id,
+            caller_user_id=user_id,
+            caller_scopes=set(g.user.get("scope") or []),
+        )
+    except KeyTeamError as exc:
+        return jsonify({"error": exc.message}), exc.status

     # Vuln A fix: when creating a key for ANOTHER user, validate the target
     # exists, belongs to target_org_id, and has a role no higher than the
     # caller's. Creating a key for oneself needs no such lookup (the caller is
     # authenticated and clearly exists).
+    target_row: Any = None
     if target_user_id != user_id:

-        def _validate_target_user() -> tuple[object, int]:
+        def _validate_target_user() -> tuple[Any, int]:
             """Fetch target user and validate org membership + role hierarchy."""
             database = _db()
             target = database(database.users.id == target_user_id).select().first()
@@ -467,49 +495,44 @@
                 return None, 403
             return target, 200

-        _, status_code = await asyncio.to_thread(_validate_target_user)
+        target_row, status_code = await asyncio.to_thread(_validate_target_user)
         if status_code == 404:
             return jsonify({"error": "Target user not found"}), 404
         if status_code != 200:
             return jsonify({"error": "Cannot create key for target user"}), 403

-    # Generate API key
-    key_secret = secrets.token_urlsafe(32)
-    api_key = f"wa-{key_secret}"
-    key_prefix = f"wa-{key_secret[:8]}..."
+    owner_role = user_role if target_user_id == user_id else target_row.role

     # Default expiration (1 year)
     expires_days = data.expires_days
     expires_at = datetime.utcnow() + timedelta(days=expires_days) if expires_days else None

-    def _insert():
-        new_key_id = db.virtual_keys.insert(
-            user_id=target_user_id,
-            organization_id=target_org_id,
-            name=data.name,
-            key_prefix=key_prefix,
-            key_hash=bcrypt.hash(api_key),
-            allowed_models=data.allowed_models,
-            allowed_providers=data.allowed_providers,
-            budget_limit_daily=data.budget_limit_daily,
-            budget_limit_monthly=data.budget_limit_monthly,
-            tpm_limit=data.tpm_limit,
-            rpm_limit=data.rpm_limit,
-            enabled=True,
-            expires_at=expires_at,
-            created_at=datetime.utcnow(),
-        )
-        db.commit()
-        return new_key_id
-
-    key_id = await asyncio.to_thread(_insert)
+    # Mint ``wa-{key_id}-{secret}`` into api_keys (what the proxy authenticates, carrying the
+    # team) AND virtual_keys (limits/metadata), linked -- closes #303.
+    minted = await asyncio.to_thread(
+        mint_proxy_key,
+        _db(),
+        user_id=target_user_id,
+        org_id=target_org_id,
+        name=data.name,
+        owner_role=owner_role,
+        team_id=team_id,
+        expires_at=expires_at,
+        allowed_models=data.allowed_models,
+        allowed_providers=data.allowed_providers,
+        budget_limit_daily=data.budget_limit_daily,
+        budget_limit_monthly=data.budget_limit_monthly,
+        tpm_limit=data.tpm_limit,
+        rpm_limit=data.rpm_limit,
+    )

     return {
-        "id": key_id,
+        "id": minted.virtual_key_id,
         "name": data.name,
-        "api_key": api_key,
-        "key_prefix": key_prefix,
+        "api_key": minted.api_key,
+        "key_prefix": minted.key_prefix,
         "expires_at": expires_at.isoformat() if expires_at else None,
+        "team_id": team_id,
         "message": "Key created successfully. Save the api_key - it will not be shown again.",
     }, 201
```

- [ ] **Step 4: Run it and the existing key route suites**

Run: `.venv/bin/python -m pytest tests/unit/management/test_create_key_teams.py tests/unit/management/test_key_routes.py tests/unit/management/test_security_review_2026_07_26.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/keys.py tests/unit/management/conftest.py tests/unit/management/test_create_key_teams.py
git commit -m "fix(keys): POST /keys mints a team-bound proxy credential (closes gh-303)"
```

- [ ] **Phase 5c gate:** Step 4's command passes.
