---
name: management-observability-and-audit-gotchas
description: Wiring OTel (traces/metrics/logs) + an audit-log after_request middleware into services/management; penguin_dal auto-commit, OTel test-provider reset, hand-rolled ASGI/SQLAlchemy instrumentation, and the local-venv/mypy divergence discovered on fix/mgmt-observability-audit (release-audit-2026-09-23).
metadata:
  type: project
---

Discovered 2026-09-23 wiring OTel + admin-audit into `services/management` (release-audit O1 + G10).

**1. `penguin_dal.TableProxy.insert()` commits its own session; `DB.commit()` is a documented no-op.**
`insert()` opens `with self._session_factory() as session: session.execute(...); session.commit()`.
So a write middleware (e.g. the audit `after_request`) must NOT call `db.commit()` — the row
already persisted. Calling it trips `app_mock_db.commit.assert_not_called()` in every
read-only-but-POST test (the two `TestRoutingDryRun::*_writes_nothing` tests in
`test_routing_admin_api.py` assert exactly this). Insert into your own table only; never commit.

**2. Audit identity comes from `g.user`, a dict with `user_id`/`organization_id` on BOTH auth paths.**
`auth.py`'s JWT path sets `g.user = _decode_token(...)` (keys: user_id, username, role,
organization_id, scope, jti, exp) and the API-key path returns the same shape (+key_id). So
`g.user.get("user_id")` / `.get("organization_id")` work uniformly. Log `user_id` (int FK to
`users.id`), NEVER `username` — that is the PII boundary. Read `g` via `getattr(g, "user", None)`.

**3. The management ASGI entrypoint is `services/management/asgi.py` (`hypercorn asgi:app`), which
calls `app.create_app()`.** There is no `app/wsgi.py`. Wire OTel/audit inside `create_app()` (after
`init_extensions`, so `extensions.db.engine` exists) — never in a separate wsgi module.

**4. No `opentelemetry-instrumentation-asgi`/`-sqlalchemy` packages are pinned — only the core
api/sdk/otlp-grpc (1.44.0).** Hand-roll: an ASGI middleware class for request SERVER spans + RED
metrics, and `sqlalchemy.event.listen(engine, "before/after_cursor_execute", ...)` for DB spans.
penguin_dal runs EVERY runtime query through `db.engine` (a real SQLAlchemy Engine — confirmed:
`readyz`/`metrics` already do `db.engine.connect()`), so Core cursor listeners span all runtime DB
work. Guard with `isinstance(engine, sqlalchemy.engine.Engine)` so a MagicMock test DB is skipped.

**5. RED metric attributes must be bounded — `http.request.method` + `http.response.status_code`
only.** Never the raw path or user id as a metric label (the audit's own O1 cardinality finding is
`waddleai_tokens_total` labelled by raw user_id). Raw path is fine as a *span* attribute (spans
aren't cardinality-bounded), not as a metric label.

**6. Re-setting the global OTel MeterProvider in tests needs the run-once latch reset, not just the
provider ref.** `otel_metrics._internal._METER_PROVIDER = None` is NOT enough
(`test_pii_telemetry.py`'s pattern silently no-ops on the 2nd fixture use — "Overriding of current
MeterProvider is not allowed"). Also do
`otel_metrics._internal._METER_PROVIDER_SET_ONCE = Once()` (import `from opentelemetry.util._once
import Once`). **BUT you MUST save+restore both `_METER_PROVIDER` and `_METER_PROVIDER_SET_ONCE`
around the fixture (try/finally) — resetting-without-restoring LEAKS the dead in-memory provider
into other test modules.** This broke `tests/unit/security/test_pii_telemetry.py` (a CI failure):
"management" sorts before "security", so the management obs test ran first, left the latch tripped,
and pii's own fixture (which only nulls `_METER_PROVIDER`, never the latch) could no longer install
its reader → `waddleai.pii.detected` recorded zero points. For traces/logs, hand the code an
explicit `TracerProvider().get_tracer(...)` / `LoggerProvider` instead of touching globals. The
management app factory itself never sets a global provider without `OTEL_EXPORTER_OTLP_ENDPOINT`, so
this is a test-only hazard. In-memory sinks:
`opentelemetry.sdk.trace.export.in_memory_span_exporter.InMemorySpanExporter`,
`opentelemetry.sdk.metrics.export.InMemoryMetricReader`,
`opentelemetry.sdk._logs.export.InMemoryLogExporter`.

**7. ruff `B010` (setattr-with-constant) vs mypy `attr-defined` tension.** Stashing a provider on an
`InMemorySpanExporter` in a fixture: `setattr(exporter, "_provider", p)` — ruff's pre-commit hook
autofixes it back to `exporter._provider = p` (reintroducing the mypy error). Resolve by annotating
the local `exporter: Any = InMemorySpanExporter()` — an Any target satisfies both.

**8. `app.asgi_app = Middleware(app.asgi_app)` on a `Quart`-typed variable needs
`# type: ignore[method-assign,assignment]`.** The proxy avoids the error only because its `app` is
untyped there. `warn_unused_ignores` is off in `[tool.mypy]`, so the ignore is harmless where CI
doesn't hit it.

**9. The single-alembic-head guard test lives in `test_migration_019.py`
(`test_single_alembic_head_no_divergent_branches`), not in a dedicated file** — it pins
`heads[0]`. Bump it in the same commit that adds a migration (I moved it 020 -> 021).

**10. This sandbox's `--system-site-packages` venv diverges from the CI mypy baseline.** The base
uv python is missing `quart_cors`, `penguin_aaa`, `orjson`, `tiktoken` (install service reqs, not
root — see [[management_venv_and_requirements]]). `mypy-gate.sh` (empty baseline, so any error
fails) reports ~32 errors in 4 grpc files (`grpc_server.py`, `management/app/grpc/client.py`,
`test_grpc_server.py`, `test_grpc_auth_interceptor.py`) here that CI's env doesn't — a grpcio-stub
version divergence. They don't import app code, so they're a fixed pre-existing set: assert YOUR
files add zero new errors by grepping the gate output for your paths, don't chase the 32.

**11. Changing ANY management route's docstring drifts `openapi/v1.yaml` and fails the
`openapi-lint` "regenerate + fail on drift" CI gate.** `scripts/generate_openapi_spec.py` boots the
app (mocked DB, per-run `CREDENTIAL_ENCRYPTION_KEY`) and derives each path's `summary`/`description`
from the route docstring. After touching a route (I rewrote `/metrics`), run
`.venv/bin/python scripts/generate_openapi_spec.py --output openapi/v1.yaml`, confirm `git diff`
shows ONLY your route, and commit the regenerated spec. Gate is `spectral lint --fail-severity=error`
(warnings don't fail; the tree carries ~327 pre-existing warnings). `/metrics`/`/readyz`/`/livez` are
in the full committed spec — the generator always emits the full doc.

See also: [[management_venv_and_requirements]], [[mcp_sdk_and_alembic_gotchas]],
[[smoke_test_fix_and_orjson_gap]].
