# Elastic Pooled Quota — Phase 4e: `QuotaEngine`

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6, §7, D5, D7, D9, D10.

---

### Task 26: `QuotaEngine` — reserve / reconcile / release orchestration

The engine is the one place that knows the policy: skip when unconfigured, estimate credits, call the limiter, turn a deny into a block, and on reconcile apply cache-hit-is-free, authoritative overrides and the durable ledger write. The `QuotaStage` (Task 27) and the `_finalize` hook (Task 28) are thin callers.

**Files:**
- Create: `shared/quota/engine.py`
- Test: `tests/unit/quota/test_engine.py`

**Interfaces:**
- Consumes: Task 11 types (`QuotaOutcome`, `QuotaReservation`, `QuotaDecision`, `ReconcileResult`) and `cycle_index/cycle_started/day_stamp/utc_today`; Task 12 `QuotaConfigStore.get_org/get_team`, `QuotaConfigError`; Task 20 `estimate_io_tokens`; Tasks 21-22 `QuotaLimiter.reserve/reconcile/release`; Task 23 `QuotaLedger.add`, `LedgerEntry`, `SCOPE_*`; Task 7 `TokenManager.rates_stale/refresh_rates_if_stale/estimate_credits/providers_for_model`; Task 25 `QuotaTelemetry`.
- Produces: `QuotaEngine(limiter, config_store, ledger, token_manager, telemetry: QuotaTelemetry | None = None, today: Callable[[], date] = utc_today)`:
  - `async reserve(self, user: Any, model: str | None, messages: Sequence[Mapping[str, Any]]) -> QuotaOutcome` — `user` needs `user_id`, `organization_id`, `team_id` attributes. No-op (`blocked=False`, `reservation=None`) when teamless, org config missing/disabled/no anchor, team config missing or in a different org, or the cycle has not started. Config-load failure or a degraded limiter fails OPEN. A deny returns `QuotaOutcome(blocked=True, reason=...)`.
  - `async reconcile(self, reservation: QuotaReservation | None, *, actual_credits: int, provider: str, model: str, cache_hit: bool = False) -> ReconcileResult | None` — `None` reservation or any internal error returns `None` (never raises); `cache_hit` forces 0 actual credits (D9) and no override debit; override caps matched against the ACTUAL provider:model; durable ledger rows written for the final split in the reservation's own cycle.
  - `async release(self, reservation: QuotaReservation | None) -> None` — full refund, never raises.

- [ ] **Step 1: Write the failing test**

`tests/unit/quota/test_engine.py`:

```python
"""QuotaEngine policy with fakes: no-op paths, fail-open paths, deny, and reconcile semantics."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pytest

from shared.quota.config import QuotaConfigError
from shared.quota.engine import QuotaEngine
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry
from shared.quota.types import (
    OrgQuotaConfig, OverrideRule, QuotaDecision, QuotaReservation, ReconcileResult,
    TeamQuotaConfig, UserReservation,
)  # fmt: skip

TODAY = date(2026, 2, 3)
ANCHOR = date(2026, 1, 1)  # cycle 1


@dataclass
class User:
    """The three attributes the engine reads from a UserContext."""

    user_id: int = 7
    organization_id: int = 2
    team_id: int | None = 4


class Tele:
    """Records telemetry calls; span() is a no-op context manager."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a))

    @contextmanager
    def span(self, name):
        class S:
            def set_attribute(self, *a):
                pass

        yield S()


class TM:
    """TokenManager stand-in: fixed estimate, records the model it was asked about."""

    def __init__(self, credits=5):
        self.credits, self.models = credits, []

    def rates_stale(self):
        return False

    def estimate_credits(self, model, i, o):
        self.models.append(model)
        return self.credits

    def providers_for_model(self, model):
        return frozenset({"openai"}) if model == "gpt-4o" else frozenset()


class Limiter:
    """Programmable limiter recording reserve kwargs and reconcile/release calls."""

    def __init__(self, decision=None, reconcile=None):
        self.decision = decision or QuotaDecision(True, None, "rid1", 3, 2)
        self.result = reconcile or ReconcileResult(True, 3, 2)
        self.reserved, self.reconciled, self.released = [], [], []

    async def reserve(self, **kw):
        self.reserved.append(kw)
        return self.decision

    async def reconcile(self, rid, actual, caps):
        self.reconciled.append((rid, actual, list(caps)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def release(self, rid):
        self.released.append(rid)
        return ReconcileResult(True)


class Store:
    """Config store returning programmed configs (or raising)."""

    def __init__(self, org="default", team="default", error=None):
        self.org = OrgQuotaConfig(2, True, ANCHOR, 1000, (OverrideRule(1, 50, user_id=7), OverrideRule(2, 9, provider="openai"))) if org == "default" else org
        self.team = TeamQuotaConfig(4, 2, 100, 10, {7: UserReservation(7, 20, 6)}) if team == "default" else team
        self.error = error

    async def get_org(self, org_id):
        if self.error:
            raise self.error
        return self.org

    async def get_team(self, team_id):
        return self.team


class Ledger:
    """Records ledger writes; can be told to fail."""

    def __init__(self, fail=False):
        self.rows, self.fail = [], fail

    def add(self, org_id, cycle, entries):
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append((org_id, cycle, list(entries)))


def _engine(limiter=None, store=None, ledger=None, tm=None, tele=None):
    """An engine over fakes with a fixed 'today'."""
    return QuotaEngine(limiter or Limiter(), store or Store(), ledger or Ledger(), tm or TM(), tele or Tele(), today=lambda: TODAY)


MSG = [{"role": "user", "content": "hello world " * 10}]


@pytest.mark.parametrize(
    ("user", "store"),
    [
        (User(team_id=None), Store()),                                                       # teamless
        (User(), Store(org=None)),                                                           # no org pool
        (User(), Store(org=OrgQuotaConfig(2, False, ANCHOR, 1, ()))),                        # disabled
        (User(), Store(org=OrgQuotaConfig(2, True, None, 1, ()))),                           # no anchor
        (User(), Store(team=None)),                                                          # team has no pool
        (User(), Store(team=TeamQuotaConfig(4, 99, 1, 1, {}))),                              # team of another org
        (User(), Store(org=OrgQuotaConfig(2, True, date(2026, 6, 1), 1, ()))),               # cycle not started
    ],
)
async def test_unconfigured_paths_are_a_strict_noop(user, store):
    """Review-focus 2: never blocks, never reserves, never touches the limiter."""
    limiter = Limiter()
    out = await _engine(limiter=limiter, store=store).reserve(user, "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None) and limiter.reserved == []


async def test_allowed_reserve_builds_the_reservation_and_filters_overrides():
    """Credits come from the estimate; an unambiguous provider rule is included at reserve; cycle = 1."""
    limiter, tm = Limiter(), TM(credits=5)
    out = await _engine(limiter=limiter, tm=tm).reserve(User(), "gpt-4o", MSG)
    kw = limiter.reserved[0]
    assert (kw["credits"], kw["guaranteed"], kw["buffer_cap"], kw["daily_subcap"]) == (5, 20, 10, 6)
    assert (kw["cycle"], kw["day"], kw["user_id"], kw["team_id"]) == (1, "20260203", 7, 4)
    assert [o.id for o in kw["overrides"]] == [1, 2]  # gpt-4o is served only by openai -> rule 2 matches
    assert out.blocked is False
    assert out.reservation == QuotaReservation("rid1", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


async def test_provider_override_is_skipped_at_reserve_when_the_model_provider_is_unknown():
    """Review-focus 1: an alias/unknown model has no provider set, so provider rules wait for reconcile."""
    limiter = Limiter()
    await _engine(limiter=limiter).reserve(User(), "smart-router", MSG)
    assert [o.id for o in limiter.reserved[0]["overrides"]] == [1]


async def test_unknown_model_none_estimates_with_empty_model_and_user_without_reservation_gets_zero_guarantee():
    """Review-focus 1: model None -> estimate_credits('') ; a member with no row has guaranteed 0."""
    limiter, tm = Limiter(), TM()
    store = Store(team=TeamQuotaConfig(4, 2, 100, 10, {}))
    await _engine(limiter=limiter, tm=tm, store=store).reserve(User(), None, MSG)
    assert tm.models == [""] and limiter.reserved[0]["guaranteed"] == 0 and limiter.reserved[0]["daily_subcap"] is None


async def test_deny_blocks_with_the_reason_and_records_a_rejection():
    """A limiter deny becomes a block (the stage answers 429) with telemetry."""
    tele = Tele()
    limiter = Limiter(QuotaDecision(False, "quota_exhausted"))
    out = await _engine(limiter=limiter, tele=tele).reserve(User(), "m", MSG)
    assert (out.blocked, out.reason, out.reservation) == (True, "quota_exhausted", None)
    assert ("rejection", ("quota_exhausted",)) in tele.calls


async def test_degraded_limiter_and_config_failure_fail_open():
    """D10: a failed-open limiter or an unreadable config never blocks."""
    out = await _engine(limiter=Limiter(QuotaDecision(True, degraded=True))).reserve(User(), "m", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    out = await _engine(store=Store(error=QuotaConfigError("db down"))).reserve(User(), "m", MSG)
    assert (out.blocked, out.reservation) == (False, None)


RES = QuotaReservation("rid1", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


async def test_reconcile_matches_overrides_against_the_actual_provider_and_writes_the_ledger():
    """Caps matched on actual provider:model; ledger gets user/buffer/override rows in the reservation's cycle."""
    limiter, ledger = Limiter(reconcile=ReconcileResult(True, 6, 1, 0, [])), Ledger()
    await _engine(limiter=limiter, ledger=ledger).reconcile(RES, actual_credits=7, provider="openai", model="gpt-4o")
    assert limiter.reconciled == [("rid1", 7, [(1, 50), (2, 9)])]
    assert ledger.rows == [(2, 1, [LedgerEntry(SCOPE_USER, 7, 6), LedgerEntry(SCOPE_BUFFER, 4, 1),
                                   LedgerEntry(SCOPE_OVERRIDE, 1, 7), LedgerEntry(SCOPE_OVERRIDE, 2, 7)])]  # fmt: skip


async def test_cache_hit_costs_zero_and_debits_no_override():
    """D9: cache_hit forces 0 actual credits and sends no override caps."""
    limiter = Limiter(reconcile=ReconcileResult(True, 0, 0))
    ledger = Ledger()
    await _engine(limiter=limiter, ledger=ledger).reconcile(RES, actual_credits=99, provider="cache", model="m", cache_hit=True)
    assert limiter.reconciled == [("rid1", 0, [])] and ledger.rows == []


async def test_reconcile_never_raises_and_orphans_write_no_ledger():
    """Limiter error -> None; orphan (applied False) -> no ledger; ledger failure swallowed; None reservation -> None."""
    assert await _engine(limiter=Limiter(reconcile=RuntimeError("boom"))).reconcile(RES, actual_credits=1, provider="p", model="m") is None
    ledger = Ledger()
    await _engine(limiter=Limiter(reconcile=ReconcileResult(False)), ledger=ledger).reconcile(RES, actual_credits=1, provider="p", model="m")
    assert ledger.rows == []
    res = await _engine(limiter=Limiter(reconcile=ReconcileResult(True, 1, 0)), ledger=Ledger(fail=True)).reconcile(RES, actual_credits=1, provider="p", model="m")
    assert res.applied is True
    assert await _engine().reconcile(None, actual_credits=1, provider="p", model="m") is None


async def test_overage_and_over_cap_are_reported_to_telemetry():
    """Buffer overage and over-cap overrides surface as telemetry (and WARNING logs)."""
    tele = Tele()
    limiter = Limiter(reconcile=ReconcileResult(True, 1, 1, 4, [2]))
    await _engine(limiter=limiter, tele=tele).reconcile(RES, actual_credits=8, provider="openai", model="m")
    assert ("overage", ("buffer", 4)) in tele.calls and ("overage", ("override", 1)) in tele.calls


async def test_release_refunds_and_is_noop_without_a_reservation():
    """release() calls the limiter once; None reservation does nothing."""
    limiter = Limiter()
    await _engine(limiter=limiter).release(RES)
    await _engine(limiter=limiter).release(None)
    assert limiter.released == ["rid1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/quota/test_engine.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.engine`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/engine.py`:

```python
"""QuotaEngine: reserve-before / reconcile-after policy for the pooled quota (spec §6, §7).

Thin callers (``QuotaStage``, the ``_finalize`` hook) delegate here. Every failure path that
could otherwise affect availability fails OPEN with a log (D10); reconcile and release never
raise. Reserve estimates with the REQUESTED model (provider unknown pre-dispatch); reconcile
uses the actual provider:model and is authoritative for override caps.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

from shared.quota.config import QuotaConfigError
from shared.quota.credits import estimate_io_tokens
from shared.quota.cycle import cycle_index, cycle_started, day_stamp, utc_today
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry
from shared.quota.telemetry import QuotaTelemetry
from shared.quota.types import QuotaOutcome, QuotaReservation, ReconcileResult

logger = logging.getLogger(__name__)


class QuotaEngine:
    """Orchestrates config lookup, credit estimate, limiter calls, ledger writes and telemetry."""

    def __init__(
        self,
        limiter: Any,
        config_store: Any,
        ledger: Any,
        token_manager: Any,
        telemetry: QuotaTelemetry | None = None,
        today: Callable[[], date] = utc_today,
    ) -> None:
        """Bind collaborators; ``today`` is injectable so cycle behaviour is testable."""
        self.limiter = limiter
        self.config_store = config_store
        self.ledger = ledger
        self.token_manager = token_manager
        self.telemetry = telemetry or QuotaTelemetry()
        self._today = today

    async def reserve(
        self, user: Any, model: str | None, messages: Sequence[Mapping[str, Any]]
    ) -> QuotaOutcome:
        """Reserve credits for a request, or report why it is blocked (or that nothing applies)."""
        started = time.perf_counter()
        with self.telemetry.span("quota.reserve") as span:
            outcome = await self._reserve(user, model, messages)
            span.set_attribute("quota.blocked", outcome.blocked)
            span.set_attribute("quota.reserved", outcome.reservation is not None)
        self.telemetry.reserve_seconds(time.perf_counter() - started)
        return outcome

    def _noop(self, why: str) -> QuotaOutcome:
        """Record a no-op decision (nothing configured / not applicable) and proceed."""
        logger.debug("quota noop: %s", why)
        self.telemetry.decision("noop", why)
        return QuotaOutcome(blocked=False)

    async def _reserve(
        self, user: Any, model: str | None, messages: Sequence[Mapping[str, Any]]
    ) -> QuotaOutcome:
        """Policy body of :meth:`reserve` (span/latency wrap lives there)."""
        user_id = getattr(user, "user_id", None)
        org_id = getattr(user, "organization_id", None)
        team_id = getattr(user, "team_id", None)
        if team_id is None or org_id is None or user_id is None:
            return self._noop("teamless")
        try:
            org_cfg, team_cfg = await asyncio.gather(
                self.config_store.get_org(org_id), self.config_store.get_team(team_id)
            )
        except QuotaConfigError as exc:
            logger.error("quota config unavailable; failing open: %s", exc)
            self.telemetry.decision("degraded", "config_error")
            return QuotaOutcome(blocked=False)
        if org_cfg is None or not org_cfg.enabled or org_cfg.cycle_anchor_date is None:
            return self._noop("org_unconfigured")
        if team_cfg is None or team_cfg.org_id != org_id:
            return self._noop("team_unconfigured")
        today = self._today()
        if not cycle_started(org_cfg.cycle_anchor_date, today):
            return self._noop("cycle_not_started")
        cycle = cycle_index(org_cfg.cycle_anchor_date, today)

        if self.token_manager.rates_stale():
            await asyncio.to_thread(self.token_manager.refresh_rates_if_stale)
        in_tokens, out_tokens = estimate_io_tokens(messages)
        credits = max(1, self.token_manager.estimate_credits(model or "", in_tokens, out_tokens))
        providers = self.token_manager.providers_for_model(model or "")
        reservation = team_cfg.reservations.get(user_id)
        decision = await self.limiter.reserve(
            user_id=user_id, team_id=team_id, cycle=cycle, day=day_stamp(today), credits=credits,
            guaranteed=reservation.guaranteed_credits if reservation else 0,
            buffer_cap=team_cfg.shared_buffer_credits,
            daily_subcap=reservation.daily_subcap if reservation else None,
            overrides=[o for o in org_cfg.overrides if o.matches_reserve(user_id, model, providers)],
        )  # fmt: skip
        if decision.degraded or (decision.allowed and decision.reservation_id is None):
            self.telemetry.decision("degraded", "limiter_error")
            return QuotaOutcome(blocked=False)
        if not decision.allowed:
            self.telemetry.rejection(decision.reason or "unknown")
            self.telemetry.decision("denied", decision.reason)
            logger.info("quota denied (%s) cycle=%s credits=%d", decision.reason, cycle, credits)
            return QuotaOutcome(blocked=True, reason=decision.reason)
        self.telemetry.decision("allowed")
        self.telemetry.reserved(credits)
        if decision.from_buffer:
            self.telemetry.lease(decision.from_buffer)
        return QuotaOutcome(
            blocked=False,
            reservation=QuotaReservation(
                reservation_id=decision.reservation_id, org_id=org_id, team_id=team_id,
                user_id=user_id, cycle=cycle, credits_reserved=credits,
                from_reservation=decision.from_reservation, from_buffer=decision.from_buffer,
                model_requested=model,
            ),  # fmt: skip
        )

    async def reconcile(
        self,
        reservation: QuotaReservation | None,
        *,
        actual_credits: int,
        provider: str,
        model: str,
        cache_hit: bool = False,
    ) -> ReconcileResult | None:
        """Settle ``reservation`` against actual usage; never raises (returns ``None`` on error)."""
        if reservation is None:
            return None
        try:
            return await self._reconcile(reservation, actual_credits, provider, model, cache_hit)
        except Exception as exc:  # noqa: BLE001 -- reconcile must never fail the response
            logger.error("quota reconcile failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            return None

    async def _reconcile(
        self, res: QuotaReservation, actual_credits: int, provider: str, model: str, cache_hit: bool
    ) -> ReconcileResult:
        """Body of :meth:`reconcile`: caps on the actual provider:model, then the ledger."""
        actual = 0 if cache_hit else max(0, int(actual_credits))
        caps: list[tuple[int, int]] = []
        if actual > 0:
            try:
                org_cfg = await self.config_store.get_org(res.org_id)
            except QuotaConfigError as exc:
                logger.warning("quota override caps skipped (config unavailable): %s", exc)
                org_cfg = None
            if org_cfg is not None:
                caps = [
                    (o.id, o.cap_credits) for o in org_cfg.overrides
                    if o.matches_actual(res.user_id, provider, model)
                ]  # fmt: skip
        result = await self.limiter.reconcile(res.reservation_id, actual, caps)
        self.telemetry.reconciled(result.applied, actual)
        if not result.applied:
            logger.warning("quota reconcile found no reservation (expired or swept); estimate stands")
            return result
        entries = [
            LedgerEntry(SCOPE_USER, res.user_id, result.from_reservation),
            LedgerEntry(SCOPE_BUFFER, res.team_id, result.from_buffer),
            *[LedgerEntry(SCOPE_OVERRIDE, oid, actual) for oid, _cap in caps],
        ]
        entries = [e for e in entries if e.credits > 0]
        if entries:
            try:
                await asyncio.to_thread(self.ledger.add, res.org_id, res.cycle, entries)
            except Exception as exc:  # noqa: BLE001 -- ledger is durable reporting, not enforcement
                logger.error("quota ledger write failed: %s: %s", type(exc).__name__, exc)
        if result.overage:
            self.telemetry.overage("buffer", result.overage)
            logger.warning("quota buffer overage of %d credits recorded", result.overage)
        if result.over_cap_override_ids:
            self.telemetry.overage("override", len(result.over_cap_override_ids))
            logger.warning("quota override cap exceeded for %d rule(s)", len(result.over_cap_override_ids))
        return result

    async def release(self, reservation: QuotaReservation | None) -> None:
        """Refund a reservation in full (request blocked after reserve); never raises."""
        if reservation is None:
            return
        try:
            result = await self.limiter.release(reservation.reservation_id)
            self.telemetry.reconciled(result.applied, 0)
        except Exception as exc:  # noqa: BLE001 -- release is best-effort; the sweeper is the backstop
            logger.error("quota release failed: %s: %s", type(exc).__name__, exc)
```


- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/quota -v --no-cov`
Expected: PASS. The engine test's `Tele.__getattr__` fake records positional calls, so the asserted tuples (`("rejection", ("quota_exhausted",))`, `("overage", ("buffer", 4))`) match the engine's calls exactly.

- [ ] **Step 5: Commit**

```bash
git add shared/quota/engine.py tests/unit/quota/test_engine.py
git commit -m "feat(quota): QuotaEngine — reserve/reconcile/release policy, fail-open, ledger write"
git push
```
