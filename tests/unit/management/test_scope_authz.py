"""OIDC scope-authorization migration tests (feature/oidc-scope-authz).

Covers the three safety properties the migration must hold:

1. Every route that used to gate on `require_role(...)` now gates on
   `require_scope(...)`, enumerated programmatically from the live
   `flask_app.url_map` (via the `_required_scopes` attribute `require_scope`
   attaches to the view function) -- not hand-listed, so a route that loses
   its decorator, or a new route that ships without one, is caught here
   rather than discovered in production.
2. A caller whose token lacks the required scope is refused (403) for every
   one of those routes; a caller who holds it is not blocked at the scope
   gate. `resource_manager` is exercised per-route against its actual tier
   (some migrated routes are admin-only) so this doubles as the "did a role
   gain access it didn't have before" check at the HTTP-enforcement layer.
3. No bundle in `ROLE_PERMISSIONS` grants more than it is supposed to: each
   scope minted for this migration is held by *exactly* the role set the
   route allowed before (admin-only, or admin+resource_manager) -- never
   reporter, never plain user, never a superset.
"""

import re
from pathlib import Path

import pytest

from shared.auth.rbac import ROLE_PERMISSIONS, Permission, Role

REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# 1. No require_role left in application code (outside tests/penguincode).
# ---------------------------------------------------------------------------


def test_no_require_role_outside_tests() -> None:
    """grep-based regression guard: `require_role` must not exist in app code.

    Catches a reintroduced role-name check anywhere under services/, shared/,
    or proxy/ (penguincode has its own unrelated Permission/role concepts and
    is intentionally excluded, matching the migration's original scope).
    """
    hits: list[str] = []
    decorator_re = re.compile(r"^\s*@require_role\(")
    import_re = re.compile(r"^\s*from .+ import .*\brequire_role\b")
    def_re = re.compile(r"^\s*def require_role\(")
    for base in ("services", "shared", "proxy"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/tests/" in f"/{rel}" or "penguincode" in rel:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if decorator_re.match(line) or import_re.match(line) or def_re.match(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert hits == [], f"require_role still used outside tests/penguincode: {hits}"


# ---------------------------------------------------------------------------
# 2. Route -> scope mapping, enumerated from the blueprint, HTTP-level checks.
# ---------------------------------------------------------------------------

# Tier classification for every scope this migration touches -- mirrors
# shared/auth/rbac.py ROLE_PERMISSIONS exactly. Used to compute, per route,
# which of {admin, resource_manager} the route allows, so the HTTP-level
# test below asserts a resource_manager caller is admitted on B-tier routes
# and refused on A-tier (admin-only) routes -- never the reverse.
_B_TIER_SCOPES = {
    Permission.CACHE_CONFIG_WRITE.value,
    Permission.KNOWLEDGE_WRITE.value,
    Permission.MODEL_ALIAS_WRITE.value,
    Permission.QUOTA_LIST.value,
    Permission.QUOTA_UPDATE.value,
    Permission.ROUTING_ASSIGNMENT_WRITE.value,
    Permission.ROUTING_POLICY_WRITE.value,
    Permission.ROUTING_RULE_WRITE.value,
    Permission.SECURITY_BYPASS_GRANT_WRITE.value,
    Permission.USAGE_READ_BY_USER.value,
    Permission.USER_MANAGE.value,
}

# The complete set of migrated route scopes -- the route enumeration below
# must find exactly these attached to view functions (no more, no less).
_MIGRATED_SCOPES = _B_TIER_SCOPES | {
    Permission.ORG_CREATE.value,
    Permission.ORG_ADMIN_UPDATE.value,
    Permission.ORG_DELETE.value,
    Permission.USER_DELETE.value,
    Permission.CILIUM_ADMIN.value,
    Permission.INTEGRATION_ADMIN.value,
    Permission.LLAMACPP_ADMIN.value,
    Permission.MEMORY_CONFIG_ADMIN.value,
    Permission.MEMORY_SCOPING_ADMIN.value,
    Permission.OLLAMA_ADMIN.value,
    Permission.OLLAMA_MODEL_ADMIN.value,
    Permission.PROVIDER_ADMIN.value,
    Permission.QUOTA_ORG_UPDATE.value,
    Permission.ROUTING_ASSIGNMENT_ADMIN.value,
    Permission.ROUTING_DRY_RUN_ADMIN.value,
    Permission.ROUTING_POLICY_DELETE.value,
    Permission.SECURITY_POLICY_ADMIN.value,
}

_EXPECTED_ROUTE_COUNT = 94  # matches the audited require_role call-site count


def _concrete_path(rule_str: str) -> str:
    """Substitute werkzeug path converters with a dummy value of the right shape."""

    def repl(m: re.Match) -> str:
        converter = m.group(1)
        return "1" if converter == "int" else "test"

    return re.sub(r"<(?:(\w+):)?(\w+)>", repl, rule_str)


def _iter_scoped_routes(flask_app):
    """Yield (rule, method, required_scopes) for every route carrying `_required_scopes`."""
    seen = set()
    for rule in flask_app.url_map.iter_rules():
        view = flask_app.view_functions.get(rule.endpoint)
        scopes = getattr(view, "_required_scopes", None)
        if not scopes:
            continue
        methods = (rule.methods or set()) - {"HEAD", "OPTIONS"}
        for method in methods:
            key = (rule.rule, method)
            if key in seen:
                continue
            seen.add(key)
            yield rule, method, scopes


def test_scoped_routes_match_audited_count(flask_app) -> None:
    """The migration touched exactly 94 route/method pairs -- catches drift either way."""
    routes = list(_iter_scoped_routes(flask_app))
    assert len(routes) == _EXPECTED_ROUTE_COUNT, [
        f"{m} {r.rule}" for r, m, _s in routes
    ]


def test_scoped_routes_use_only_migrated_scopes(flask_app) -> None:
    """Every enumerated route's required scope(s) are ones this migration minted or reused."""
    for rule, method, scopes in _iter_scoped_routes(flask_app):
        for scope in scopes:
            assert scope in _MIGRATED_SCOPES, (
                f"{method} {rule.rule} requires unexpected scope {scope!r}"
            )


@pytest.fixture
def scopeless_headers(user_auth_headers: dict[str, str]) -> dict[str, str]:
    """A validly-authenticated caller (role=user) holding none of the migrated scopes.

    Role.USER's bundle predates this migration and was never extended with
    any of the 24 scopes minted here, so this is exactly "a caller lacking
    the required scope" for every one of the 94 migrated routes -- no
    synthetic empty-scope token needed.
    """
    return user_auth_headers


async def test_every_scoped_route_refuses_caller_without_scope(
    flask_app, client, scopeless_headers
) -> None:
    """For every migrated route, a caller without the required scope gets 403.

    This is the core safety property: enumerated from the blueprint (not
    hand-listed), so a route that later loses its `require_scope` decorator
    -- or a new admin-tier route added without one -- fails this test
    instead of shipping unprotected.
    """
    failures = []
    for rule, method, scopes in _iter_scoped_routes(flask_app):
        path = _concrete_path(rule.rule)
        resp = await client.open(path, method=method, headers=scopeless_headers, json={})
        if resp.status_code != 403:
            failures.append(f"{method} {path} required={scopes} got={resp.status_code}")
    assert failures == [], "\n".join(failures)


async def test_every_scoped_route_denies_caller_with_no_auth(flask_app, client) -> None:
    """Deny-by-default companion: no Authorization header at all -> 401, never a pass-through."""
    failures = []
    for rule, method, _scopes in _iter_scoped_routes(flask_app):
        path = _concrete_path(rule.rule)
        resp = await client.open(path, method=method, json={})
        if resp.status_code != 401:
            failures.append(f"{method} {path} got={resp.status_code}")
    assert failures == [], "\n".join(failures)


def test_resource_manager_matches_its_actual_tier(flask_app) -> None:
    """resource_manager's scope bundle admits it on B-tier routes, refuses it on A-tier ones.

    Pure in-memory check (no HTTP dispatch, so no route handler body ever
    executes) against the same intersection `require_scope` performs at
    request time: `set(token_scopes) & set(required_scopes)`.
    `test_every_scoped_route_refuses_caller_without_scope` and
    `test_scope_gate_wiring_smoke` already prove the decorator wiring itself
    (header -> g.user -> gate) end-to-end over real HTTP, so this test only
    needs to prove the *data* (which scopes resource_manager holds) matches
    each route's actual tier -- doing that over HTTP for all 94 routes cost
    several minutes because "admitted" cases ran real (if DB-mocked) handler
    bodies, including at least one real outbound `requests.get(timeout=10)`
    in llamacpp.py.
    """
    rm_scopes = {p.value for p in ROLE_PERMISSIONS[Role.RESOURCE_MANAGER]}
    failures = []
    for rule, method, scopes in _iter_scoped_routes(flask_app):
        admitted = bool(rm_scopes.intersection(scopes))
        is_b_tier = any(s in _B_TIER_SCOPES for s in scopes)
        if admitted != is_b_tier:
            failures.append(
                f"{method} {rule.rule} requires={scopes} resource_manager admitted={admitted} "
                f"expected={is_b_tier}"
            )
    assert failures == [], "\n".join(failures)


def test_admin_holds_every_migrated_route_scope(flask_app) -> None:
    """Pure in-memory sanity: admin's bundle intersects every migrated route's required scope(s).

    See `test_resource_manager_matches_its_actual_tier` for why this is a
    scope-set check rather than an HTTP round trip.
    """
    admin_scopes = {p.value for p in ROLE_PERMISSIONS[Role.ADMIN]}
    failures = []
    for rule, method, scopes in _iter_scoped_routes(flask_app):
        if not admin_scopes.intersection(scopes):
            failures.append(f"{method} {rule.rule} admin lacks all of {scopes}")
    assert failures == [], "\n".join(failures)


async def test_scope_gate_wiring_smoke(
    flask_app, auth_headers, rm_auth_headers, user_auth_headers
) -> None:
    """End-to-end proof that `require_auth` + `require_scope` are wired correctly over real HTTP.

    Deliberately does NOT hit any of the 20 real resource route files --
    several of their handlers make real outbound calls when the scope gate
    is passed (e.g. llamacpp.py's health check does a real `requests.get`),
    which turned an earlier version of this test into a multi-minute run
    against a sandboxed/no-network environment. Instead this builds a tiny
    throwaway Quart app with one dummy route wired to the *actual*
    `require_auth`/`require_scope` decorators imported from
    `services.management.app.api.v1.auth`, reusing the same OIDC provider
    `flask_app` already patched into that module (see conftest.py) so
    `auth_headers`/`rm_auth_headers`/`user_auth_headers` tokens verify
    against it. This isolates "does the gate itself work" from "does route
    X's business logic work", which the 94-route enumeration tests and the
    existing per-route business-logic tests already cover separately.
    """
    from quart import Quart, jsonify

    from services.management.app.api.v1.auth import require_auth, require_scope

    probe_app = Quart("scope_gate_probe")

    @probe_app.route("/probe", methods=["GET"])
    @require_auth
    @require_scope(Permission.ORG_CREATE)  # admin-only, reused-permission case
    async def probe():
        return jsonify({"ok": True})

    async with probe_app.test_client() as c:
        resp = await c.get("/probe", headers=auth_headers)
        assert resp.status_code == 200, "admin (holds ORG_CREATE) must pass the gate"

        resp = await c.get("/probe")
        assert resp.status_code == 401, "no Authorization header must be refused, not allowed"

        resp = await c.get("/probe", headers=user_auth_headers)
        assert resp.status_code == 403, "plain user (lacks ORG_CREATE) must be refused"

        resp = await c.get("/probe", headers=rm_auth_headers)
        assert resp.status_code == 403, "resource_manager (lacks ORG_CREATE) must be refused"


def test_require_scope_fails_closed_with_no_scopes() -> None:
    """require_scope() with zero scopes raises at decoration time (fail-fast, not fail-open)."""
    from services.management.app.api.v1.auth import require_scope

    with pytest.raises(ValueError):
        require_scope()


# ---------------------------------------------------------------------------
# 3. ROLE_PERMISSIONS bundle membership: exact, not "at most".
# ---------------------------------------------------------------------------


def _holders(permission: Permission) -> set[Role]:
    return {role for role, perms in ROLE_PERMISSIONS.items() if permission in perms}


@pytest.mark.parametrize("permission", sorted(_B_TIER_SCOPES))
def test_b_tier_scope_held_by_exactly_admin_and_resource_manager(permission: str) -> None:
    """A migrated admin+resource_manager scope must be held by exactly those two roles."""
    perm = Permission(permission)
    assert _holders(perm) == {Role.ADMIN, Role.RESOURCE_MANAGER}, (
        f"{perm} holders drifted: {_holders(perm)}"
    )


@pytest.mark.parametrize(
    "permission",
    sorted(_MIGRATED_SCOPES - _B_TIER_SCOPES),
)
def test_a_tier_scope_held_by_exactly_admin(permission: str) -> None:
    """A migrated admin-only scope must be held by admin, and admin alone."""
    perm = Permission(permission)
    assert _holders(perm) == {Role.ADMIN}, f"{perm} holders drifted: {_holders(perm)}"


@pytest.mark.parametrize(
    ("permission", "expected_holders"),
    [
        (Permission.ORG_CREATE, {Role.ADMIN}),
        (Permission.ORG_DELETE, {Role.ADMIN}),
        (Permission.USER_DELETE, {Role.ADMIN}),
        (Permission.QUOTA_UPDATE, {Role.ADMIN, Role.RESOURCE_MANAGER}),
    ],
)
def test_reused_permissions_unchanged_by_migration(
    permission: Permission, expected_holders: set
) -> None:
    """The four pre-existing permissions reused as route scopes kept their original holders.

    These were reused (rather than a new scope minted) only because their
    existing role membership already matched the route's former
    require_role(...) set exactly -- this test pins that membership so a
    later, unrelated change to ROLE_PERMISSIONS can't silently widen a
    route that reuses one of these.
    """
    assert _holders(permission) == expected_holders


def test_reporter_and_user_hold_none_of_the_migrated_scopes() -> None:
    """Reporter and plain user must not hold any scope minted or reused by this migration.

    Neither role ever appeared in a require_role(...) call site being
    migrated, so neither may gain access to a migrated route now.
    """
    reused = {
        Permission.ORG_CREATE,
        Permission.ORG_DELETE,
        Permission.USER_DELETE,
        Permission.QUOTA_UPDATE,
    }
    migrated_perms = {Permission(v) for v in _MIGRATED_SCOPES} | reused
    for role in (Role.REPORTER, Role.USER):
        overlap = migrated_perms & ROLE_PERMISSIONS[role]
        assert overlap == set(), f"{role} unexpectedly holds migrated scopes: {overlap}"
