---
name: proxy-auth-cache-hot-path-pr268
description: O7-a/O11 proxy auth hot-path fix (PR #268) — grpc-vs-http blocking scope, two key tables, time.monotonic test hang
metadata:
  type: project
---

PR #268 (fix/proxy-auth-hot-path -> release/v0.2.X, 2026-10-03) fixed release-audit-2026-10-02
O7-a (sync PyDAL+bcrypt+last_used write on the event loop) and O11 (no cache in front of
api-key auth), plus wired the dead `record_database_operation()` histogram (O1).

**grpc-vs-http blocking scope** — the real per-request blocker is `_api_key_verifier`
(`proxy/apps/proxy_server/main.py`), called by `OIDCAuthMiddleware` on every HTTP request
directly on the Hypercorn event loop. `grpc_identity_resolver`'s sync
`RBACManager.authenticate_api_key` call was deliberately left untouched: the gRPC servicer
runs on its own `grpc.server(futures.ThreadPoolExecutor(...))` thread (`run_grpc_in_thread`),
never the asyncio loop, so it was never actually blocking anything. Before editing any
"sync call blocks the event loop" finding here, check whether the call site is on the
Hypercorn coroutine path or a separate thread (grpc server, background worker) — audit
report line numbers can point at code that's already safe.

**Two parallel key tables** — `api_keys` (`wa-{key_id}-{secret}`, `RBACManager.authenticate_api_key`,
the proxy's own auth) vs `virtual_keys` (`wa-{secret}`, management's `keys.py` CRUD). No
management REST endpoint existed for `api_keys` before this PR (only
`RBACManager.create_api_key()`, Python-only, used by test fixtures, plus a one-time bootstrap
seed in `extensions.py`). Added `DELETE /api/v1/proxy-keys/{key_id}`
(`services/management/app/api/v1/proxy_keys.py`) to give it a revoke path that also
invalidates the proxy's Valkey auth-cache entry. Shared key-naming helper:
`shared.auth.rbac.auth_cache_key()`/`AUTH_CACHE_KEY_PREFIX` (lets management invalidate
without importing the proxy's app package). Read `redis_client` via
`services.management.app.extensions` module reference at call time, never a stale
`from ...extensions import redis_client` module-level binding (see `cache_configs.py`'s
existing latent bug for why that pattern silently no-ops).

**New route → `tests/unit/management/conftest.py`'s `ROUTE_MODULES` list** — guarded by
`test_route_registration_guard.py::test_route_modules_list_matches_modules_needing_db_patch`;
any new `services/management/app/api/v1/*.py` module that imports `db` at module level
must be added there or that guard test fails (caught this once, fixed by appending).

**`time.monotonic` monkeypatch + real asyncio awaits = silent hang, not a failure** —
patching the global stdlib `time.monotonic` in an async test that also does real
`asyncio.sleep()`/executor awaits freezes asyncio's own event-loop clock
(`BaseEventLoop.time()` reads the same `time.monotonic`), so every subsequent real sleep's
deadline is computed against a clock that never advances — the test hangs forever (looked
like a 120s backgrounded-command timeout with zero output, not a clean pytest failure).
Fix: patch only the target module's own `time` reference by dotted path
(`monkeypatch.setattr("proxy.apps.proxy_server.auth_cache.time", SimpleNamespace(monotonic=...))`),
never the global `time` module object, whenever the test body awaits real async work after
the patch.

**Coverage tool note** — `--cov=<dotted.module.path>` with multiple targets can crash
(core dump) in this repo's venv (consistent with [[penguincode_coverage_gate_numpy_cov_crash]]);
use file-path `--cov=path/to/file.py` instead, works reliably with `--cov-report=term-missing`.
