---
name: wave2-cache-memory-audit-insights
description: Wave-2 audit (cache_configs/memory_config/memory_scoping) — the role→scope conversion trap, quart-schema idioms, and the divergent-token test technique for management API v1
metadata:
  type: feedback
---

Insights from the 2026-09-14 wave-2 security sweep on `services/management/app/api/v1/{cache_configs,memory_config,memory_scoping}.py`.

**Not every `role == "admin"` gate converts to a scope check.** cache_configs `_authorize_scope_write` gates its cross-tenant/global bypass on `role == "admin"`. It CANNOT be converted to a scope check with existing Permission members: the only cache scope, `CACHE_CONFIG_WRITE`, is held by BOTH admin AND resource_manager (rbac.py ROLE_PERMISSIONS), and the write routes already `@require_scope(CACHE_CONFIG_WRITE)`. Keying the bypass on it lets resource_manager write any org's + the global config → breaks tenant isolation and the #239 exhaustiveness tests.
- **Why:** house rule (security.md) says authorize on scope never role, but a faithful conversion needs an admin-ONLY scope (e.g. a new `CACHE_CONFIG_ADMIN`). memory_scoping's `role == "admin"` in promote/correct DID convert cleanly because `MEMORY_SCOPING_ADMIN` is admin-only (resource_manager lacks it).
- **How to apply:** before converting a role→scope admin gate, check whether the target scope is admin-only in rbac.py ROLE_PERMISSIONS. If a non-admin role also holds it, the conversion is a privilege-escalation regression — stop and flag that a new admin-only scope is needed. See [[management_route_gates_and_test_traps]].

**Proving a scope check ≠ role check (pre-change-failure test).** With standard conftest fixtures, role and scope are always aligned (scope derived from role via ROLE_PERMISSIONS), so a scope-vs-role conversion passes tests both before AND after. To get a genuine pre-change failure, mint a divergent token: inside a test (flask_app patch active) do `provider = auth._get_oidc_provider()` then `issue_token(UserContext(role=Role.RESOURCE_MANAGER, permissions={Permission.MEMORY_SCOPING_ADMIN}, ...), provider)`. `user_context_to_claims` serializes scope from `permissions` and role from `role.value` INDEPENDENTLY, so g.user ends up role=resource_manager + scope=[memory_scoping:admin]. Old role check → 403; new scope check → 200.

**quart-schema 0.19 idioms (management app).** Handlers may `return SomeDataclass(...), code` (instances, not just dicts) and @validate_response serializes+validates them. Stacking `@validate_response(Model, 200)` + `@validate_response(Model, 201)` works for one handler returning either status. Decorator order: `@require_auth` (outermost) → `@require_scope` → `@validate_response` → `@validate_request` so 401/403 fire before 400.

**Adding validation drifts openapi/v1.yaml** — see [[openapi_v1_drift_gate]] concept; the CI job hard-fails on any diff. In parallel-agent waves, do NOT regenerate v1.yaml (conflict magnet); drift is resolved once at consolidation.

**Mocked-DB insert mock retains cross-test state.** conftest caches the `_DBTable` per table name for the module-scoped app, and its `.insert` mock is NOT a child of mock_db, so `app_mock_db.reset_mock()` leaves prior tests' calls on it. Before `app_mock_db.<table>.insert.assert_not_called()`, call `app_mock_db.<table>.insert.reset_mock()` first. See [[management_venv_and_requirements]].
