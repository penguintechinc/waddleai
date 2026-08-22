# mypy baseline — 2026-08-22

## Why mypy could not complete a run

`make lint` invoked `mypy proxy services shared scripts tests --ignore-missing-imports`
and mypy aborted after 1 file with a hard config error, not a type error — the
run never reached the rest of the tree, so `|| true` was silently discarding a
gate that had never once produced a real error count.

Two distinct module-path collisions, found and fixed in order:

1. **Duplicate module name**: `services/penguincode/server/app.py` vs
   `services/management/app/__init__.py` both resolve to the top-level module
   `app`. `services/penguincode` is vendored upstream code with its own
   `pyproject.toml`/build system, already excluded from ruff
   (`extend-exclude`) and bandit (`--exclude services/penguincode`) for the
   same reason — not maintained here, own module namespace. Fix: add the same
   exclusion to `[tool.mypy] exclude` in `pyproject.toml` rather than renaming
   either `app` module.
2. **Source file found twice under different module names**: once
   `services/penguincode` was excluded, `proxy/apps/proxy_server/pipeline/stages.py`
   resolved to `proxy_server.pipeline.stages` by mypy's default walk-up
   algorithm (stops at `proxy/apps/`, which has no `__init__.py`) and to
   `proxy.apps.proxy_server.pipeline.stages` when reached via an import
   statement instead of a direct path scan. Fix: `explicit_package_bases = true`
   in `[tool.mypy]`, which resolves every module name relative to the
   invocation cwd (mypy's default `mypy_path`) consistently for both direct
   scans and import-following, instead of guessing package roots per file.
   `namespace_packages` is already `True` by default on mypy 2.3.1, so no
   change was needed there.

Both are config fixes in `pyproject.toml`'s `[tool.mypy]` table — no source
file was renamed or restructured to make mypy happy.

## Invocation

```bash
.venv/bin/python -m mypy proxy services shared scripts tests --ignore-missing-imports
```

Config is picked up automatically from `pyproject.toml` (mypy looks for
config files in the invocation cwd, which is the repo root for `make lint`).

## Baseline (2026-08-22, HEAD 89fa3186 + this PR's config changes)

```
Found 688 errors in 98 files (checked 434 source files)
```

688 raw error lines confirmed against `grep -cE ': error:'` on the full
`--show-error-codes` output — the summary count and the line count agree.

### By error code

| Code | Count |
|---|---|
| arg-type | 157 |
| union-attr | 148 |
| attr-defined | 87 |
| misc | 85 |
| assignment | 60 |
| valid-type | 49 |
| operator | 25 |
| annotation-unchecked | 20 |
| index | 19 |
| list-item | 17 |
| return-value | 13 |
| override | 7 |
| var-annotated | 6 |
| call-overload | 5 |
| call-arg | 3 |
| method-assign | 2 |
| dict-item | 2 |
| return | 1 |
| no-redef | 1 |
| has-type | 1 |

### By top-level package

| Package | Count |
|---|---|
| tests | 236 |
| services | 232 |
| shared | 195 |
| proxy | 25 |

### Top 10 files by error count

| File | Errors |
|---|---|
| services/management/app/models_sqlalchemy.py | 94 |
| shared/utils/rag_integration.py | 56 |
| shared/utils/llm_connectors.py | 56 |
| tests/unit/proxy/test_grpc_server.py | 31 |
| tests/unit/fleet/test_placement.py | 23 |
| services/management/app/api/v1/memory_scoping.py | 22 |
| tests/unit/test_token_manager.py | 19 |
| shared/utils/memory_integration.py | 19 |
| proxy/apps/proxy_server/main.py | 19 |
| services/management/app/services/coderag_worker.py | 18 |

## Gating shape adopted (this PR)

`|| true` replaced with `scripts/mypy-gate.sh`, called from `make lint`. The
gate re-runs mypy, diffs its `error:` lines against a committed
`mypy-baseline.txt` (688 lines, generated the same way — see the script's
regenerate command), and fails if any current error line is not present in
the baseline. It also fails outright if mypy examines zero source files
(config regression silently produces a false pass otherwise). Baseline
entries keep the line number — an unrelated edit that shifts a downstream
error's line number reads as "new" and forces a baseline regen; this is a
deliberate simplicity tradeoff over fuzzy (message-only or per-file/code)
matching, which would hide real regressions as often as it saves busywork.

Proven both directions in this PR: a clean baseline run passes
(`make lint` exit 0), and a deliberate type error in a throwaway file under
`shared/` makes both `scripts/mypy-gate.sh` and `make lint` fail (exit 1 /
exit 2 respectively) before being reverted.

## Proposed ratchet (follow-up work, not this PR)

- Track baseline count over time; any PR that touches a file in the top-10
  list above should be asked to reduce that file's error count, not just
  avoid adding new ones (same spirit as the coverage ratchet 74% → 90%, see
  `docs/superpowers/plans/2026-08-21-coverage-90.md`).
- `union-attr` (148) and `attr-defined` (87) together are 34% of the total
  and are frequently mechanical (`Optional` narrowing, mock attribute
  access in tests) — a good first wedge for a follow-up cleanup PR.
- `tests/` carries 236 of 688 (34%) — most of it is fixture/mock typing
  (`Callable` signature mismatches, `None` defaults on typed params). Worth
  a dedicated `conftest.py`/fixture-typing pass rather than fixing file by
  file.
- CI does not run mypy at all today (`grep -n mypy .github/workflows/*.yml`
  is empty) — out of scope here since another PR this wave is actively
  editing `.github/workflows/docker-build.yml`; add a `make lint` (or a
  dedicated mypy) CI step once that PR lands.
