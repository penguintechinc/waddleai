---
name: routing-authz-scope-gaps
description: Which routing/model-access admin routes have (and lack) an admin-tier OIDC scope for the "admin cross-tenant/global" privilege — role→scope conversion gaps
metadata:
  type: project
---

From the 2026-09-14 wave-2 audit of `services/management/app/api/v1/{routing_assignments,model_access_policies,model_aliases,routing_rules,routing_policies,routing_decisions,routing_dry_run}.py`.

The capability access gate on every state-changing route is ALREADY scope-based via `@require_scope(...)` (prior OIDC-scope migration wave). The residual `user_role == "admin"` / `"resource_manager"` checks all live inside `_can_write`/`_can_access`/`_visible_query`/`_visible_org_filter` and implement **tenant isolation with a super-admin cross-tenant bypass** — the org/user comparison is PRESERVED per house rule; only the admin-bypass half is a role-name gate.

**Scopes that exist** (`shared/auth/rbac.py` Permission enum): `ROUTING_ASSIGNMENT_WRITE`/`ROUTING_ASSIGNMENT_ADMIN`, `ROUTING_POLICY_WRITE`/`ROUTING_POLICY_DELETE`, `ROUTING_RULE_WRITE`, `MODEL_ALIAS_WRITE`, `MODEL_ACCESS_POLICY_WRITE`/`MODEL_ACCESS_POLICY_DELETE`, `ROUTING_DRY_RUN_ADMIN`.

**Only routing_assignments has both tiers** → `_can_write`/`_visible_query` were converted to scope-based (`ROUTING_ASSIGNMENT_ADMIN` = global/cross-org write, `ROUTING_ASSIGNMENT_WRITE` = own-org write; tenant compare preserved).

**Scope GAPS — no admin-tier scope to express "admin may write global / act cross-org"** (left role-name based, reported, did NOT edit rbac.py):
- `model_aliases`: no `MODEL_ALIAS_ADMIN` (only `MODEL_ALIAS_WRITE`, held by admin+rm) → can't scope "only admin writes a global/NULL-org alias".
- `routing_rules`: no `ROUTING_RULE_ADMIN` → same for global rules.
- `routing_policies`: no policy write-admin scope (only `ROUTING_POLICY_DELETE`) → `_can_access` admin cross-org bypass unscopable.
- `model_access_policies`: no write-admin scope (only `MODEL_ACCESS_POLICY_DELETE`) → `_can_write` admin cross-org bypass unscopable.
- `routing_decisions`: NO routing_decision scope at all (routes are `@require_auth`-only) → admin cross-org read visibility (`_visible_org_filter`, `list_decisions_summary` org selection) unscopable.

If a future task needs these converted, mint `*_ADMIN` (and a `routing_decision:read`) Permission first. `routing_dry_run` has no role-name checks (admin-only via `@require_scope(ROUTING_DRY_RUN_ADMIN)`).

**UPDATE 2026-09-22 — GAPS FILLED.** All of the above were minted and converted in `feat/audit-w2-authz-reconcile`: `MODEL_ALIAS_ADMIN`, `ROUTING_RULE_ADMIN`, `ROUTING_POLICY_ADMIN`, `MODEL_ACCESS_POLICY_ADMIN`, `ROUTING_DECISION_READ` (+ `APIKEY_ADMIN`, `QUOTA_ADMIN`, `CACHE_CONFIG_ADMIN`) now exist admin-only in rbac.py and gate the former role-name bypasses in-handler. See [[wave2_authz_reconciliation]].

Related: [[quart_schema_validation_behaviors]], [[management_authz_testability_and_openapi_gate]].
