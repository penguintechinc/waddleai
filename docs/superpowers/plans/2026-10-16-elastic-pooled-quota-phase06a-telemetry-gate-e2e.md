# Elastic Pooled Quota — Phase 6a: Telemetry Gate + Flag-On End-to-End

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §10 (Testing, E2E flag-on), house gates `testing.md` Telemetry Validation + Logging Library Conformance.

---

### Task 30: Telemetry-emission gate, logging conformance, and the shared integration "world"

`testing.md`: telemetry emission is verified every commit with counts reported; zero received is a FAIL, never a skip. This task adds that gate for the quota feature and the shared real-Valkey + real-schema fixture builder Task 31 reuses.

**Files:**
- Create: `tests/integration/quota/world.py`
- Create: `tests/integration/quota/test_quota_telemetry_emission.py`
- Create: `tests/unit/quota/test_logging_conformance.py`

**Interfaces:**
- Consumes: Tasks 12, 14, 21-26 (`QuotaConfigStore`, `quota_pools` service, `QuotaLimiter`, `QuotaLedger`, `QuotaEngine`, `QuotaTelemetry`); fixtures `team_world`, `valkey_async`.
- Produces (`tests/integration/quota/world.py`): `Clock(value: date)`; `FixedTM(credits: int = 4)` (TokenManager stand-in: `rates_stale() -> False`, `estimate_credits(model, i, o) -> credits`, `providers_for_model(model) -> frozenset({"openai"}) for "gpt-4o", else frozenset()`); `World` dataclass (`db, org: int, team: int, u: dict[str, int], engine, limiter, ledger, store, clock: Clock, tm: FixedTM, valkey`); `build_world(team_world, valkey, *, credits=4, tele=None, start=date(2026, 1, 15)) -> World` (org a: anchor 2026-01-01, pool 1000, enabled; team a1: pool 100, buffer 10; reservations mia=20, olive=30, adam none); `user_of(w, name) -> SimpleNamespace(user_id, organization_id, team_id)`; `MSG` (one short user message).

- [ ] **Step 1: Write the failing tests**

`tests/integration/quota/world.py`:

```python
"""Shared builder for the quota integration tests: real Valkey + real schema DB + real engine."""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

from services.management.app.services import quota_pools as qp
from shared.quota.config import QuotaConfigStore
from shared.quota.engine import QuotaEngine
from shared.quota.ledger import QuotaLedger
from shared.quota.lease import QuotaLimiter

MSG = [{"role": "user", "content": "hello quota"}]


@dataclass
class Clock:
    """A mutable 'today' so a test can cross a cycle boundary."""

    value: date


class FixedTM:
    """TokenManager stand-in: a fixed credit estimate and a single-provider model."""

    def __init__(self, credits: int = 4) -> None:
        self.credits = credits

    def rates_stale(self) -> bool:
        return False

    def estimate_credits(self, model: str, input_tokens: int, output_tokens: int) -> int:
        return self.credits

    def providers_for_model(self, model: str) -> frozenset[str]:
        return frozenset({"openai"}) if model == "gpt-4o" else frozenset()


@dataclass
class World:
    """Everything an end-to-end quota test needs."""

    db: Any
    org: int
    team: int
    u: dict[str, int]
    engine: QuotaEngine
    limiter: QuotaLimiter
    ledger: QuotaLedger
    store: QuotaConfigStore
    clock: Clock
    tm: FixedTM
    valkey: Any


def build_world(team_world, valkey, *, credits=4, tele=None, start=date(2026, 1, 15)) -> World:
    """Configure org a / team a1 through the real service layer and wire a real engine."""
    db, org, team_id = team_world.db, team_world.org["a"], team_world.t["a1"]
    qp.set_org_pool(db, org, cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True)
    team = db(db.teams.id == team_id).select().first()
    qp.set_team_pool(db, org, team, 100)
    qp.set_team_buffer(db, team, 10)
    qp.set_user_reservation(db, team, team_world.u["mia"], 20, None)
    qp.set_user_reservation(db, team, team_world.u["olive"], 30, None)
    limiter, store, ledger = QuotaLimiter(valkey), QuotaConfigStore(valkey, db), QuotaLedger(db)
    clock, tm = Clock(start), FixedTM(credits)
    engine = QuotaEngine(limiter, store, ledger, tm, tele, today=lambda: clock.value)
    return World(db, org, team_id, dict(team_world.u), engine, limiter, ledger, store, clock, tm, valkey)


def user_of(w: World, name: str) -> SimpleNamespace:
    """A UserContext-shaped identity for one team_world user in team a1."""
    return SimpleNamespace(user_id=w.u[name], organization_id=w.org, team_id=w.team)
```

`tests/integration/quota/test_quota_telemetry_emission.py`:

```python
"""Telemetry emission gate (testing.md): logs, metric points, histograms and spans must all be > 0."""

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shared.quota.telemetry import QuotaTelemetry
from tests.integration.quota.world import MSG, build_world, user_of


def _collect(reader):
    """name -> data points, across every scope/resource."""
    out = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                out[metric.name] = list(metric.data.data_points)
    return out


async def test_scenario_emits_logs_metrics_histograms_and_spans(team_world, valkey_async, caplog):
    """Allow x7, buffer leases, one 429, a reconcile and an orphan reconcile: every signal is received."""
    reader, exporter = InMemoryMetricReader(), InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tele = QuotaTelemetry(
        meter=MeterProvider(metric_readers=[reader]).get_meter("t"), tracer=tracer_provider.get_tracer("t")
    )
    w = build_world(team_world, valkey_async, credits=4, tele=tele)
    mia = user_of(w, "mia")
    with caplog.at_level("DEBUG", logger="shared.quota"):
        outcomes = [await w.engine.reserve(mia, "gpt-4o", MSG) for _ in range(8)]
        first = outcomes[0].reservation
        applied = await w.engine.reconcile(first, actual_credits=3, provider="openai", model="gpt-4o")
        orphan = await w.engine.reconcile(first, actual_credits=3, provider="openai", model="gpt-4o")
    assert [o.blocked for o in outcomes] == [False] * 7 + [True]  # 20/4=5 reservation + 2 buffer leases, 8th is a 429
    assert applied.applied is True and orphan.applied is False

    logs = [r for r in caplog.records if r.name.startswith("shared.quota")]
    points = _collect(reader)
    metric_points = sum(len(v) for v in points.values())
    histogram_points = sum(len(v) for k, v in points.items() if hasattr(v[0], "bucket_counts"))
    spans = exporter.get_finished_spans()
    summary = f"logs={len(logs)} metric_points={metric_points} histogram_points={histogram_points} spans={len(spans)}"
    print(f"quota telemetry emission: {summary}")
    assert len(logs) >= 1, summary
    assert metric_points >= 1, summary
    assert histogram_points >= 1, summary
    assert len(spans) >= 1, summary
    assert {p.attributes["outcome"] for p in points["waddleai.quota.decisions"]} >= {"allowed", "denied"}
    assert sum(p.value for p in points["waddleai.quota.rejections"]) == 1
    assert sum(p.value for p in points["waddleai.quota.buffer_leases"]) == 2
    assert {p.attributes["applied"] for p in points["waddleai.quota.reconciles"]} == {"true", "false"}
```

`tests/unit/quota/test_logging_conformance.py`:

```python
"""Logging-library conformance (testing.md): no hand-rolled print/basicConfig in quota service code."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FILES = sorted(
    [*ROOT.glob("shared/quota/*.py"),
     ROOT / "shared/utils/conversion_rate_store.py",
     ROOT / "shared/utils/default_conversion_rates.py",
     ROOT / "proxy/apps/proxy_server/quota_hooks.py",
     ROOT / "proxy/apps/proxy_server/quota_sweeper.py",
     ROOT / "proxy/apps/proxy_server/pipeline/quota_stage.py",
     ROOT / "services/management/app/services/quota_pools.py",
     ROOT / "services/management/app/services/quota_invalidation.py",
     ROOT / "services/management/app/api/v1/quota_pools.py",
     ROOT / "services/management/app/api/v1/team_quota.py"]
)  # fmt: skip


def _hand_rolled(tree: ast.AST) -> list[str]:
    """print(...) calls and logging.basicConfig(...) calls found in a module."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "print":
                found.append(f"print@{node.lineno}")
            if isinstance(fn, ast.Attribute) and fn.attr == "basicConfig":
                found.append(f"basicConfig@{node.lineno}")
    return found


def test_scanner_examined_the_files_and_found_no_hand_rolled_logging():
    """A scanner pointed at the wrong root reports clean, so the examined count is itself asserted."""
    assert len(FILES) >= 15, f"scanned only {len(FILES)} files"
    assert all(f.exists() for f in FILES), [str(f) for f in FILES if not f.exists()]
    offenders = {str(f.relative_to(ROOT)): hits for f in FILES if (hits := _hand_rolled(ast.parse(f.read_text())))}
    assert offenders == {}, offenders


def test_modules_that_log_use_the_module_logger():
    """Every quota module that logs does so through logging.getLogger(__name__)."""
    users = [f for f in FILES if "logger." in f.read_text()]
    assert len(users) >= 8, f"only {len(users)} quota modules log"
    assert all("logging.getLogger(__name__)" in f.read_text() for f in users)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make test-valkey` (the new telemetry test) and `python3 -m pytest tests/unit/quota/test_logging_conformance.py -v --no-cov`
Expected: `test_logging_conformance` PASSES immediately only if every listed file already exists and is clean — if any listed module name differs, FAIL listing the missing file; the telemetry test FAILS with `ModuleNotFoundError: tests.integration.quota.world` until Step 3 creates it. (Create `world.py` first, then re-run to see the telemetry assertions run.)

- [ ] **Step 3: Write minimal implementation**

No production code changes are expected: the gate asserts behaviour Tasks 21-26 already implement. If a count assertion fails, fix the producer (e.g. a missing `logger.info`/`debug` in `QuotaEngine`, a missing instrument call) — never weaken the assertion. The engine already logs at INFO on deny, DEBUG on no-op, WARNING on orphan reconcile and overage.

- [ ] **Step 4: Run tests to verify they pass**

Run: `make test-valkey && python3 -m pytest tests/unit/quota/test_logging_conformance.py -v --no-cov`
Expected: PASS; the telemetry test prints `quota telemetry emission: logs=N metric_points=N histogram_points=N spans=N` with every count > 0.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/quota/world.py tests/integration/quota/test_quota_telemetry_emission.py tests/unit/quota/test_logging_conformance.py
git commit -m "test(quota): telemetry-emission gate, logging conformance scan, shared integration world"
git push
```

---

### Task 31: Flag-on end-to-end (spec §10) against real Valkey + real schema

**Files:**
- Create: `tests/integration/quota/test_pooled_quota_e2e.py`

**Interfaces:**
- Consumes: `build_world`, `user_of`, `MSG`, `World` (Task 30); `QuotaStage` (Task 27); `reconcile_quota` (Task 28); `ProxyPipeline`, `PipelineContext`, `Stage`; `quota_pools` service; `keys`.
- Produces: the executable form of the spec's E2E bullet: exhaust reservation -> auto-lease -> buffer empty -> 429; a second user's reservation untouched; cycle reset restores capacity; an override blocks after reconcile; plus fail-open, no-op, and release-on-block with real counters.

- [ ] **Step 1: Write the failing test**

`tests/integration/quota/test_pooled_quota_e2e.py`:

```python
"""Spec §10 flag-on E2E: real Valkey, real schema DB, real engine/stage/pipeline."""

from datetime import date

import redis.asyncio as aredis

from proxy.apps.proxy_server.pipeline.quota_stage import QuotaStage
from proxy.apps.proxy_server.pipeline.stages import PipelineContext, ProxyPipeline, Stage
from proxy.apps.proxy_server.quota_hooks import reconcile_quota
from services.management.app.services import quota_pools as qp
from shared.quota import keys
from shared.quota.engine import QuotaEngine
from shared.quota.lease import QuotaLimiter
from tests.integration.quota.world import MSG, build_world, user_of


async def _n(client, key):
    """Integer value of a counter (0 when absent)."""
    return int(await client.get(key) or 0)


async def _burn(w, name, count):
    """``count`` sequential reserves for ``name``; returns the outcomes."""
    return [await w.engine.reserve(user_of(w, name), "gpt-4o", MSG) for _ in range(count)]


async def test_exhaust_reservation_lease_buffer_then_429_and_other_reservations_survive(team_world, valkey_async):
    """mia: 5 x 4 from her 20 guarantee, 2 x 4 leased from the 10 buffer, the 8th is blocked; olive/adam as spec'd."""
    w = build_world(team_world, valkey_async, credits=4)
    outcomes = await _burn(w, "mia", 8)
    assert [o.reservation.from_buffer if o.reservation else None for o in outcomes] == [0] * 5 + [4, 4, None]
    assert outcomes[7].blocked and outcomes[7].reason == "quota_exhausted"
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 20
    assert await _n(valkey_async, keys.buffer_counter(w.team, 0)) == 8
    olive = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)  # buffer is nearly empty, her guarantee is not
    assert olive.blocked is False and (olive.reservation.from_reservation, olive.reservation.from_buffer) == (4, 0)
    adam = await w.engine.reserve(user_of(w, "adam"), "gpt-4o", MSG)  # no reservation: buffer only, 8+4 > 10
    assert adam.blocked is True


async def test_reconcile_refunds_and_the_ledger_records_the_final_split(team_world, valkey_async):
    """Actual 1 < reserved 4: the counter drops to 1 and the durable ledger shows mia: 1 in cycle 0."""
    w = build_world(team_world, valkey_async, credits=4)
    out = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    await w.engine.reconcile(out.reservation, actual_credits=1, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 1
    usage = {(e.scope_type, e.scope_id): e.credits for e in w.ledger.usage(w.org, 0)}
    assert usage == {("user", w.u["mia"]): 1}


async def test_cycle_boundary_restores_capacity_and_leaves_the_old_cycle_alone(team_world, valkey_async):
    """Day 30 starts cycle 1: mia is allowed again from a fresh reservation; cycle-0 counters are untouched."""
    w = build_world(team_world, valkey_async, credits=4)
    await _burn(w, "mia", 8)
    assert (await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)).blocked is True
    w.clock.value = date(2026, 1, 31)  # (Jan 31 - Jan 1) = 30 days -> cycle 1
    fresh = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert fresh.blocked is False and fresh.reservation.cycle == 1 and fresh.reservation.from_buffer == 0
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 20
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 1)) == 4


async def test_reconcile_after_rollover_still_charges_the_reservations_cycle(team_world, valkey_async):
    """Review-focus 5: reserved in cycle 0, clock moves to cycle 1, reconcile still adjusts cycle 0."""
    w = build_world(team_world, valkey_async, credits=4)
    out = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)
    w.clock.value = date(2026, 1, 31)
    await w.engine.reconcile(out.reservation, actual_credits=2, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["olive"], 0)) == 2
    assert await valkey_async.get(keys.user_counter(w.u["olive"], 1)) is None
    assert [(e.scope_id, e.credits) for e in w.ledger.usage(w.org, 0)] == [(w.u["olive"], 2)]


async def test_provider_override_blocks_the_next_request_after_reconcile(team_world, valkey_async):
    """Override cap 6 on openai: admitted, reconciled at 7 (over cap, recorded), the NEXT request is refused."""
    w = build_world(team_world, valkey_async, credits=4)
    first = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)  # primes the config cache
    qp.create_override(w.db, w.org, user_id=None, model=None, provider="openai", cap_credits=6)
    await valkey_async.delete(keys.org_config(w.org))  # what invalidate_quota_config does after the edit
    result = await w.engine.reconcile(first.reservation, actual_credits=7, provider="openai", model="gpt-4o")
    assert result.over_cap_override_ids and result.applied
    nxt = await w.engine.reserve(user_of(w, "olive"), "gpt-4o", MSG)
    assert (nxt.blocked, nxt.reason) == (True, "override_exceeded")
    assert await _n(valkey_async, keys.user_counter(w.u["olive"], 0)) == 7  # the denial mutated nothing


async def test_valkey_outage_fails_open_without_a_reservation(team_world, valkey_async):
    """D10: the limiter pointed at a dead Valkey allows the request and holds nothing."""
    w = build_world(team_world, valkey_async, credits=4)
    dead = aredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    engine = QuotaEngine(QuotaLimiter(dead), w.store, w.ledger, w.tm, None, today=lambda: w.clock.value)
    out = await engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    await dead.aclose()


async def test_unconfigured_org_writes_nothing_to_valkey(team_world, valkey_async):
    """Disabled org: reserve is a strict no-op -- no counter, reservation or index key is created."""
    w = build_world(team_world, valkey_async, credits=4)
    qp.set_org_pool(w.db, w.org, cycle_anchor_date=None, pool_credits=None, enabled=False)
    await valkey_async.delete(keys.org_config(w.org))
    out = await w.engine.reserve(user_of(w, "mia"), "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:user:*")] == []
    assert [k async for k in valkey_async.scan_iter(match="wa:quota:resv*")] == []


class _Features:
    """Flag helper stub: every flag on."""

    def is_feature_enabled(self, flag, distinct_id=None, **kw):
        return True


class _Blocker(Stage):
    """A later stage that blocks (stands in for security_out)."""

    async def __call__(self, ctx):
        ctx.blocked, ctx.status_code, ctx.block_reason = True, 400, "blocked downstream"
        return ctx


def _ctx(w, name):
    """A pipeline context for ``name`` asking for gpt-4o."""
    return PipelineContext(user=user_of(w, name), body={}, model="gpt-4o", messages=MSG)


async def test_stage_blocks_with_429_and_a_downstream_block_refunds_the_reservation(team_world, valkey_async):
    """Review-focus 4 with real counters: admit, then a later block refunds; exhaustion answers 429."""
    w = build_world(team_world, valkey_async, credits=4)
    stage = QuotaStage("quota", w.engine, _Features())
    ctx = await ProxyPipeline([stage, _Blocker("sec", None)], None).run(_ctx(w, "mia"))
    assert ctx.blocked and ctx.block_reason == "blocked downstream"
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 0  # refunded, not leaked
    await _burn(w, "mia", 7)  # 5 from the guarantee + 2 buffer leases
    denied = await ProxyPipeline([stage], None).run(_ctx(w, "mia"))
    assert (denied.blocked, denied.status_code) == (True, 429)
    assert denied.block_reason == "Token quota exhausted for the current billing cycle"


async def test_finalize_hook_settles_the_reservation_exactly_once(team_world, valkey_async):
    """reconcile_quota adjusts to the actual credits and clears the hooks; a second call is a no-op."""
    w = build_world(team_world, valkey_async, credits=4)
    ctx = await ProxyPipeline([QuotaStage("quota", w.engine, _Features())], None).run(_ctx(w, "mia"))
    assert ctx.quota_reservation is not None
    await reconcile_quota(w.engine, ctx, waddleai_tokens=2, provider="openai", model="gpt-4o")
    await reconcile_quota(w.engine, ctx, waddleai_tokens=2, provider="openai", model="gpt-4o")
    assert await _n(valkey_async, keys.user_counter(w.u["mia"], 0)) == 2
    assert (ctx.quota_reservation, ctx.quota_release) == (None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `make test-valkey`
Expected: FAIL only if a prior task is incomplete (the module imports and behaviours exist after Tasks 1-30); on a correct build the first run is the green run. If a test fails, the failure points at the producing task — fix there.

- [ ] **Step 3: Write minimal implementation**

None — this task is pure end-to-end verification of Tasks 1-30.

- [ ] **Step 4: Run test to verify it passes**

Run: `make test-valkey`
Expected: PASS (all quota integration tests, including Tasks 1, 2, 3, 21, 22, 23, 30, 31).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/quota/test_pooled_quota_e2e.py
git commit -m "test(quota): flag-on E2E — reservation->lease->429, isolation, cycle reset, override, fail-open, release"
git push
```
