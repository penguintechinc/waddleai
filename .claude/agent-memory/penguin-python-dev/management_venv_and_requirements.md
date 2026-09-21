---
name: management-venv-and-requirements
description: Management-service tests need services/management/requirements.txt (root requirements.txt lacks quart-cors); pytest-cov is in no requirements file but pytest.ini addopts require it.
metadata:
  type: project
---

Setting up a venv for `tests/unit/management/`:

```bash
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python --require-hashes -r services/management/requirements.txt
uv pip install --python .venv/bin/python pytest pytest-asyncio pytest-cov ruff mypy bandit
```

- The **root** `requirements.txt` is not enough — it omits `quart-cors`, and
  `services/management/app/__init__.py` imports it at module level. The failure
  surfaces as a misleading `AttributeError: module 'services.management' has no
  attribute 'app'` from `unittest.mock.patch`, not as an ImportError.
- `pytest-cov` appears in **no** requirements file, yet `pytest.ini` `addopts`
  hardcodes `--cov=...`, so a pinned-only install cannot run any test
  (`unrecognized arguments: --cov=shared`). `make venv` installs it separately.
- Coverage invocation: `--cov=services/management/app --cov-config=.coveragerc`.
  A **dotted** module path (`--cov=services.management.app.api.v1.auth`) silently
  collects nothing and reports "No data was collected" — a zero denominator that
  reads as a pass. Use the slash path.
- Module-scoped `flask_app` fixture setup costs ~20-65s; individual tests are fast.

**Why:** a fresh worktree venv built from the documented `requirements.txt` alone
cannot collect a single management test. **How to apply:** install the
service-level requirements file, not the repo root one.
