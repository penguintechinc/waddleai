---
name: wave2-authz-reconciliation
description: Filling the wave-2 admin-cross-org scope gaps in rbac.py + 8 mgmt route files — the divergent-token fixture, the pre-existing-partial-conversion trap, and the worktree/pytest gotchas that made it work.
metadata:
  type: project
---

The 2026-09-14 wave-2 authz reconciliation (branch `feat/audit-w2-authz-reconcile`,
base `f075e3cc` = all 8 wave-2 streams merged). Minted 8 admin-EXCLUSIVE scopes in
`shared/auth/rbac.py` (added only to `Role.ADMIN`) and converted the `role == "admin"`
cross-org bypasses that earlier groups left on role-name checks:
`apikey:admin, quota:admin, model_alias:admin, routing_rule:admin, routing_policy:admin,
model_access_policy:admin, cache_config:admin, routing_decision:read`.

**Grep the WORKTREE for the CURRENT role-name checks — some are already converted.**
A merged wave had already converted `keys.py delete_key` to the admin-only
`APIKEY_DELETE` scope (reuse of an existing admin-exclusive scope), and
`cache_configs._authorize_scope_write` already carried a NOTE explicitly asking for
`CACHE_CONFIG_ADMIN`. So the surviving `role=="admin"` sites are the ONLY ones to touch.
Reuse an existing admin-only scope where one fits semantically (delete→apikey:delete);
mint otherwise. `QUOTA_ORG_UPDATE` did NOT fit the quotas bypasses (write-org-specific;
the bypasses span cross-entity reads + key writes) → minted `quota:admin`.

**Primary checkout (release/v0.2.X) and the worktree base (f075e3cc) DIVERGE** for all 8
route files (wave-2 added validation/pagination). `rbac.py`/`penguin_auth.py`/
`test_scope_authz.py` were identical; the route files were NOT. I edited the PRIMARY
`shared/auth/rbac.py` by reflex once — always operate on worktree paths, and `git
checkout -- <file>` in the primary to undo (it was clean there).

**Convert in-handler, never change the `@require_scope` decorator set** (keeps
`test_scope_authz.py`'s 113-route count + `_MIGRATED_SCOPES` membership green — the new
admin scopes are NOT in `_MIGRATED_SCOPES`, held by admin alone, so nothing drifts).
Helpers called inside `asyncio.to_thread` closures can't read `g`, so compute
`can_admin = _has_scope(<PERM>)` IN the handler and pass it in; helpers whose only use of
`user_role` was the admin check get their param renamed `user_role:str -> can_admin:bool`
(forces updating any DIRECT helper unit test — only `test_model_aliases_routes.py` had
those). See [[routing_authz_scope_gaps]] (now filled), [[wave2_cache_memory_audit_insights]].

Related: [[wave2_authz_reconciliation_test_traps]].
