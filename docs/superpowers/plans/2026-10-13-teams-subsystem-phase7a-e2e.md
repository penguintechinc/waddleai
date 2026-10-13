# Teams Subsystem — Phase 7a: DSAR Manifest, OpenAPI, and Cross-Plane End-to-End Tests

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §10 (OpenAPI/response models), §11 (E2E), §12.
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phases 1-6.

---

<!-- TASK 25 -->
## Task 26: DSAR manifest lists `team_members`; regenerate and lint the OpenAPI spec

**Files:**
- Modify: `services/management/app/dsar.py` (`data_holding_manifest`)
- Modify: `openapi/v1.yaml` (**generated** -- never hand-edit)
- Test: `tests/unit/management/test_dsar_teams.py` (create)

**Interfaces:**
- Produces: a `team_members` entry in the GDPR Art. 15 data-holding manifest (`contains_pii: False` -- the table holds ids and a role only -- `reference: "team_members.user_id -> users.id"`); the manifest is hand-curated, so a new user-referencing table must be added by hand.
- `openapi/v1.yaml` is generated from the `quart-schema` annotations (`make generate-openapi`), and CI runs `spectral lint` at `--fail-severity=error`; the ten new team operations plus the `team_id` field on the key create request/response land in the diff.

- [ ] **Step 1: Write the failing test**

```python
"""DSAR data-holding manifest covers the teams tables (GDPR Art. 15; teams spec §10).

``team_members`` references the subject by id only -- no PII column -- but a data-subject export
must still disclose that the membership exists, so it is listed (``contains_pii`` False).
"""

from services.management.app import dsar


def test_manifest_discloses_team_membership_as_id_only_data() -> None:
    """``team_members`` is listed, references users.id, and is flagged as non-PII."""
    by_table = {e["table"]: e for e in dsar.data_holding_manifest()}
    entry = by_table["team_members"]
    assert entry["contains_pii"] is False
    assert "users.id" in entry["reference"]


def test_manifest_has_no_duplicate_tables() -> None:
    """One entry per table (a regression guard for the hand-curated list)."""
    tables = [e["table"] for e in dsar.data_holding_manifest()]
    assert len(tables) == len(set(tables))
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/management/test_dsar_teams.py --no-cov -q`
Expected: FAIL `KeyError: 'team_members'`.

- [ ] **Step 3: Implement and regenerate the spec**

```diff
--- a/services/management/app/dsar.py
+++ b/services/management/app/dsar.py
@@ -122,6 +122,12 @@
             "reference": "primary identity row (subject of this export/erasure)",
         },
         {
+            "table": "team_members",
+            "description": "The user's team membership and team role (ids only, no personal data).",
+            "contains_pii": False,
+            "reference": "team_members.user_id -> users.id",
+        },
+        {
             "table": "api_keys",
             "description": "API credentials issued to the user; keys are stored hashed.",
             "contains_pii": False,
```

Then regenerate and lint the spec:

```bash
make generate-openapi          # rewrites openapi/v1.yaml from the live blueprint
make openapi-lint              # spectral, fails on errors (warnings are pre-existing)
git diff --stat openapi/v1.yaml
```

Expected: the lint line `0 errors`; the diff adds the `/api/v1/teams...` paths and `team_id` on `CreateKeyRequest`/`CreateKeyResponse`.

- [ ] **Step 4: Run it and the spec/DSAR suites**

Run: `.venv/bin/python -m pytest tests/unit/management/test_dsar_teams.py tests/unit/management/test_dsar_routes.py tests/unit/management/test_openapi_spec.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/dsar.py tests/unit/management/test_dsar_teams.py openapi/v1.yaml
git commit -m "feat(teams): DSAR manifest lists team_members; regenerate OpenAPI"
```

---

<!-- TASK 26 -->
## Task 27: Cross-plane end-to-end tests (spec §11 E2E)

**Files:**
- Modify: `tests/unit/management/conftest.py` (add `SharedValkey` and the `shared_valkey` fixture)
- Test: `tests/unit/management/test_teams_e2e.py` (create)

**Interfaces:**
- Consumes: everything above. One real SQLite database and one shared fake Valkey connect the management HTTP app (real signed JWTs) to the proxy's REAL `ApiKeyAuthenticator` + `ApiKeyAuthCache` -- no mock of any code under test.
- Produces: fixture `shared_valkey` (one dict seen through both planes' client shapes -- management's sync `delete`, the proxy's async `get`/`set`) installed as management's `redis_client`.
- Scenarios (all required by spec §11 E2E / Review Focus): (1) lifecycle -- org admin creates a team, a teamless user's management-created key authenticates at the proxy teamless, is **adopted** into the team when added, sees a promotion immediately, a new key defaults into the team, and after removal the same key authenticates teamless with no team scopes (`# regression: gh-303` + D7); (2) a team admin manages only their own team (read/add on another team = 403; cannot delete their own); (3) member and viewer can read but every mutation is a 403, and only the member may bind a key to the team; (4) a cross-team member cannot read, list members, or issue a key into another team; (5) flipping `waddleai.disable-teams` ON makes the proxy teamless for that key once its cache entry is dropped (the key itself keeps working).

- [ ] **Step 1: Write the test and fixture**

```diff
--- a/tests/unit/management/conftest.py
+++ b/tests/unit/management/conftest.py
@@ -904,6 +904,37 @@
     return _make


+class SharedValkey:
+    """One dict seen through BOTH planes' client shapes: management (sync) and proxy (async)."""
+
+    def __init__(self) -> None:
+        """Start empty."""
+        self.store: dict[str, str] = {}
+
+    def delete(self, *keys: str) -> int:
+        """Management's sync ``delete`` (what ``invalidate_key_ids`` calls)."""
+        return sum(self.store.pop(k, None) is not None for k in keys)
+
+    async def get(self, key: str) -> str | None:
+        """The proxy's async ``get``."""
+        return self.store.get(key)
+
+    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
+        """The proxy's async ``set`` (TTL ignored by the fake)."""
+        self.store[key] = value
+        return True
+
+
+@pytest.fixture
+def shared_valkey(monkeypatch) -> SharedValkey:
+    """A fake Valkey wired in as management's ``redis_client`` and usable as the proxy's client."""
+    import services.management.app.extensions as ext_mod
+
+    fake = SharedValkey()
+    monkeypatch.setattr(ext_mod, "redis_client", fake)
+    return fake
+
+
 @pytest.fixture
 def redis_spy(monkeypatch) -> MagicMock:
     """A recording stand-in for management's Valkey client (``extensions.redis_client``)."""
```

```python
"""Teams end to end across both planes (teams spec §11 E2E).

Management HTTP (real Quart app, real signed JWTs) on one side; the proxy's REAL
``ApiKeyAuthenticator`` + ``ApiKeyAuthCache`` on the other; one real database and one shared
fake Valkey between them. No mocks of any code under test.
"""

import pytest

from proxy.apps.proxy_server.auth_cache import ApiKeyAuthCache, ApiKeyAuthenticator
from shared.auth.rbac import Permission, RBACManager

P = Permission


@pytest.fixture
def proxy(team_world, shared_valkey) -> ApiKeyAuthenticator:
    """The proxy's cache-fronted authenticator over the shared database and Valkey."""
    cache = ApiKeyAuthCache(valkey=shared_valkey)
    return ApiKeyAuthenticator(rbac=RBACManager(team_world.db), cache=cache)


async def _create_key(client, headers, **extra) -> dict:
    """POST /keys and return the JSON body (asserting 201)."""
    resp = await client.post("/api/v1/keys", json={"name": "e2e", **extra}, headers=headers)
    assert resp.status_code == 201, await resp.get_data()
    return await resp.get_json()


async def test_lifecycle_membership_changes_reach_the_proxy(
    team_world, real_db_client, team_headers, proxy
) -> None:
    """Org admin builds a team; the member's mgmt-created key acts in it at the proxy.

    # regression: gh-303 (key works at the proxy) + D7 (every membership change is visible there
    once management invalidates): added -> promoted -> removed, same key throughout.
    """
    w = team_world
    boss, loner = team_headers("boss"), team_headers("loner")

    created = await real_db_client.post("/api/v1/teams", json={"name": "Ops"}, headers=boss)
    assert created.status_code == 201
    ops = (await created.get_json())["id"]

    # loner has no team yet: their management-created key is teamless but works at the proxy.
    first = await _create_key(real_db_client, loner)
    ctx = await proxy.authenticate(first["api_key"])
    assert (ctx.user_id, ctx.team_id) == (w.u["loner"], None)

    # Added to Ops as member: the SAME key now acts in Ops (adopted + cache invalidated).
    add = await real_db_client.post(
        f"/api/v1/teams/{ops}/members", json={"user_id": w.u["loner"], "team_role": "member"},
        headers=boss,
    )  # fmt: skip
    assert add.status_code == 201
    ctx = await proxy.authenticate(first["api_key"])
    assert (ctx.team_id, ctx.team_role) == (ops, "member")
    assert P.TEAM_USE in ctx.permissions

    # Promoted to admin: visible at the proxy at once.
    promote = await real_db_client.put(
        f"/api/v1/teams/{ops}/members/{w.u['loner']}", json={"team_role": "admin"}, headers=boss
    )
    assert promote.status_code == 200
    ctx = await proxy.authenticate(first["api_key"])
    assert ctx.team_role == "admin"
    assert P.TEAM_MEMBER_MANAGE in ctx.permissions

    # A key minted now defaults into Ops too.
    second = await _create_key(real_db_client, loner)
    assert second["team_id"] == ops
    assert (await proxy.authenticate(second["api_key"])).team_id == ops

    # Removed: the key still authenticates, but teamless -- no team scopes.
    gone = await real_db_client.delete(f"/api/v1/teams/{ops}/members/{w.u['loner']}", headers=boss)
    assert gone.status_code == 200
    ctx = await proxy.authenticate(first["api_key"])
    assert ctx.team_id is None
    assert P.TEAM_USE not in ctx.permissions


async def test_team_admin_manages_only_their_own_team(
    team_world, real_db_client, team_headers
) -> None:
    """Adam (admin of a1): may manage a1's membership; a2 is off limits; cannot delete a1."""
    w = team_world
    a1, a2 = f"/api/v1/teams/{w.t['a1']}", f"/api/v1/teams/{w.t['a2']}"
    adam = team_headers("adam")

    ok = await real_db_client.post(
        f"{a1}/members", json={"user_id": w.u["loner"], "team_role": "viewer"}, headers=adam
    )
    cross_read = await real_db_client.get(a2, headers=adam)
    cross_add = await real_db_client.post(
        f"{a2}/members", json={"user_id": w.u["boss"], "team_role": "member"}, headers=adam
    )
    delete_own = await real_db_client.delete(a1, headers=adam)

    assert (ok.status_code, cross_read.status_code, cross_add.status_code) == (201, 403, 403)
    assert delete_own.status_code == 403


async def test_member_and_viewer_cannot_mutate_anything(
    team_world, real_db_client, team_headers
) -> None:
    """Mia (member) and vic (viewer) can read their team but every mutation is a 403."""
    w = team_world
    base = f"/api/v1/teams/{w.t['a1']}"
    for who in ("mia", "vic"):
        hdr = team_headers(who)
        assert (await real_db_client.get(base, headers=hdr)).status_code == 200
        assert (await real_db_client.put(base, json={"name": "x"}, headers=hdr)).status_code == 403
        assert (await real_db_client.delete(base, headers=hdr)).status_code == 403
        add = await real_db_client.post(
            f"{base}/members", json={"user_id": w.u["loner"], "team_role": "member"}, headers=hdr
        )
        assert add.status_code == 403
    # Only the member can spend: viewer holds no team:key_create, so naming the team is refused.
    named = {"name": "k", "team_id": w.t["a1"]}
    assert (
        await real_db_client.post("/api/v1/keys", json=named, headers=team_headers("vic"))
    ).status_code == 403
    assert (
        await real_db_client.post("/api/v1/keys", json=named, headers=team_headers("mia"))
    ).status_code == 201


async def test_cross_team_member_cannot_issue_or_read_the_other_team(
    team_world, real_db_client, team_headers
) -> None:
    """Nat (member of a2) gets 403 reading a1, listing its members, and issuing a key into it."""
    w = team_world
    nat = team_headers("nat")
    base = f"/api/v1/teams/{w.t['a1']}"
    assert (await real_db_client.get(base, headers=nat)).status_code == 403
    assert (await real_db_client.get(f"{base}/members", headers=nat)).status_code == 403
    issue = await real_db_client.post(
        "/api/v1/keys", json={"name": "k", "team_id": w.t["a1"]}, headers=nat
    )
    assert issue.status_code == 403


async def test_kill_switch_flip_makes_the_proxy_teamless_after_invalidation(
    team_world, real_db_client, team_headers, proxy, monkeypatch
) -> None:
    """Review focus: turning ``waddleai.disable-teams`` ON stops team context at the proxy.

    The auth cache bakes the resolved team, so the flip takes effect when each entry is dropped
    (TTL, or an explicit invalidation as here) -- the key itself keeps authenticating.
    """
    from services.management.app.services.auth_cache_invalidation import (
        invalidate_user_auth_cache,
    )

    w = team_world
    body = await _create_key(real_db_client, team_headers("mia"))
    assert (await proxy.authenticate(body["api_key"])).team_id == w.t["a1"]

    monkeypatch.setenv("WADDLEAI_FLAG_DISABLE_TEAMS", "1")
    await invalidate_user_auth_cache(w.db, [w.u["mia"]])

    ctx = await proxy.authenticate(body["api_key"])
    assert ctx.team_id is None
    assert ctx.user_id == w.u["mia"]
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/unit/management/test_teams_e2e.py --no-cov -q`
Expected: `5 passed`. (All production code exists by now, so this is a verification task; to see it bite, comment out the `invalidate_user_auth_cache` call in `add_team_member` and re-run -- `test_lifecycle_membership_changes_reach_the_proxy` fails at the "added to Ops" assertion. Restore it before committing.)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/management/conftest.py tests/unit/management/test_teams_e2e.py
git commit -m "test(teams): cross-plane end-to-end tests for membership, keys and the kill switch"
```

- [ ] **Phase 7a gate:** Task 26-27 commands pass.
