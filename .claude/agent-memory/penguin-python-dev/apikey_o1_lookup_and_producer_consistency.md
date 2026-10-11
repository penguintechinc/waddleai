---
name: apikey-o1-lookup-and-producer-consistency
description: The O(1) API-key auth fix (parse embedded key_id/prefix) requires ALL key producers to conform; two producers (extensions.py admin, proxy main.py contract key) did NOT, and the require_auth enabled-recheck must use db.table[pk] to avoid breaking 200 side_effect tests.
metadata:
  type: project
---

Release-audit-2026-09-23 fix/mgmt-auth-scale (M1/M3/O6). Three api-key formats
coexist and the "parse the embedded key_id" O(1) fix only works if the KEY VALUE
encodes its own lookup handle. Confirm all producers conform before shipping.

**Two api-key tables, two lookup handles:**
- `api_keys` (proxy path, `rbac.RBACManager.authenticate_api_key`): value
  `wa-{key_id}-{secret}`, `key_id` column (unique). key_id = `token_hex(8)` is
  dash-free, so `api_key.split("-")[1]` == key_id even though `secret`
  (token_urlsafe) CAN contain dashes. Fix: look up the one row by key_id.
- `virtual_keys` (management path, `auth.verify_api_key`): value `wa-{secret}`,
  NO embedded key_id. Narrow by `key_prefix = f"wa-{secret[:8]}..."` =
  `api_key[:11] + "..."` (helper `auth._virtual_key_prefix`).

**Producer consistency is the trap.** Conforming: `rbac.create_api_key`,
`keys.py`. NON-conforming (both broke silently under the O(1) fix):
- `services/management/app/extensions.py` admin bootstrap emitted a bare
  `wa-{secret}` with `key_prefix="wa-admin"` and `key_id="admin-key-{hex}"` —
  neither derivable from the value → admin could not auth to proxy OR mgmt. FIXED
  here: one `wa-{key_id}-{secret}` value, key_prefix + api_keys.key_id both
  derived from it. (extensions.py is app-level, NOT in the api/v1/proxy/__init__
  forbidden list — a justified 2nd cross-file touch beyond the sanctioned rbac.py.)
- `proxy/apps/proxy_server/main.py` `_TEST_API_KEY_VALUE =
  "wa-contract-test-0001-secretvalue"` with `key_id="contract-test-key"` →
  `split("-")[1]=="contract"` ≠ column. STILL BROKEN (proxy forbidden): breaks
  `tests/unit/proxy/test_proxy_server_main.py::{test_raw_api_key_path_returns_user_context,test_valid_api_key_returns_claims_dict}`
  + HTTP contract tests. Proxy-branch must make the embedded key_id dash-free and
  equal to the `key_id=` column.
- `tests/e2e/conftest.py::_seed_org` hand-rolled `wa-{slug}-e2esecretvalue0001`
  with `key_id="e2e-{slug}-key"` (and every caller slug had a dash: "pii-block",
  "cache-orgb") → O(1) lookup 401'd it (CI FAILURE, `test_security_pii_e2e`).
  FIXED: now mints via the REAL `RBACManager.create_api_key` path (can't drift
  again). Lesson: e2e/contract key FIXTURES must mint via `create_api_key`, never
  hand-roll `wa-...` + a separate `key_id=` column.

**Admin key_id is now DETERMINISTIC** (`extensions._ADMIN_MASTER_KEY_ID="adminkey"`,
not `token_hex`) — a non-secret lookup handle (secret stays random), so the
derived `key_prefix="wa-adminkey..."` is reproducible. Regenerated
`tests/contract/snapshots/mgmt_keys_list.json` via `CONTRACT_RECORD=1 pytest
tests/contract/test_management_contract.py::test_keys_list` (real mgmt hypercorn
subprocess DID boot here, ~13s; only `key_prefix` changed wa-admin→wa-adminkey...).
snapshot.py `_VOLATILE_KEYS` does NOT include `key_prefix` (compared literally) —
prefer a deterministic value over adding it to volatile.

**require_auth enabled-recheck without breaking the suite:** the JWT path never
re-checked `enabled` (M1). A `db(query).select().first()` recheck in require_auth
BREAKS ~215 tests — the conftest mock funnels every `db(query)` through one
`mock_query`, so it consumes the `.first.side_effect` LISTS ~200 tests configure.
`.count()` collides too (tests set count.return_value/side_effect). The ONLY
isolated path is penguin_dal's primary-key fetch **`db.users[user_id]`**
(TableProxy.__getitem__) — it resolves through `db.users` (a `_DBTable`), a
DIFFERENT mock branch than `db(query).select().first()`, so ZERO blast radius
(1879 passed). Needed a 2-line additive `__getitem__` (+ `__invert__` for
`orderby=~col`) on conftest `_DBTable`. Disable is soft (`delete_user` sets
enabled=False, never hard-deletes), so found-and-disabled → reject is the real
case; users.py owns the disable handler and was NOT touched.

See [[penguin_aaa_token_ttl_and_jti]] (jti denylist reuse for refresh/change-pw
revocation), [[management_route_gates_and_test_traps]], [[wave2_scope_conversion_and_validate_response_traps]] (mocked-DB-ignores-queries).
