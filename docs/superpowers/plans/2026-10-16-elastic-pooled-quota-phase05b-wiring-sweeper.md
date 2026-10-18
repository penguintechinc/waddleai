# Elastic Pooled Quota — Phase 5b: Proxy Wiring, Finalize Hook, Expiry Sweeper

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6 (single reconcile site in `_finalize`), D7, D10.

---

### Task 28: Wire the engine + stage into `ProxyServer`, and reconcile in both `_finalize` sites

**Files:**
- Create: `proxy/apps/proxy_server/quota_hooks.py`
- Modify: `proxy/apps/proxy_server/main.py` (imports; `ProxyServer.__init__`; `_build_pipeline`; both `_finalize` closures)
- Modify: `tests/unit/proxy/test_proxy_server_main.py` (`test_pipeline_stage_order`; new `TestPooledQuotaWiring`)
- Test: `tests/unit/proxy/test_quota_hooks.py`

**Interfaces:**
- Consumes: `QuotaEngine`, `QuotaConfigStore`, `QuotaLedger`, `QuotaLimiter` (Tasks 12, 23, 21-22, 26); `QuotaStage` (Task 27); `PipelineContext.quota_reservation/quota_release`; existing `valkey` local and `self.token_manager`, `self.features`, `self.db` inside `_build_pipeline`.
- Produces: `quota_hooks.reconcile_quota(engine, ctx, *, waddleai_tokens: int, provider: str, model: str) -> None` (no-op when `engine` or `ctx.quota_reservation` is None; clears `ctx.quota_reservation`/`ctx.quota_release` BEFORE reconciling so nothing is released twice; passes `cache_hit=bool(ctx.cache_hit)`; never raises) and `quota_hooks.config_ttl_seconds() -> int` (`PROXY_QUOTA_CONFIG_TTL_SECONDS`, default 300, invalid/non-positive -> 300). `ProxyServer.quota_engine` / `.quota_limiter` attributes (None without Valkey). The pipeline always contains a `quota` stage between `routing` and `dispatch` (13 stages), a no-op without an engine.

- [ ] **Step 1: Write the failing tests**

`tests/unit/proxy/test_quota_hooks.py`:

```python
"""Finalize/reconcile helper: single settlement, never raises, config TTL parsing."""

import pytest

from proxy.apps.proxy_server.pipeline.stages import PipelineContext
from proxy.apps.proxy_server.quota_hooks import config_ttl_seconds, reconcile_quota
from shared.quota.types import QuotaReservation

RES = QuotaReservation("rid", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


class Engine:
    """Records reconcile calls."""

    def __init__(self):
        self.calls = []

    async def reconcile(self, reservation, **kw):
        self.calls.append((reservation, kw))


def _ctx(**kw):
    """A context holding RES and a release closure."""
    ctx = PipelineContext(user=None, body={}, **kw)
    ctx.quota_reservation = RES

    async def _release():
        raise AssertionError("must not be released after reconcile")

    ctx.quota_release = _release
    return ctx


async def test_reconcile_passes_actual_usage_and_clears_the_release_hook():
    """The reservation is settled once; the release hook is cleared before reconciling."""
    engine, ctx = Engine(), _ctx()
    await reconcile_quota(engine, ctx, waddleai_tokens=9, provider="openai", model="gpt-4o")
    assert engine.calls == [(RES, {"actual_credits": 9, "provider": "openai", "model": "gpt-4o", "cache_hit": False})]
    assert ctx.quota_reservation is None and ctx.quota_release is None


async def test_cache_hit_flag_is_forwarded():
    """cache_hit on the context reaches the engine (D9: 0 credits)."""
    engine = Engine()
    await reconcile_quota(engine, _ctx(cache_hit=True), waddleai_tokens=9, provider="cache", model="m")
    assert engine.calls[0][1]["cache_hit"] is True


async def test_noop_without_engine_or_reservation():
    """No engine (no Valkey) or no reservation (flag off / unconfigured): nothing happens."""
    engine = Engine()
    await reconcile_quota(None, _ctx(), waddleai_tokens=1, provider="p", model="m")
    empty = PipelineContext(user=None, body={})
    await reconcile_quota(engine, empty, waddleai_tokens=1, provider="p", model="m")
    assert engine.calls == []


@pytest.mark.parametrize(("raw", "expected"), [(None, 300), ("45", 45), ("abc", 300), ("0", 300), ("-9", 300)])
def test_config_ttl_parsing(monkeypatch, raw, expected):
    """Garbage or non-positive TTLs fall back to the 300s default instead of crashing startup."""
    if raw is None:
        monkeypatch.delenv("PROXY_QUOTA_CONFIG_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROXY_QUOTA_CONFIG_TTL_SECONDS", raw)
    assert config_ttl_seconds() == expected
```

Append to `tests/unit/proxy/test_proxy_server_main.py` (and update `test_pipeline_stage_order`: docstring "documented 13-stage order", insert `"quota",` between `"routing",` and `"dispatch",`):

```python
class _FakeQuotaEngine:
    """Records reserve/reconcile/release; returns a programmed reserve outcome."""

    def __init__(self, outcome):
        self.outcome, self.reserved, self.reconciled, self.released = outcome, [], [], []

    async def reserve(self, user, model, messages):
        self.reserved.append(model)
        return self.outcome

    async def reconcile(self, reservation, **kw):
        self.reconciled.append((reservation, kw))

    async def release(self, reservation):
        self.released.append(reservation)


class TestPooledQuotaWiring:
    """QuotaStage + the _finalize reconcile hook, end to end through the real Quart app."""

    @staticmethod
    def _arm(monkeypatch, outcome, flag="1"):
        """Install a fake engine on both the stage and the server, set the flag, reset flag cache."""
        fake = _FakeQuotaEngine(outcome)
        stage = next(s for s in proxy_main.proxy_server.pipeline.stages if s.name == "quota")
        monkeypatch.setattr(stage, "engine", fake)
        monkeypatch.setattr(proxy_main.proxy_server, "quota_engine", fake)
        monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", flag)
        proxy_main.proxy_server.features._cache.clear()
        return fake

    async def _post(self, running_app):
        """POST a minimal authenticated chat completion."""
        return await running_app.test_client().post(
            "/v1/chat/completions", headers=_bearer_headers(),
            json={"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "hello world"}]},
        )  # fmt: skip

    async def test_denied_request_is_429_and_never_reconciled(self, running_app, monkeypatch):
        """A quota deny answers 429 with the stable message; nothing is reconciled."""
        from shared.quota.types import QuotaOutcome

        fake = self._arm(monkeypatch, QuotaOutcome(blocked=True, reason="quota_exhausted"))
        resp = await self._post(running_app)
        body = await resp.get_json()
        assert resp.status_code == 429
        assert body["error"]["message"] == "Token quota exhausted for the current billing cycle"
        assert fake.reconciled == []

    async def test_admitted_request_is_reconciled_once_with_actual_credits(self, running_app, monkeypatch):
        """_finalize reconciles the reservation with the credits process_usage computed."""
        from shared.quota.types import QuotaOutcome, QuotaReservation

        res = QuotaReservation("rid", 1, 1, 1, 0, 5, 5, 0, "gpt-3.5-turbo")
        fake = self._arm(monkeypatch, QuotaOutcome(blocked=False, reservation=res))
        resp = await self._post(running_app)
        body = await resp.get_json()
        assert resp.status_code == 200
        ((reservation, kw),) = fake.reconciled
        assert reservation is res and fake.released == []
        assert kw["actual_credits"] == body["usage"]["waddleai_tokens"]
        assert kw["provider"] not in ("", "unknown") and kw["cache_hit"] is False

    async def test_flag_off_never_reserves(self, running_app, monkeypatch):
        """With waddleai.pooled_quota OFF the engine is never consulted and the request succeeds."""
        from shared.quota.types import QuotaOutcome

        fake = self._arm(monkeypatch, QuotaOutcome(blocked=True, reason="quota_exhausted"), flag="0")
        resp = await self._post(running_app)
        assert resp.status_code == 200 and fake.reserved == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/proxy/test_quota_hooks.py tests/unit/proxy/test_proxy_server_main.py -k "quota or stage_order or Quota" -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: ...quota_hooks`; stage-order test fails (no `quota`).

- [ ] **Step 3: Write minimal implementation**

`proxy/apps/proxy_server/quota_hooks.py`:

```python
"""Glue between ``main.py`` and the pooled-quota engine (elastic quota spec §6, D7).

``reconcile_quota`` is the SINGLE reconcile site, called from both ``_finalize`` closures right
after ``process_usage`` so the credits come from ``token_usage.waddleai_tokens``. It clears the
context's release hook first, so a request that completed normally is never also released.
"""

import logging
import os
from typing import Any

from .pipeline.stages import PipelineContext

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_TTL_S = 300


def config_ttl_seconds() -> int:
    """TTL of the proxy's quota-config cache (``PROXY_QUOTA_CONFIG_TTL_SECONDS``, default 300)."""
    raw = os.getenv("PROXY_QUOTA_CONFIG_TTL_SECONDS")
    try:
        value = int(raw) if raw else _DEFAULT_CONFIG_TTL_S
    except ValueError:
        return _DEFAULT_CONFIG_TTL_S
    return value if value > 0 else _DEFAULT_CONFIG_TTL_S


async def reconcile_quota(
    engine: Any, ctx: PipelineContext, *, waddleai_tokens: int, provider: str, model: str
) -> None:
    """Settle ``ctx.quota_reservation`` against the actual credits; never raises."""
    reservation = ctx.quota_reservation
    if engine is None or reservation is None:
        return
    ctx.quota_reservation = None
    ctx.quota_release = None
    await engine.reconcile(
        reservation,
        actual_credits=waddleai_tokens,
        provider=provider,
        model=model,
        cache_hit=bool(ctx.cache_hit),
    )
```

`main.py` edits (then run `ruff check --select I --fix proxy/apps/proxy_server/main.py` so imports land in sorted position):

1. Imports, next to `from .mcp_mount import MCPMount`:

```python
from .pipeline.quota_stage import QuotaStage
from .quota_hooks import config_ttl_seconds, reconcile_quota
```

2. `ProxyServer.__init__`, after the line `self.pipeline = None  # Built once in startup`:

```python
        self.quota_engine = None  # QuotaEngine (pooled quota), built in _build_pipeline()
        self.quota_limiter = None  # its QuotaLimiter (also driven by the expiry sweeper)
```

3. `_build_pipeline`: replace

```python
                    flag="waddleai.smart_routing",
                ),
            ]
        )

        stages.extend(
            [
                DispatchStage(
```

with

```python
                    flag="waddleai.smart_routing",
                ),
            ]
        )

        # Elastic pooled quota (spec §6): reserve just before dispatch -- after CacheStage (a
        # cache hit is free and never 429s) and RoutingStage (the alias-resolved model is the one
        # estimated). Always in the pipeline so the stage order is deterministic; a no-op without
        # Valkey, and flag-gated per org (waddleai.pooled_quota, default OFF) inside the stage.
        self.quota_engine = None
        self.quota_limiter = None
        if valkey is not None:
            from shared.quota.config import QuotaConfigStore
            from shared.quota.engine import QuotaEngine
            from shared.quota.ledger import QuotaLedger
            from shared.quota.lease import QuotaLimiter

            self.quota_limiter = QuotaLimiter(valkey)
            self.quota_engine = QuotaEngine(
                limiter=self.quota_limiter,
                config_store=QuotaConfigStore(valkey, self.db, ttl_s=config_ttl_seconds()),
                ledger=QuotaLedger(self.db),
                token_manager=self.token_manager,
            )
        stages.append(
            QuotaStage(name="quota", engine=self.quota_engine, features=self.features, flag=None)
        )

        stages.extend(
            [
                DispatchStage(
```

4. `chat_completions._finalize` — replace

```python
                actual_input_tokens=usage.get("input_tokens", 0),
                actual_output_tokens=usage.get("output_tokens", 0),
            )

            proxy_server.metrics.record_llm_request(
```

with

```python
                actual_input_tokens=usage.get("input_tokens", 0),
                actual_output_tokens=usage.get("output_tokens", 0),
            )

            await reconcile_quota(
                proxy_server.quota_engine,
                final_ctx,
                waddleai_tokens=token_usage.waddleai_tokens,
                provider=provider,
                model=resolved_model,
            )

            proxy_server.metrics.record_llm_request(
```

5. `claude_messages._finalize` — replace

```python
                actual_input_tokens=usage_info.get("input_tokens", 0),
                actual_output_tokens=usage_info.get("output_tokens", 0),
            )

            proxy_server.metrics.record_llm_request(
```

with the same block (identical `await reconcile_quota(...)` call).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/proxy/test_quota_hooks.py tests/unit/proxy/test_proxy_server_main.py tests/unit/proxy/test_streaming.py -v --no-cov`
Expected: PASS. Any other test that lists the exact stage order (grep `"routing",` under `tests/`) gets `"quota"` inserted between `routing` and `dispatch`.

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/quota_hooks.py proxy/apps/proxy_server/main.py tests/unit/proxy/test_quota_hooks.py tests/unit/proxy/test_proxy_server_main.py
git commit -m "feat(quota): wire QuotaEngine/QuotaStage into the proxy; reconcile in both _finalize sites"
git push
```

---

### Task 29: Expiry sweeper (refund reservations that never reconcile)

Closes the gap for requests that die between reserve and reconcile without passing through the pipeline's block/raise path (process crash, client disconnect mid-stream, `process_usage` raising): their record expires after `RESERVATION_TTL_S` and this loop refunds the credits.

**Files:**
- Create: `proxy/apps/proxy_server/quota_sweeper.py`
- Modify: `proxy/apps/proxy_server/main.py` (`ProxyServer.__init__` attrs; `startup()` after `self.pipeline = self._build_pipeline()`; `shutdown()` first statement)
- Test: `tests/unit/proxy/test_quota_sweeper.py`

**Interfaces:**
- Consumes: `QuotaLimiter.sweep_expired() -> int` (Task 22).
- Produces: `sweeper_interval_s() -> float` (`PROXY_QUOTA_SWEEP_INTERVAL_SECONDS`, default 60, invalid or `< 1` -> 60); `async run_quota_sweeper(limiter, *, interval_s: float, stop: asyncio.Event) -> None` (loops until `stop` is set; an exception in one pass is logged and the loop continues; refunds are logged at WARNING with the count); `async stop_quota_sweeper(task: asyncio.Task | None, stop: asyncio.Event | None) -> None` (no-op for `None`; sets `stop`, cancels, awaits, swallows `CancelledError`). `ProxyServer._quota_sweeper_task` / `._quota_sweeper_stop`.

- [ ] **Step 1: Write the failing test**

`tests/unit/proxy/test_quota_sweeper.py`:

```python
"""The expiry sweeper loop: runs, survives errors, stops promptly, interval parsing."""

import asyncio

import pytest

from proxy.apps.proxy_server.quota_sweeper import run_quota_sweeper, stop_quota_sweeper, sweeper_interval_s


class Limiter:
    """Counts sweeps; programmable results/exceptions per pass."""

    def __init__(self, results):
        self.results, self.calls = list(results), 0

    async def sweep_expired(self):
        self.calls += 1
        item = self.results.pop(0) if self.results else 0
        if isinstance(item, Exception):
            raise item
        return item


async def test_loop_sweeps_repeatedly_and_stops_on_event(caplog):
    """Several passes run, a refund is logged at WARNING, and setting the event ends the loop."""
    limiter, stop = Limiter([2, 0, 0]), asyncio.Event()
    with caplog.at_level("WARNING"):
        task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.01, stop=stop))
        while limiter.calls < 3:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert "refunded 2 expired" in caplog.text


async def test_a_failing_pass_does_not_kill_the_loop(caplog):
    """An exception in one pass is logged and the next pass still runs."""
    limiter, stop = Limiter([RuntimeError("boom"), 1]), asyncio.Event()
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(run_quota_sweeper(limiter, interval_s=0.01, stop=stop))
        while limiter.calls < 2:
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
    assert "quota sweeper pass failed" in caplog.text


async def test_stop_helper_cancels_and_tolerates_none():
    """stop_quota_sweeper ends a running task cleanly and is a no-op for None."""
    await stop_quota_sweeper(None, None)
    stop = asyncio.Event()
    task = asyncio.create_task(run_quota_sweeper(Limiter([]), interval_s=60, stop=stop))
    await asyncio.sleep(0.01)
    await stop_quota_sweeper(task, stop)
    assert task.done()


@pytest.mark.parametrize(("raw", "expected"), [(None, 60.0), ("5", 5.0), ("abc", 60.0), ("0.2", 60.0), ("-3", 60.0)])
def test_interval_parsing(monkeypatch, raw, expected):
    """Garbage or sub-second intervals fall back to 60s (never a hot loop)."""
    if raw is None:
        monkeypatch.delenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS", raw)
    assert sweeper_interval_s() == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/proxy/test_quota_sweeper.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: ...quota_sweeper`.

- [ ] **Step 3: Write minimal implementation**

`proxy/apps/proxy_server/quota_sweeper.py`:

```python
"""Background loop that refunds pooled-quota reservations which never reconciled (spec D7).

A reservation record that outlives ``RESERVATION_TTL_S`` (process crash, client disconnect
mid-stream, an error between reserve and reconcile) still holds its credits in the counters;
``QuotaLimiter.sweep_expired`` refunds them. Several proxy replicas may sweep concurrently: the
refund Lua is atomic and idempotent (a second release finds no record), so that is harmless.
"""

import asyncio
import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 60.0


def sweeper_interval_s() -> float:
    """Seconds between sweeps (``PROXY_QUOTA_SWEEP_INTERVAL_SECONDS``); bad or < 1 -> 60."""
    raw = os.getenv("PROXY_QUOTA_SWEEP_INTERVAL_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_INTERVAL_S
    except ValueError:
        return _DEFAULT_INTERVAL_S
    return value if value >= 1 else _DEFAULT_INTERVAL_S


async def run_quota_sweeper(limiter: Any, *, interval_s: float, stop: asyncio.Event) -> None:
    """Sweep expired reservations every ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        try:
            released = await limiter.sweep_expired()
            if released:
                logger.warning("quota sweeper refunded %d expired reservation(s)", released)
        except Exception as exc:  # noqa: BLE001 -- one bad pass must not end the loop
            logger.error("quota sweeper pass failed: %s: %s", type(exc).__name__, exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


async def stop_quota_sweeper(task: asyncio.Task | None, stop: asyncio.Event | None) -> None:
    """Signal the sweeper to stop, cancel it, and wait for it to finish (no-op for ``None``)."""
    if stop is not None:
        stop.set()
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

`main.py` edits: import `from .quota_sweeper import run_quota_sweeper, stop_quota_sweeper, sweeper_interval_s` (then `ruff check --select I --fix`); in `__init__` after `self.quota_limiter = None  # ...` add:

```python
        self._quota_sweeper_task: asyncio.Task | None = None
        self._quota_sweeper_stop: asyncio.Event | None = None
```

in `startup()` immediately after `logger.info("ProxyPipeline built with %d stages", len(self.pipeline.stages))` add:

```python
        if self.quota_limiter is not None:
            self._quota_sweeper_stop = asyncio.Event()
            self._quota_sweeper_task = asyncio.create_task(
                run_quota_sweeper(
                    self.quota_limiter, interval_s=sweeper_interval_s(), stop=self._quota_sweeper_stop
                )
            )
```

and as the FIRST statement of `async def shutdown(self):` (before `if self.grpc_server:`):

```python
        await stop_quota_sweeper(self._quota_sweeper_task, self._quota_sweeper_stop)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/proxy/test_quota_sweeper.py tests/unit/proxy/test_proxy_server_main.py -v --no-cov`
Expected: PASS (the in-process app boots the sweeper only when Valkey is configured; shutdown tolerates `None`).

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/quota_sweeper.py proxy/apps/proxy_server/main.py tests/unit/proxy/test_quota_sweeper.py
git commit -m "feat(quota): expiry sweeper refunds reservations that never reconcile"
git push
```
