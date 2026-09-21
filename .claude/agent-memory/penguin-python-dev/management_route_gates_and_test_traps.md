---
name: management-route-gates-and-test-traps
description: WaddleAI management-route change gates — empty mypy-baseline, openapi/v1.yaml drift gate, dev tooling absent from requirements.txt, and existing tests that encode vulnerabilities as expected behaviour
metadata:
  type: project
---

Four things that bite when changing a `services/management/app/api/v1/*.py` route.

## The test suite can encode a vulnerability as intended behaviour

`tests/unit/management/test_quota_routes.py::TestSetKeyQuota::test_set_key_quota_regular_user_own_key`
asserted `200` for a plain `Role.USER` raising `rpm_limit` on their own key — i.e. it
*locked in* the privilege-escalation finding (audit-2026-09-14) as a passing test, docstring
"Regular user can set quota for own key". Fixing the route turned it red.

**Implication:** a green suite is not evidence a route is safe, and when a security fix makes an
existing test fail, read that test before assuming your fix is wrong. Grep sibling routes for the
same in-handler pattern — the ownership check
`if user_role not in ["admin"]: ... elif key.user_id != user_id: 403` proves *ownership only* and
is copy-pasted across several routes; it is not a permission check.

## `mypy-baseline.txt` is EMPTY — the tree is fully clean

`scripts/mypy-gate.sh` is a ratchet against a committed baseline, but the baseline is 0 lines
(458 source files, 0 errors). So **any** new type error fails the gate outright; there is no
slack.

Trap: mypy config is non-strict (`pyproject.toml [tool.mypy]` sets only `ignore_missing_imports`/
`explicit_package_bases`), so an *unannotated* function's body is unchecked. Adding annotations to
a previously-unannotated route makes mypy start checking its body — and `extensions.db` is typed
`DB | None`, so bare `db(...)` calls that were invisible become errors. Use the `_db()` narrowing
helper (`keys.py` has the canonical one; copy it rather than annotating around it). It reads the
module global at call time, so `conftest.py`'s `patch(f"{m}.db", mock_db)` still works.

## `openapi/v1.yaml` is a CI-gated generated artifact

Adding `@validate_request`/`@validate_response`/`@tag` to any route changes the generated spec, and
CI (`.github/workflows/docker-build.yml`) regenerates it and **fails on drift**. Regenerate with
`make generate-openapi` (or `python scripts/generate_openapi_spec.py`) and commit it. `make
openapi-lint` gates on `--fail-severity=error` only — 335 pre-existing *warnings* are expected and
do not fail.

**Parallel-agent hazard:** it is a single 117KB committed YAML, so two concurrent branches that
each add a schema will both touch it and conflict. Keep the regen in its own commit so the
conflict is trivially resolvable by re-running the generator.

## pytest-cov / ruff / bandit are NOT in requirements.txt

`requirements.txt` is hash-pinned and has pytest + pytest-asyncio but **not** pytest-cov, ruff, or
bandit — yet `pytest.ini`'s `addopts` hardcodes `--cov=...`, so a fresh venv fails every pytest
invocation with `unrecognized arguments: --cov=shared ...` before running a single test (and
`--no-cov` is itself an unrecognized argument without the plugin). Mirror CI's extra install:

```
uv pip install --require-hashes -r requirements.txt
uv pip install -r services/management/requirements.txt   # also required, separate file
uv pip install pytest-cov bandit 'ruff==0.16.2'          # ruff pinned to the pre-commit hook's version
```

Note `pytest -p no:randomly` is unnecessary here (pytest-randomly isn't installed) but harmless.
Route suites are slow: `test_key_routes.py` + `test_quota_routes.py` ≈ 2min cold, ~20s warm.

Related: [[waddleai_management_test_harness_gotchas]] (conftest fixture/keypair trap — use the
token *fixtures*, never a direct `make_token` import), [[e2e_suite_and_penguin_dal_gotchas]].
