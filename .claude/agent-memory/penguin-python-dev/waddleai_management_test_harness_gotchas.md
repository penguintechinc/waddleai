---
name: waddleai-management-test-harness-gotchas
description: WaddleAI management-service test harness landmines — dual-module conftest import via missing tests/unit/__init__.py, and WaddleAIMetrics singleton collision across get_proxy_metrics()/get_management_metrics()
metadata:
  type: project
---

Two landmines found while building `tests/unit/management/` route tests for a new blueprint (agent-hooks, migration 015).

## `tests/unit/__init__.py` is missing (but `tests/unit/management/__init__.py` exists)

`tests/__init__.py` and `tests/unit/management/__init__.py` exist; `tests/unit/__init__.py` does not. This means `from tests.unit.management.conftest import X` in a test file can resolve to a **second, independent copy** of `conftest.py` (namespace-package import ambiguity) distinct from the copy pytest's own conftest-autoloading mechanism uses for fixtures.

**Symptom:** calling `make_token(...)` via a direct import (not through a fixture) produces a JWT signed with a *different* RSA keypair than the one `services.management.app.api.v1.auth._get_oidc_provider` is patched to return (`flask_app` fixture in conftest.py) — because `make_token`'s internal `_test_oidc_provider()` call resolves via *its own module's* `@lru_cache`, which is a different cache instance in the duplicate module. `verify_token()` then silently returns `None` (broad `except Exception`), auth falls through to the API-key path, and you get a confusing `passlib` `TypeError: hash must be str or bytes, not MagicMock` several layers down — nothing about the traceback points at the real cause.

**Fix:** never call `make_token()` (or anything that transitively touches `_test_oidc_provider`) via a direct `from tests.unit.management.conftest import make_token` in a test file. Only use conftest.py's own **fixtures** (`admin_token`, `user_token`, `resource_manager_token`, `auth_headers`, etc.) — fixture injection always resolves through whichever copy of conftest.py pytest itself loaded, so it's internally consistent. If a test needs an identity the existing fixtures don't cover (e.g. a *second* org's `resource_manager`), add a new named fixture to conftest.py (it's on the append-only contended-file list, so this is expected) rather than constructing the token ad hoc in the test module. See `rm_org2_auth_headers` fixture for the pattern.

`make_select_result` and other non-token helpers are unaffected (no `lru_cache`/identity dependency), safe to import directly as the rest of the test suite already does.

## `WaddleAIMetrics` was a latent Prometheus registry collision

`shared/utils/metrics.py`'s `WaddleAIMetrics.__init__` used to construct fresh `Counter`/`Histogram`/`Gauge`/`Info` objects on every instantiation. `get_proxy_metrics()` and `get_management_metrics()` each memoize *their own* singleton, but since prometheus_client's default `CollectorRegistry` is process-global and metric identity is by *name* only (not by which `WaddleAIMetrics` instance created it), constructing both singletons in the same process (e.g. one test file exercises `get_proxy_metrics()`, another — for the first time ever — exercises `get_management_metrics()`) raises `ValueError: Duplicated timeseries in CollectorRegistry`. This was **latent since the class was written**: nothing in the test suite had ever called `get_management_metrics()` before, so it never fired. First code to do so (any new management-service feature that touches metrics) will trip it in a full-suite run even though it passes in isolation.

**Fixed** (this session): `WaddleAIMetrics` now builds its collectors once (Borg-style, class-level `_shared_collectors` dict) and every subsequent instantiation reuses them; `service_name` stays per-instance since it's a label value at record time, not part of collector identity. If this file gets touched again and someone "simplifies" it back to per-instance construction, the collision returns — silent until two singleton-getters are both exercised in one process.

Related: [[e2e_suite_and_penguin_dal_gotchas]], [[mcp_sdk_and_alembic_gotchas]] for other WaddleAI test-harness landmines.
