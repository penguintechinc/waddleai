# Elastic Pooled Quota — Phase 5a: `QuotaStage` + Release-on-Block

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6 (reserve), D7, D9, D12.

Placement: `QuotaStage` runs immediately before `DispatchStage`, i.e. AFTER `CacheStage` (a cache hit is free and must never 429 — D9) and AFTER `RoutingStage` (the alias-resolved `ctx.model` is the one estimated). It resolves the `waddleai.pooled_quota` flag itself with `distinct_id=str(organization_id)`: the generic stage-flag path in `ProxyPipeline._run_stage` derives `distinct_id` from `ctx.user.id`, which `UserContext` does not have, so a pipeline-level flag would evaluate with no org targeting.

---

### Task 27: `QuotaStage`, context fields, and release-on-block in the pipeline

**Files:**
- Create: `proxy/apps/proxy_server/pipeline/quota_stage.py`
- Modify: `proxy/apps/proxy_server/pipeline/stages.py` (`PipelineContext` gains two fields; `ProxyPipeline._run_stage` releases a held reservation when a later stage blocks or raises; new `_release_quota` helper)
- Test: `tests/unit/proxy/test_quota_stage.py`, `tests/unit/proxy/test_pipeline_quota_release.py`

**Interfaces:**
- Consumes: `QuotaEngine.reserve/release` and `QuotaOutcome`/`QuotaReservation` (Tasks 11, 26); `Stage`, `PipelineContext`, `_resolve_flag(features, flag_key, distinct_id, *, default=False)` (existing, `stages.py`); `POOLED_QUOTA_FLAG` (Task 16).
- Produces:
  - `PipelineContext.quota_reservation: Any | None = None` and `PipelineContext.quota_release: Callable[[], Awaitable[None]] | None = None`.
  - `QuotaStage(name: str, engine: QuotaEngine | None, features: Any, flag: str | None = None)` in `quota_stage.py`: no-op when `engine is None`, the org id is unknown, `ctx.cache_hit` is true, or the flag is OFF; a blocked outcome sets `ctx.blocked = True`, `ctx.status_code = 429`, `ctx.block_reason = <message below>`; an admitted request sets `ctx.quota_reservation` and `ctx.quota_release` (an async closure that calls `engine.release`). Module constant `BLOCK_MESSAGES: dict[str, str]` (`quota_exhausted` -> "Token quota exhausted for the current billing cycle", `daily_subcap_exceeded` -> "Daily token limit reached", `override_exceeded` -> "Token limit reached for this user, model or provider") with a generic default.
  - `ProxyPipeline._run_stage`: after any stage returns with `ctx.blocked` set, or raises, a non-None `ctx.quota_release` is awaited exactly once, cleared, and a failing release is logged, never raised. Task 28's `_finalize` hook clears `quota_release` after reconcile so a normally-completed request is never released.

- [ ] **Step 1: Write the failing tests**

`tests/unit/proxy/test_quota_stage.py`:

```python
"""QuotaStage: flag/cache/org gating, 429 on deny, reservation + release closure on allow."""

from dataclasses import dataclass

import pytest

from proxy.apps.proxy_server.pipeline.quota_stage import BLOCK_MESSAGES, QuotaStage
from proxy.apps.proxy_server.pipeline.stages import PipelineContext
from shared.quota.types import QuotaOutcome, QuotaReservation

RES = QuotaReservation("rid", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


@dataclass
class User:
    """The attribute the stage reads (UserContext has organization_id, no ``id``)."""

    organization_id: int | None = 2
    user_id: int = 7


class Features:
    """Sync feature helper stub recording (flag, distinct_id) lookups."""

    def __init__(self, on=True):
        self.on, self.calls = on, []

    def is_feature_enabled(self, flag, distinct_id=None, **kw):
        self.calls.append((flag, distinct_id))
        return self.on


class Engine:
    """Records reserve/release; returns a programmed outcome."""

    def __init__(self, outcome=None):
        self.outcome = outcome or QuotaOutcome(blocked=False, reservation=RES)
        self.reserved, self.released = [], []

    async def reserve(self, user, model, messages):
        self.reserved.append((user, model, list(messages)))
        return self.outcome

    async def release(self, reservation):
        self.released.append(reservation)


def _ctx(user=None, **kw):
    """A pipeline context for an authenticated org-2 user asking for gpt-4o."""
    return PipelineContext(user=user or User(), body={}, model="gpt-4o", messages=[{"content": "hi"}], **kw)


async def test_flag_is_resolved_per_org_and_off_is_a_noop():
    """distinct_id is str(organization_id); OFF means the engine is never consulted."""
    feats, engine = Features(on=False), Engine()
    ctx = await QuotaStage("quota", engine, feats)(_ctx())
    assert feats.calls == [("waddleai.pooled_quota", "2")]
    assert engine.reserved == [] and ctx.blocked is False and ctx.quota_reservation is None


@pytest.mark.parametrize("make", [lambda: _ctx(cache_hit=True), lambda: _ctx(user=User(organization_id=None))])
async def test_cache_hits_and_unknown_org_skip_the_engine(make):
    """D9: a cache hit is free (never reserved, never 429); no org id -> nothing to key on."""
    engine = Engine()
    ctx = await QuotaStage("quota", engine, Features())(make())
    assert engine.reserved == [] and ctx.quota_reservation is None


async def test_no_engine_is_a_noop():
    """Without Valkey the proxy builds the stage with engine=None."""
    ctx = await QuotaStage("quota", None, Features())(_ctx())
    assert ctx.blocked is False


@pytest.mark.parametrize("reason", ["quota_exhausted", "daily_subcap_exceeded", "override_exceeded", "something_new"])
async def test_deny_blocks_with_429_and_a_user_safe_message(reason):
    """A deny becomes status 429 with a stable message; unknown reasons get the generic text."""
    engine = Engine(QuotaOutcome(blocked=True, reason=reason))
    ctx = await QuotaStage("quota", engine, Features())(_ctx())
    assert (ctx.blocked, ctx.status_code) == (True, 429)
    assert ctx.block_reason == BLOCK_MESSAGES.get(reason, "Token quota exceeded")
    assert ctx.quota_reservation is None and ctx.quota_release is None


async def test_allow_stores_the_reservation_and_a_release_closure():
    """The context carries the reservation; calling the closure refunds through the engine."""
    engine = Engine()
    ctx = await QuotaStage("quota", engine, Features())(_ctx())
    assert ctx.blocked is False and ctx.quota_reservation == RES
    await ctx.quota_release()
    assert engine.released == [RES]
    assert engine.reserved[0][1] == "gpt-4o"  # the alias-resolved model is what is estimated


async def test_noop_outcome_leaves_the_context_untouched():
    """Teamless/unconfigured: the engine returns blocked=False with no reservation."""
    ctx = await QuotaStage("quota", Engine(QuotaOutcome(blocked=False)), Features())(_ctx())
    assert (ctx.blocked, ctx.quota_reservation, ctx.quota_release) == (False, None, None)
```

`tests/unit/proxy/test_pipeline_quota_release.py`:

```python
"""ProxyPipeline releases a held reservation when a LATER stage blocks or raises (review-focus 4)."""

from unittest.mock import AsyncMock

import pytest

from proxy.apps.proxy_server.pipeline.stages import PipelineContext, ProxyPipeline, Stage


class Holds(Stage):
    """Pretends to be the QuotaStage: installs a release closure."""

    def __init__(self, release):
        super().__init__("quota", None)
        self.release = release

    async def __call__(self, ctx):
        ctx.quota_release = self.release
        return ctx


class Blocks(Stage):
    """A later stage that blocks the request (e.g. security_out)."""

    async def __call__(self, ctx):
        ctx.blocked, ctx.status_code, ctx.block_reason = True, 400, "blocked"
        return ctx


class Raises(Stage):
    """A later stage that raises."""

    async def __call__(self, ctx):
        raise RuntimeError("provider exploded")


class Passes(Stage):
    """A later stage that succeeds."""

    async def __call__(self, ctx):
        return ctx


def _ctx():
    """A bare context."""
    return PipelineContext(user=None, body={})


async def test_block_after_reserve_releases_exactly_once_and_clears_the_hook():
    """security_out-style block: the reservation is refunded once and the hook cleared."""
    release = AsyncMock()
    ctx = await ProxyPipeline([Holds(release), Blocks("b", None), Passes("p", None)], None).run(_ctx())
    release.assert_awaited_once()
    assert ctx.quota_release is None and ctx.quota_reservation is None


async def test_exception_after_reserve_releases_and_still_raises():
    """A crashing stage refunds and the error propagates unchanged."""
    release = AsyncMock()
    with pytest.raises(RuntimeError, match="provider exploded"):
        await ProxyPipeline([Holds(release), Raises("r", None)], None).run(_ctx())
    release.assert_awaited_once()


async def test_successful_pipeline_does_not_release():
    """Normal completion leaves the reservation for _finalize to reconcile."""
    release = AsyncMock()
    ctx = await ProxyPipeline([Holds(release), Passes("p", None)], None).run(_ctx())
    release.assert_not_awaited()
    assert ctx.quota_release is release


async def test_failing_release_is_logged_not_raised(caplog):
    """A refund that itself fails must not mask the block."""
    release = AsyncMock(side_effect=ConnectionError("down"))
    with caplog.at_level("ERROR"):
        ctx = await ProxyPipeline([Holds(release), Blocks("b", None)], None).run(_ctx())
    assert ctx.blocked is True and "quota release failed" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/proxy/test_quota_stage.py tests/unit/proxy/test_pipeline_quota_release.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: ...quota_stage` / `TypeError: unexpected keyword 'cache_hit'`-free but `AttributeError: quota_release` on the context.

- [ ] **Step 3: Write minimal implementation**

In `stages.py`, add to `PipelineContext` right after `reservation_id: str | None = None`:

```python
    # --- Pooled quota (elastic quota spec §6, D7) ---
    # Set by QuotaStage on admit; _finalize reconciles then clears both. `quota_release` is an
    # async closure ProxyPipeline awaits when a later stage blocks/raises so the reserved
    # credits are refunded immediately (a TTL on the record alone would not refund counters).
    quota_reservation: Any | None = None
    quota_release: Callable[[], Awaitable[None]] | None = None
```

Replace the tail of `ProxyPipeline._run_stage` (the `with self.tracer...` block through `return ctx`) with:

```python
        # Execute stage with tracing
        with self.tracer.start_as_current_span(stage.name) as span:
            try:
                ctx = await stage(ctx)
                ctx.stage_log.append(f"ran:{stage.name}")
                if stage.name == "dispatch":
                    self._set_genai_attributes(span, ctx)
            except Exception as e:
                logger.error(f"Stage {stage.name} failed: {e}", exc_info=True)
                span.set_attribute("error", True)
                span.set_attribute("error.message", str(e))
                await self._release_quota(ctx)
                # Don't block the pipeline on stage errors; let them propagate
                raise

        if ctx.blocked:
            await self._release_quota(ctx)
        return ctx

    @staticmethod
    async def _release_quota(ctx: PipelineContext) -> None:
        """Refund a held quota reservation once; a failing refund is logged, never raised."""
        release = ctx.quota_release
        if release is None:
            return
        ctx.quota_release = None
        ctx.quota_reservation = None
        try:
            await release()
        except Exception as exc:  # noqa: BLE001 -- the sweeper is the backstop for a failed refund
            logger.error("quota release failed: %s: %s", type(exc).__name__, exc)
```

`proxy/apps/proxy_server/pipeline/quota_stage.py`:

```python
"""QuotaStage: reserve pooled-quota credits just before dispatch (elastic quota spec §6, D7, D9).

Placed after CacheStage (a cache hit is free and must never 429) and RoutingStage (the
alias-resolved model is the one estimated). The flag is resolved here with
``distinct_id=str(organization_id)`` -- the generic stage-flag path derives it from
``ctx.user.id``, which ``UserContext`` lacks. A deny is a 429 with a stable, user-safe message;
an admit stores the reservation plus a release closure so a later block/exception refunds it.
"""

import logging
from typing import Any

from shared.quota.flags import POOLED_QUOTA_FLAG

from .stages import PipelineContext, Stage, _resolve_flag

logger = logging.getLogger(__name__)

BLOCK_MESSAGES: dict[str, str] = {
    "quota_exhausted": "Token quota exhausted for the current billing cycle",
    "daily_subcap_exceeded": "Daily token limit reached",
    "override_exceeded": "Token limit reached for this user, model or provider",
}
_DEFAULT_MESSAGE = "Token quota exceeded"


class QuotaStage(Stage):
    """Reserve credits via the QuotaEngine; block with 429 when the quota is exhausted."""

    def __init__(self, name: str, engine: Any, features: Any, flag: str | None = None) -> None:
        """Bind the (possibly ``None``) engine and the feature helper used for the org flag."""
        super().__init__(name, flag)
        self.engine = engine
        self.features = features

    async def __call__(self, ctx: PipelineContext) -> PipelineContext:
        """Reserve for this request, or pass through when quota does not apply."""
        org_id = getattr(ctx.user, "organization_id", None)
        if self.engine is None or org_id is None or ctx.cache_hit:
            return ctx
        if not await _resolve_flag(self.features, POOLED_QUOTA_FLAG, str(org_id), default=False):
            return ctx
        outcome = await self.engine.reserve(ctx.user, ctx.model, ctx.messages)
        if outcome.blocked:
            ctx.blocked = True
            ctx.status_code = 429
            ctx.block_reason = BLOCK_MESSAGES.get(outcome.reason or "", _DEFAULT_MESSAGE)
            return ctx
        reservation = outcome.reservation
        if reservation is not None:
            engine = self.engine
            ctx.quota_reservation = reservation

            async def _release() -> None:
                """Refund this request's reservation (a later stage blocked or raised)."""
                await engine.release(reservation)

            ctx.quota_release = _release
        return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/proxy/test_quota_stage.py tests/unit/proxy/test_pipeline_quota_release.py tests/unit/proxy/test_pipeline.py tests/unit/proxy/test_pipeline_stages.py -v --no-cov`
Expected: PASS. In `test_pipeline_quota_release.py` the stages are built with positional `(name, flag)` args — `Blocks("b", None)` calls `Stage.__init__(name, flag)`; `Holds.__init__` passes `("quota", None)`.

- [ ] **Step 5: Commit**

```bash
git add proxy/apps/proxy_server/pipeline/quota_stage.py proxy/apps/proxy_server/pipeline/stages.py tests/unit/proxy/test_quota_stage.py tests/unit/proxy/test_pipeline_quota_release.py
git commit -m "feat(quota): QuotaStage (flag per org, cache hits free) + release-on-block/raise in ProxyPipeline"
git push
```
