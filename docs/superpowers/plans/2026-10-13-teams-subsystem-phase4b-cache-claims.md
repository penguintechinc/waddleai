# Teams Subsystem — Phase 4b: Proxy Auth Cache and JWT `ext` Claim Plumbing

> Part of the [Teams Subsystem Implementation Plan](2026-10-13-teams-subsystem.md).
> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Spec:** [`../specs/2026-10-13-teams-subsystem-design.md`](../specs/2026-10-13-teams-subsystem-design.md) §6, §7 ("UserContext plumbing"), §11 (Auth).
**Run tests with:** `.venv/bin/python -m pytest <path> --no-cov -q`.
**Prerequisite:** Phase 4a (`RBACManager.resolve_key_team`, team-aware `build_user_context`).

---

<!-- TASK 11 -->
## Task 11: `CachedKeyRecord` and `ApiKeyAuthenticator` carry the resolved team

**Files:**
- Modify: `proxy/apps/proxy_server/auth_cache.py`
- Modify: `tests/unit/proxy/test_auth_cache.py` (`_mock_rbac` test double)
- Test: `tests/unit/proxy/test_auth_cache_teams.py` (create)

**Interfaces:**
- Consumes: `RBACManager.resolve_key_team`, `RBACManager.build_user_context(..., team_id=, team_role=)`.
- Produces: `CachedKeyRecord.team_id: int | None = None`, `.team_role: str | None = None` (frozen/slots dataclass: defaulted fields go last); `from_dict` reads them with `.get`, so entries written by a pre-teams proxy replica during a rolling deploy read as teamless instead of failing; `_db_lookup_and_verify` resolves the team via `rbac.resolve_key_team(key_record, user)`; both authenticate paths pass the cached/resolved team to `build_user_context`. The cached value is the RESOLVED team, so a cache hit never touches the DB.
- The existing `_mock_rbac` double hard-codes `build_user_context`'s keyword set, so it must accept the two new kwargs and `resolve_key_team` must return `(None, None)` -- otherwise every existing cache test breaks.
- Review Focus (membership change not reflected until invalidation): `TestMembershipChangeCoherence` proves the stale value is served until `cache.invalidate(key_id)`, then the new role.

- [ ] **Step 1: Write the failing test and update the double**

```diff
--- a/tests/unit/proxy/test_auth_cache.py
+++ b/tests/unit/proxy/test_auth_cache.py
@@ -256,9 +256,18 @@
     rbac = MagicMock()
     rbac.fetch_key_and_user.return_value = found
     rbac.touch_api_key_last_used.return_value = None
+    rbac.resolve_key_team.return_value = (None, None)  # teamless key: no team_members lookup

     def _build_user_context(
-        *, user_id, username, role, organization_id, managed_orgs, api_key_id=None
+        *,
+        user_id,
+        username,
+        role,
+        organization_id,
+        managed_orgs,
+        api_key_id=None,
+        team_id=None,
+        team_role=None,
     ):
         from shared.auth.rbac import ROLE_PERMISSIONS, UserContext
```

```python
"""Auth-cache team plumbing and membership-change coherence (teams spec D3/D7).

Real ``RBACManager`` over a real schema (``team_world``), real ``ApiKeyAuthenticator`` and the
real in-process ``ApiKeyAuthCache`` -- the only fake is the absence of Valkey.
"""

from __future__ import annotations

import pytest
from passlib.hash import bcrypt

from proxy.apps.proxy_server.auth_cache import (
    ApiKeyAuthCache,
    ApiKeyAuthenticator,
    CachedKeyRecord,
)
from shared.auth.rbac import Permission, RBACManager

pytestmark = pytest.mark.asyncio

_SECRET = "wa-k1-secretvalue"  # noqa: S105 -- test fixture credential
_HASH = bcrypt.hash(_SECRET)
P = Permission


def _record(**over) -> CachedKeyRecord:
    """Build a CachedKeyRecord with team fields overridable."""
    base = {
        "key_record_id": 1, "key_hash": "h", "user_id": 5, "username": "u", "role": "user",
        "organization_id": 1, "managed_orgs": [], "user_enabled": True,
    }  # fmt: skip
    return CachedKeyRecord(**{**base, **over})


def _authenticator(world, user: str, team_key: str | None):
    """Seed key ``k1`` for ``user`` pinned to ``team_key`` and return (authenticator, cache)."""
    db = world.db
    db.api_keys.insert(
        key_id="k1", key_hash=_HASH, user_id=world.u[user],
        organization_id=db.users[world.u[user]].organization_id, name="k", enabled=True,
        team_id=world.t[team_key] if team_key else None,
    )  # fmt: skip
    db.commit()
    cache = ApiKeyAuthCache(valkey=None)
    return ApiKeyAuthenticator(rbac=RBACManager(db), cache=cache), cache


class TestCachedKeyRecordTeamFields:
    """The cached shape carries the resolved acting team and stays forward-compatible."""

    def test_round_trip_with_team(self):
        """to_dict/from_dict preserves team_id and team_role."""
        record = _record(team_id=3, team_role="admin")
        assert CachedKeyRecord.from_dict(record.to_dict()) == record

    def test_entry_written_by_a_pre_teams_replica_reads_as_teamless(self):
        """A payload with no team keys (older replica, rolling deploy) defaults to None/None."""
        payload = _record().to_dict()
        payload.pop("team_id")
        payload.pop("team_role")
        rebuilt = CachedKeyRecord.from_dict(payload)
        assert (rebuilt.team_id, rebuilt.team_role) == (None, None)


class TestTeamContextThroughTheAuthenticator:
    """Miss populates the team; a hit reproduces it without touching the DB."""

    async def test_miss_resolves_the_team_and_caches_it(self, team_world):
        """First auth: DB path resolves mia -> a1/member and the cache stores the same."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        ctx = await auth.authenticate(_SECRET)
        assert (ctx.team_id, ctx.team_role) == (team_world.t["a1"], "member")
        cached = await cache.get("k1")
        assert isinstance(cached, CachedKeyRecord)
        assert (cached.team_id, cached.team_role) == (team_world.t["a1"], "member")

    async def test_hit_reproduces_team_scopes_without_a_db_query(self, team_world):
        """Second auth is served from cache: same team + scopes even if the DB is gone."""
        auth, _cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        team_world.db(team_world.db.team_members.id > 0).delete()  # DB no longer knows the team
        ctx = await auth.authenticate(_SECRET)
        assert ctx.team_id == team_world.t["a1"]
        assert P.TEAM_USE in ctx.permissions


class TestMembershipChangeCoherence:
    """Review focus: a membership change is invisible until the cache entry is invalidated."""

    async def test_change_is_not_reflected_until_invalidation(self, team_world):
        """Promote mia to admin in the DB: stale cache says member; invalidate -> admin."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        db = team_world.db
        db(db.team_members.user_id == team_world.u["mia"]).update(team_role="admin")
        db.commit()

        stale = await auth.authenticate(_SECRET)
        assert stale.team_role == "member"  # bounded only by the cache TTL / invalidation

        await cache.invalidate("k1")
        fresh = await auth.authenticate(_SECRET)
        assert fresh.team_role == "admin"
        assert P.TEAM_MEMBER_MANAGE in fresh.permissions

    async def test_removal_then_invalidation_drops_the_acting_team(self, team_world):
        """Remove mia from a1: after invalidation her key acts teamless (no team scopes)."""
        auth, cache = _authenticator(team_world, "mia", "a1")
        await auth.authenticate(_SECRET)
        db = team_world.db
        db(db.team_members.user_id == team_world.u["mia"]).delete()
        db.commit()
        await cache.invalidate("k1")
        ctx = await auth.authenticate(_SECRET)
        assert ctx.team_id is None
        assert P.TEAM_USE not in ctx.permissions
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_auth_cache_teams.py --no-cov -q -x`
Expected: FAIL `TypeError: CachedKeyRecord.__init__() got an unexpected keyword argument 'team_id'`.

- [ ] **Step 3: Implement**

```diff
--- a/proxy/apps/proxy_server/auth_cache.py
+++ b/proxy/apps/proxy_server/auth_cache.py
@@ -147,6 +147,10 @@
     organization_id: int
     managed_orgs: list[int]
     user_enabled: bool
+    #: Resolved acting team (teams spec D3) at cache-fill time; ``None`` = teamless. Defaults
+    #: keep entries written by pre-teams replicas readable during a rolling deploy.
+    team_id: int | None = None
+    team_role: str | None = None

     def to_dict(self) -> dict[str, Any]:
         """Serialize to a plain dict suitable for ``json.dumps``."""
@@ -164,6 +168,8 @@
             organization_id=payload["organization_id"],
             managed_orgs=list(payload.get("managed_orgs") or []),
             user_enabled=bool(payload.get("user_enabled", True)),
+            team_id=payload.get("team_id"),
+            team_role=payload.get("team_role"),
         )


@@ -331,6 +337,7 @@
         return None
     key_record, user = found
     secret_ok = bool(bcrypt.verify(credential, key_record.key_hash))
+    team_id, team_role = rbac.resolve_key_team(key_record, user)
     record = CachedKeyRecord(
         key_record_id=key_record.id,
         key_hash=key_record.key_hash,
@@ -340,6 +347,8 @@
         organization_id=user.organization_id,
         managed_orgs=_normalize_managed_orgs(user.managed_orgs),
         user_enabled=bool(user.enabled),
+        team_id=team_id,
+        team_role=team_role,
     )
     return record, secret_ok

@@ -433,6 +442,8 @@
             organization_id=cached.organization_id,
             managed_orgs=cached.managed_orgs,
             api_key_id=cached.key_record_id,
+            team_id=cached.team_id,
+            team_role=cached.team_role,
         )
         self._schedule_last_used_touch(cached.key_record_id)
         return context
@@ -466,6 +477,8 @@
             organization_id=record.organization_id,
             managed_orgs=record.managed_orgs,
             api_key_id=record.key_record_id,
+            team_id=record.team_id,
+            team_role=record.team_role,
         )
         self._schedule_last_used_touch(record.key_record_id)
         return context
```

- [ ] **Step 4: Run it and the existing cache suites**

Run: `.venv/bin/python -m pytest tests/unit/proxy/test_auth_cache_teams.py tests/unit/proxy/test_auth_cache.py tests/unit/proxy/test_release_audit_20260923.py --no-cov -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/auth_cache.py tests/unit/proxy/test_auth_cache.py tests/unit/proxy/test_auth_cache_teams.py
git commit -m "feat(teams): proxy auth cache carries the resolved acting team"
```

---

<!-- TASK 12 -->
## Task 12: Team context in the JWT `ext` claim, through every converter

**Files:**
- Modify: `shared/auth/penguin_auth.py` (claim builders/parsers)
- Test: `tests/unit/test_penguin_auth_team_claims.py` (create)

**Interfaces:**
- Produces: `user_context_to_claims` and `user_context_to_claims_dict` emit `ext.team_id` (string) and `ext.team_role`; `claims_to_user_context` / `claims_dict_to_user_context` read them back via `_parse_team_id` (only an ASCII-digit **string** yields an int; anything else -> `None`) and `_parse_team_role` (only a string survives). The legacy top-level `teams` claim is untouched: penguin-aaa (`penguin-aaa==0.2.1`, external) hard-codes the `teams` wire key and offers no hook for new top-level claims, so team context rides in the lib's existing `ext` bag -- **no wire rename, no lib change** (spec D6). `verify_token` already forwards `ext`, so a mint -> verify round trip works unmodified; the test proves it with a real RS256 token.
- Malformed `ext` values never raise and never invent a team (fail closed: a non-string role expands to no scopes in `expand_team_permissions`).

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_penguin_auth_team_claims.py --no-cov -q -x`
Expected: FAIL `KeyError: 'team_id'` (the converters do not emit it yet).

- [ ] **Step 3: Implement**

```diff
--- a/shared/auth/penguin_auth.py
+++ b/shared/auth/penguin_auth.py
@@ -254,6 +254,18 @@
 # ---------------------------------------------------------------------------


+def _parse_team_id(raw: object) -> int | None:
+    """Parse a claim-borne team id; only an ASCII-digit string yields an id, anything else None."""
+    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
+        return int(raw)
+    return None
+
+
+def _parse_team_role(raw: object) -> str | None:
+    """Return a claim-borne team role only if it is a string (scope expansion validates it)."""
+    return raw if isinstance(raw, str) else None
+
+
 def user_context_to_claims(user_context: UserContext) -> Claims:
     """Convert WaddleAI UserContext to penguin-aaa Claims."""
     scopes: list[str] = []
@@ -277,6 +289,10 @@
             "api_key_id": (
                 str(user_context.api_key_id) if user_context.api_key_id is not None else None
             ),
+            # Team context rides in ``ext`` (teams spec D6): penguin-aaa hard-codes the
+            # top-level ``teams`` wire key, so it cannot be renamed without forking the lib.
+            "team_id": str(user_context.team_id) if user_context.team_id is not None else None,
+            "team_role": user_context.team_role,
         },
     )

@@ -309,6 +325,8 @@
         managed_orgs=[int(t) for t in (claims.teams or []) if t.isdigit()],
         permissions=permissions,
         api_key_id=api_key_id,
+        team_id=_parse_team_id(claims.ext.get("team_id") if claims.ext else None),
+        team_role=_parse_team_role(claims.ext.get("team_role") if claims.ext else None),
     )


@@ -332,6 +350,8 @@
         "tenant": str(uc.organization_id),
         "teams": [str(o) for o in (uc.managed_orgs or [])],
         "api_key_id": (str(uc.api_key_id) if uc.api_key_id is not None else None),
+        "team_id": (str(uc.team_id) if uc.team_id is not None else None),
+        "team_role": uc.team_role,
     }


@@ -368,6 +388,8 @@
         managed_orgs=[int(t) for t in (d.get("teams", [])) if str(t).isdigit()],
         permissions=permissions,
         api_key_id=api_key_id,
+        team_id=_parse_team_id(d.get("team_id")),
+        team_role=_parse_team_role(d.get("team_role")),
     )
```

- [ ] **Step 4: Run it and the existing converter suites**

Run: `.venv/bin/python -m pytest tests/unit/test_penguin_auth_team_claims.py tests/unit/test_penguin_auth_conversions.py tests/unit/test_penguin_auth_keystore_guard.py tests/unit/proxy/test_proxy_server_main.py --no-cov -q`
Expected: all pass (`14 passed` in the new file).

- [ ] **Step 5: Commit**

```bash
git add shared/auth/penguin_auth.py tests/unit/test_penguin_auth_team_claims.py
git commit -m "feat(teams): carry team_id/team_role in the JWT ext claim"
```

- [ ] **Phase 4b gate:** Step 4 of both tasks pass.
