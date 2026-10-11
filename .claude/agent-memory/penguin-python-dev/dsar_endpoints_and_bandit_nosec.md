---
name: dsar-endpoints-and-bandit-nosec
description: DSAR/self-service management endpoints must be @require_auth-only (in-handler scope) to stay out of the 113-count scope test; bandit # nosec parses trailing prose as test IDs
metadata:
  type: project
---

Two traps hit implementing the DSAR right-of-access/erasure endpoints in
`services/management/app/api/v1/users.py` (+ new `services/management/app/dsar.py`),
release-audit-2026-09-23 G2.

## Self-service endpoints are `@require_auth` only, NOT `@require_scope`

A statutory self-service action (a plain `Role.USER` erasing/exporting THEIR OWN
record) cannot carry a `@require_scope(...)` decorator — the plain-user bundle
holds none of USER_MANAGE/USER_CREATE/USER_DELETE, so a scope decorator would
403 the owner. Do ownership+org-admin in-handler via the existing `_has_scope`
helper (mirror `get_user`), returning None-or-403 from a shared gate.

Consequence that keeps the suite green: `tests/unit/management/test_scope_authz.py`
`_iter_scoped_routes` only yields routes that carry `_required_scopes` (set by
`require_scope`), and asserts `_EXPECTED_ROUTE_COUNT == 113`. An auth-only route
has no `_required_scopes`, so it is NOT counted — adding export/erase left the
113 untouched. (Same reason `get_user` never appears there.)

**Why:** self-service DSAR must work at Free tier; a scope gate would paywall a
statutory right AND break the audited route count. **How to apply:** any new
"act on your own resource, or an org-admin acts for the org" endpoint → auth-only
+ in-handler `_has_scope`, never a `@require_scope` decorator.

## Erasure = anonymize-in-place, never hard-delete

Per PII-tokenization, PII lives only in `users`; `api_keys`/`token_usage`/
`usage_logs`/`security_logs` FK to `users.id` (verified via grep of
models_sqlalchemy.py). Hard delete breaks those FKs. Anonymize: unique-preserving
tombstone username/email (embed the id; `.invalid` TLD), invalidated
password_hash sentinel, null login IPs, `enabled=False`. NO schema migration
needed (no `erased` column added — kept ownership to users.py + dsar.py + tests).
Centralize the PII column set + anonymizer + Art.15 manifest in `dsar.py` so it's
DB-free unit-testable and a single source of truth.

## `# nosec BXXX` parses trailing prose as extra test IDs

`ERASED_PASSWORD_SENTINEL = "!erased"` trips bandit B105 (hardcoded_password) —
Low sev, so `bandit -ll` (the pre-commit gate) does not fail on it, but a bare
`-r` run flags it. Adding `# nosec B105 -- sentinel, not a credential` SILENCED
the finding but emitted `WARNING Test in comment: sentinel/not/a/credential is
not a test name` for every trailing word — bandit treats everything after
`# nosec` as a whitespace/comma list of test IDs. Fix: keep the suppression bare
(`# noqa: S105 # nosec B105`) and put the human rationale on a separate comment
line ABOVE. Same class as [[mcp_sdk_and_alembic_gotchas]]'s S105/S106-on-
`*token*`-named-vars note.

Related: [[management_route_gates_and_test_traps]] (113-count, mypy empty
baseline, openapi drift), [[quart_schema_validation_behaviors]]
(@validate_response nested-dict returns), [[wave2_scope_conversion_and_validate_response_traps]].
