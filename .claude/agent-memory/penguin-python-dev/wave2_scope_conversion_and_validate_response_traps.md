---
name: wave2-scope-conversion-and-validate-response-traps
description: Management API role-name→scope conversions, @validate_response/@validate_request, and pagination — the gates and mock traps that decide whether an audit change passes.
metadata:
  type: project
---

Applying the Wave-2 audit pattern (role→scope, validate_request/response, pagination) to `services/management/app/api/v1/*.py`. Learned while doing users/organizations/usage.py.

**Convert role gates IN-HANDLER, never add a `@require_scope` decorator.** `tests/unit/management/test_scope_authz.py::test_scoped_routes_match_audited_count` pins EXACTLY 113 routes carrying `_required_scopes`; adding a decorator breaks it. Use a local `_has_scope(perm)` helper (`perm.value in set(getattr(g,"user",None or {}).get("scope") or [])`) — the keys.py:74 convention. Placing `@validate_response`/`@validate_request` ABOVE the existing `@require_scope` is fine; `_required_scopes` still propagates up via functools.wraps `__dict__` copy (verified: 113 count held).

**No admin-only "read-all" scope exists** for users/orgs/usage. To keep "admin sees all, others own-org" without BROADENING, gate the admin branch on an admin-EXCLUSIVE scope even if write-named: chose USER_CREATE (users admin tier), USER_MANAGE (users org tier = admin+RM), ORG_ADMIN_UPDATE (orgs admin tier), ANALYTICS_SYSTEM (usage/cross-org analytics), ORG_READ (usage own-org = admin+RM+reporter, excludes plain user). ORG_READ/USER_READ/ANALYTICS_READ are held by RM/reporter/user so they can NOT gate an admin-only tier. This is a real scope-vocabulary gap, flagged not fixed (rbac.py off-limits).

**The mocked DB ignores `.select()` queries**, so a pure scoping branch (admin→all-rows vs non-admin→own-org, both 200) is NOT status/response observable — you cannot write a refuse/allow test for it. Only genuine 403 gates and response-observable forces (create_user echoes forced `organization_id`/`role`) are testable. Neuter-verify by forcing `_has_scope`→`True` globally: every refuse/force test flips to failing at once, then restore.

**mypy is NOT --strict** (no `disallow_untyped_defs`); baseline empty so any NEW error fails `scripts/mypy-gate.sh`. Route handlers are left unannotated so mypy skips their bodies. Adding a `data: XRequest` param for `@validate_request` makes mypy check that body → `db` (`DB | None`) errors → add a narrowed `_db()` (keys.py pattern) and type `update_fields: dict[str, object]`. Nested unannotated `def _fetch()/_insert()` are skipped and may use `db` directly; lambdas ARE analyzed. Running `mypy <file.py>` directly over-reports vs the whole-tree gate — trust `scripts/mypy-gate.sh` (runs `mypy proxy services shared scripts tests`).

**Contract snapshots** (`tests/contract/snapshots/*.json`) do exact-equality after volatile-key normalization; they need a live hypercorn subprocess (NOT in the unit run). Adding a `pagination` envelope key to a list response REQUIRES bumping `mgmt_users_list.json`/`mgmt_orgs_list.json` (added `pagination:{limit,page,pages:null,total:null}`). Omit `page.meta()`'s total (no `.count()` call) or unit tests 500 — the plain mock's `.count()` returns a MagicMock and `page.meta(total)` does arithmetic on it. Do NOT paginate aggregation endpoints (summary/by-model/cost/by-user/by-key) — `limitby` would corrupt the sums; only true lists (list_users, list_organizations, usage/export). export gets no `@validate_response` (dual CSV/JSON return).

See [[management_route_gates_and_test_traps]], [[management_authz_testability_and_openapi_gate]], [[penguin_aaa_token_ttl_and_jti]].
