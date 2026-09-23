---
name: wave2-authz-reconciliation-test-traps
description: The test-harness gotchas that decide whether a scope-vs-role conversion is provable — divergent-token fixture, cross-directory conftest breakage, ruff-format pre-commit abort, mypy on **dict.
metadata:
  type: feedback
---

Testing a `role == "admin"` -> admin-only-scope conversion in `tests/unit/management/`.

**A scope-vs-role conversion is INVISIBLE to a standard-fixture test** — role and scope
are aligned there (scope derived from role via ROLE_PERMISSIONS), so it passes before AND
after. The only discriminating test uses a DIVERGENT token: role=resource_manager +
explicit `permissions={<admin scope>}`. `user_context_to_claims` serializes `scope` from
`permissions` and `roles` from `role.value` INDEPENDENTLY, so `g.user` ends up
role=resource_manager + scope=[apikey:admin]. Old role check → 403; new scope check → 200.
- **Why:** without it the test proves nothing and passes with the fix reverted.
- **How to apply:** add a factory fixture to `tests/unit/management/conftest.py` (NOT a
  bare import into a test file — the missing-`__init__.py` namespace edge-case re-lru_caches
  a 2nd provider with a different keypair). `divergent_headers(permissions, role="resource_manager")`
  builds `UserContext` + `issue_token(uc, _test_oidc_provider())`. Prove failure by
  reverting the 8 route files to base (`git checkout -- <file>`, keep rbac.py so the new
  Permission members still exist), running `-k divergent` → all should FAIL, then restore
  byte-exact (`cp` from a scratch copy + `cmp -s`). 20/20 failed pre-change.
- Some sites need MULTIPLE scopes on the divergent token: `set_key_quota` needs
  `quota:update` (passes the privileged-fields gate) AND `quota:admin` (bypasses
  ownership) — a `quota:admin`-only token is refused earlier by the privilege check.

**Never run `tests/unit/test_rbac.py` in the SAME pytest invocation as
`tests/unit/management/*`** — it makes every management fixture ("client"/"flask_app"/
"app_mock_db"/"auth_headers") report `fixture not found` and every management test ERROR
(the missing `tests/unit/__init__.py` dual-conftest-import trap). Run `tests/unit/management/`
as its own directory. Full suite there = 1821 passed, 2 skipped (~150s), `--cov-fail-under=0`
(subset can't hit the 90% floor).

**pre-commit `ruff format` aborts the commit if it reformats** — `ruff check` (lint) does
NOT flag formatting, so a now-fitting collapsed call passes `ruff check` yet fails
`ruff format`. Run `.venv/bin/ruff format <files>` before `git commit`. The pre-commit
"[INFO] Stashing unstaged files" is the framework's own mechanism, not a `git stash` you ran.

**mypy (empty baseline) flags `make_mock_key(**self.OTHER)`** — `**dict[str,int]` into a
typed signature is `arg-type`/`call-arg`. Use explicit kwargs. Also: a backgrounded
mypy-gate reads files mid-run, so re-run it AFTER the last edit or it reports stale errors.

See [[wave2_authz_reconciliation]], [[waddleai_management_test_harness_gotchas]],
[[management_venv_and_requirements]], [[tests_encode_broken_behaviour]].
