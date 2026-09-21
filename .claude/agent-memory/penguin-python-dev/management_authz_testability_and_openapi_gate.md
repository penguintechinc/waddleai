---
name: management-authz-testability-and-openapi-gate
description: A SQL-only tenant filter is untestable in tests/unit/management/ because the mocked DB ignores the query — isolation tests pass identically with the fix reverted unless a Python-side guard exists
metadata:
  type: project
---

## A SQL-only tenant filter is UNTESTABLE in `tests/unit/management/`

`tests/unit/management/conftest.py`'s `_make_mock_db()` ignores the query
entirely: `db(<anything>).select()` returns whatever the test set on
`app_mock_db.return_value.select.return_value`. So a handler enforcing
tenant isolation purely inside the `select()` query — the house idiom, see
`model_access_policies.py:_visible_query` — has **no observable behaviour
difference** under the mock between "filter present" and "filter deleted".

Consequence: a route test asserting cross-tenant isolation against a
SQL-only filter passes identically before and after the fix. That is
coverage theatre. `test_model_access_policies_routes.py::test_list_resource_manager_scoped_query`
is exactly this — its own docstring only claims it "exercises the branch",
and it proves no isolation property whatsoever.

**Pattern that works:** keep the SQL filter as the primary control, and add
a small Python-side predicate applied to the fetched row(s) immediately
before serializing or acting on them. It is genuine defence-in-depth (it
survives the query being widened later, which is the actual regression
shape seen in the 2026-09-14 audit) *and* it is the only thing the
mocked-DB harness can observe. Implemented as
`cache_configs._row_visible_to` and `memory_scoping._belongs_to_org`.

**Always confirm a new isolation test fails with the fix reverted.** Revert
with `git checkout -- <file>` (never `git stash` — it is forbidden in these
worktrees), re-run, restore from a scratch copy, then `md5sum -c` to prove
the restore was byte-exact. Watch for a revert *script* that aborts on a bad
assertion: the file stays fixed and the tests then "pass", which reads as a
successful pre-fix check but proves nothing.

## Two more mocked-DB traps found closing the key-scope write hole

**`_DBTable` mocks are cached per table name for the module-scoped app**, and
their `.insert`/`.update`/`.delete` MagicMocks are NOT children of `mock_db`,
so the `app_mock_db` fixture's `reset_mock()` does not clear them. Call counts
leak across tests in a file. Call `app_mock_db.<table>.insert.reset_mock()`
explicitly before any `assert_not_called()`. (`app_mock_db.return_value.*` IS
fresh each test — the fixture rebuilds that mock_query.)

**A `select.side_effect = [...]` sequence silently couples a test to the exact
number of DB calls the handler makes.** Adding an authorization lookup shifts
every later entry, so reverting the fix changes the status code for
*sequencing* reasons, not security ones — e.g. a cross-tenant POST returned
409 (the key row got consumed by the conflict check) instead of the 201 you
would expect from "authorization bypassed". Still a failure, but a muddy one.
Prefer `side_effect = None` + `return_value = make_select_result([])` so the
outcome is order-independent, and assert the *write* (`insert.assert_not_called()`)
**before** the status code — the write is the real security property and gives
an unambiguous pre-fix failure ("Called 1 times").

## Fresh-venv recipe for the management tests

Root `requirements.txt` has neither quart nor pytest; install
`services/management/requirements.txt` as a second `--require-hashes` pass.
`ruff` is in neither (CI pins `ruff==0.16.2` inline in the workflow);
`bandit` likewise. Full `make test-unit` ≈14 min — 3751 passed, 10 skipped,
3761 collected (floor 3400), coverage 92.09% vs a 90% floor.

For the CI-gated `openapi/v1.yaml` drift trap and the empty
`mypy-baseline.txt`, see [[management-route-gates-and-test-traps]]. Related:
[[waddleai-management-test-harness-gotchas]], [[e2e-suite-and-penguin-dal-gotchas]].
