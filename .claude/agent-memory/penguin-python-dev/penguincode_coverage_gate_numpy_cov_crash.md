---
name: penguincode-coverage-gate-numpy-cov-crash
description: pytest-cov narrow-dotted-module + --cov-branch crashes on numpy; use broad --cov target; migrate.py needs live pgvector; unreachable-guard test technique
metadata:
  type: feedback
---

**penguincode coverage-gate measurement (worktree `.worktrees/cov`, services/penguincode).**

Narrow dotted `--cov=` targets (e.g. `--cov=penguincode_cli.server.services.lessons --cov=penguincode_cli.db.migrate`) combined with `--cov-branch` crash the whole pytest run with `ImportError: cannot load module more than once per process` inside `numpy/_core/multiarray` (triggered via `tests/conftest.py`'s full `rest_app` import pulling in mem0 -> qdrant_client -> numpy). Setting `COVERAGE_CORE=ctrace` just swaps it for a `_ModuleLock` `_DeadlockError` instead — also broken.

**Why:** reproducible on a minimal one-line `import penguincode_cli.server.rest_app` test — it's the narrow dotted `--cov=` target shape itself, not test content. A broad `--cov=penguincode_cli` target (matching CI's actual `.github/workflows/ci.yml` invocation: `pytest tests/ -v --cov=penguincode_cli --cov-report=term-missing`) does not crash.

**How to apply:** always measure with `--cov=penguincode_cli --cov-branch --cov-report=term-missing` and grep the specific file's row out of the report — never pass dotted per-module `--cov=` targets with `--cov-branch` in this repo/venv.

Separately: `penguincode_cli/db/migrate.py`'s `run_migrations` body is only covered with a live Postgres — `TEST_DATABASE_URL` unset leaves it at ~49%, not the ~88% baseline a task spec may assume (CI sets `TEST_DATABASE_URL` via a Postgres service container). Spin an ephemeral `pgvector/pgvector:pg17` container (image is usually already cached locally: `docker images | grep pgvector`) on a free port before trusting any coverage baseline for this file.

**Unreachable-guard coverage technique (`lessons.py` and similar grpc servicers):** every `await context.abort(...)` call in this codebase is followed by `raise AssertionError("unreachable")` — not dead code (needed for `mypy --strict` narrowing), but normally unreachable since a real `grpc.aio.ServicerContext.abort()` always raises. To cover these lines, build a `_NonRaisingContext` double whose `abort()` records the call and returns normally (doesn't raise); calling the RPC with it makes execution fall through to the guard line, and the test asserts `pytest.raises(AssertionError, match="unreachable")`.

**`if __name__ == "__main__":` coverage:** `runpy.run_module("penguincode_cli.db.migrate", run_name="__main__")` (with `sys.argv`/`PGVECTOR_URL` set appropriately) executes the module as a script and covers the guard line + `main()` exit path — `main()`-as-a-function tests alone can't reach the `__main__` guard itself.

**mypy --strict gotcha on this file:** the venv has no `types-grpcio` installed, so bare `mypy --strict` fails on `import grpc` with "Library stubs not installed" — this is pre-existing (fails on the production `lessons.py` too, not something new tests introduce). CI's real invocation is `mypy penguincode_cli/ --ignore-missing-imports` (no `--strict`, doesn't even scan `tests/`). Verify new test files against that real command, not bare `--strict`, and don't add `# type: ignore[arg-type]` for passing a non-conforming grpc context double — under `--ignore-missing-imports`, grpc types resolve to `Any` so the ignore is flagged as unused (`warn_unused_ignores = true` is on).

See also [[management_observability_and_audit_gotchas]] for a similar penguin_dal auto-commit test-integrity pattern, and [[t13_memory_graph_extractor]] for the same ephemeral-pgvector-on-a-free-port recipe used elsewhere in penguincode.
