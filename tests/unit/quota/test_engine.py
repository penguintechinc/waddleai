# ruff: noqa: D102, D105, D107, N818, E501  -- tiny fakes; the test docstrings carry the intent
"""QuotaEngine policy with fakes: no-op paths, fail-open paths, deny, and reconcile semantics."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pytest

from shared.quota.config import QuotaConfigError
from shared.quota.engine import QuotaEngine
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry
from shared.quota.types import (
    OrgQuotaConfig,
    OverrideRule,
    QuotaDecision,
    QuotaReservation,
    ReconcileResult,
    TeamQuotaConfig,
    UserReservation,
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
        self.org = (
            OrgQuotaConfig(
                2,
                True,
                ANCHOR,
                1000,
                (OverrideRule(1, 50, user_id=7), OverrideRule(2, 9, provider="openai")),
            )
            if org == "default"
            else org
        )
        self.team = (
            TeamQuotaConfig(4, 2, 100, 10, {7: UserReservation(7, 20, 6)})
            if team == "default"
            else team
        )
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
        self.rows, self.fail, self.attempts = [], fail, 0

    def add(self, org_id, cycle, entries):
        self.attempts += 1
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append((org_id, cycle, list(entries)))


def _engine(limiter=None, store=None, ledger=None, tm=None, tele=None):
    """An engine over fakes with a fixed 'today'."""
    return QuotaEngine(
        limiter or Limiter(),
        store or Store(),
        ledger or Ledger(),
        tm or TM(),
        tele or Tele(),
        today=lambda: TODAY,
        flag=lambda _org: True,
    )


MSG = [{"role": "user", "content": "hello world " * 10}]


GATES = {
    "teamless": (User(team_id=None), Store(), True),
    "flag_off": (User(), Store(error=RuntimeError("config must not be read")), False),
    "org_none": (User(), Store(org=None), True),
    "org_disabled": (User(), Store(org=OrgQuotaConfig(2, False, ANCHOR, 1, ())), True),
    "no_anchor": (User(), Store(org=OrgQuotaConfig(2, True, None, 1, ())), True),
    "future_anchor": (User(), Store(org=OrgQuotaConfig(2, True, date(2026, 2, 4), 1, ())), True),
    "team_none": (User(), Store(team=None), True),
    "team_other_org": (User(), Store(team=TeamQuotaConfig(4, 99, 1, 1, {})), True),
}


@pytest.mark.parametrize("gate", list(GATES))
async def test_each_unconfigured_gate_is_a_strict_noop(gate):
    """Req 1: per gate -- limiter never called, NO fault (not a fail-open), not blocked."""
    user, store, flag_on = GATES[gate]
    limiter, tele = Limiter(), Tele()
    eng = QuotaEngine(
        limiter, store, Ledger(), TM(), tele, today=lambda: TODAY, flag=lambda _o: flag_on
    )
    out = await eng.reserve(user, "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None)
    assert len(limiter.reserved) == 0
    assert not [c for c in tele.calls if c[0] == "fault"]


async def test_allowed_reserve_builds_the_reservation_and_filters_overrides():
    """Credits come from the estimate; an unambiguous provider rule is included at reserve; cycle = 1."""
    limiter, tm = Limiter(), TM(credits=5)
    out = await _engine(limiter=limiter, tm=tm).reserve(User(), "gpt-4o", MSG)
    kw = limiter.reserved[0]
    assert (kw["credits"], kw["guaranteed"], kw["buffer_cap"], kw["daily_subcap"]) == (5, 20, 10, 6)
    assert (kw["cycle"], kw["day"], kw["user_id"], kw["team_id"]) == (1, "20260203", 7, 4)
    assert [o.id for o in kw["overrides"]] == [
        1,
        2,
    ]  # gpt-4o is served only by openai -> rule 2 matches
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
    assert (
        tm.models == [""]
        and limiter.reserved[0]["guaranteed"] == 0
        and limiter.reserved[0]["daily_subcap"] is None
    )


async def test_deny_blocks_with_the_reason_and_records_a_rejection():
    """A limiter deny becomes a block (the stage answers 429) with telemetry."""
    tele = Tele()
    limiter = Limiter(QuotaDecision(False, "quota_exhausted"))
    out = await _engine(limiter=limiter, tele=tele).reserve(User(), "m", MSG)
    assert (out.blocked, out.reason, out.reservation) == (True, "quota_exhausted", None)
    assert ("rejection", ("quota_exhausted",)) in tele.calls


async def test_degraded_limiter_and_config_failure_fail_open():
    """D10: a failed-open limiter or an unreadable config never blocks."""
    out = await _engine(limiter=Limiter(QuotaDecision(True, degraded=True))).reserve(
        User(), "m", MSG
    )
    assert (out.blocked, out.reservation) == (False, None)
    out = await _engine(store=Store(error=QuotaConfigError("db down"))).reserve(User(), "m", MSG)
    assert (out.blocked, out.reservation) == (False, None)


RES = QuotaReservation("rid1", 2, 4, 7, 1, 5, 3, 2, "gpt-4o")


async def test_reconcile_matches_overrides_against_the_actual_provider_and_writes_the_ledger():
    """Caps matched on actual provider:model; ledger gets user/buffer/override rows in the reservation's cycle."""
    limiter, ledger = Limiter(reconcile=ReconcileResult(True, 6, 1, 0, [])), Ledger()
    await _engine(limiter=limiter, ledger=ledger).reconcile(
        RES, actual_credits=7, provider="openai", model="gpt-4o"
    )
    assert limiter.reconciled == [("rid1", 7, [(1, 50), (2, 9)])]
    assert ledger.rows == [(2, 1, [LedgerEntry(SCOPE_USER, 7, 6), LedgerEntry(SCOPE_BUFFER, 4, 1),
                                   LedgerEntry(SCOPE_OVERRIDE, 1, 7), LedgerEntry(SCOPE_OVERRIDE, 2, 7)])]  # fmt: skip


async def test_cache_hit_costs_zero_and_debits_no_override():
    """D9: cache_hit forces 0 actual credits and sends no override caps."""
    limiter = Limiter(reconcile=ReconcileResult(True, 0, 0))
    ledger = Ledger()
    await _engine(limiter=limiter, ledger=ledger).reconcile(
        RES, actual_credits=99, provider="cache", model="m", cache_hit=True
    )
    assert limiter.reconciled == [("rid1", 0, [])] and ledger.rows == []


async def test_reconcile_never_raises_and_orphans_write_no_ledger():
    """Limiter error -> None; orphan (applied False) -> no ledger; ledger failure swallowed; None reservation -> None."""
    assert (
        await _engine(limiter=Limiter(reconcile=RuntimeError("boom"))).reconcile(
            RES, actual_credits=1, provider="p", model="m"
        )
        is None
    )
    ledger = Ledger()
    await _engine(limiter=Limiter(reconcile=ReconcileResult(False)), ledger=ledger).reconcile(
        RES, actual_credits=1, provider="p", model="m"
    )
    assert ledger.rows == []
    res = await _engine(
        limiter=Limiter(reconcile=ReconcileResult(True, 1, 0)), ledger=Ledger(fail=True)
    ).reconcile(RES, actual_credits=1, provider="p", model="m")
    assert res.applied is True
    assert await _engine().reconcile(None, actual_credits=1, provider="p", model="m") is None


async def test_overage_and_over_cap_are_reported_to_telemetry():
    """Buffer overage and over-cap overrides surface as telemetry (and WARNING logs)."""
    tele = Tele()
    limiter = Limiter(reconcile=ReconcileResult(True, 1, 1, 4, [2]))
    await _engine(limiter=limiter, tele=tele).reconcile(
        RES, actual_credits=8, provider="openai", model="m"
    )
    assert ("overage", ("buffer", 4)) in tele.calls and ("overage", ("override", 1)) in tele.calls


async def test_release_refunds_and_is_noop_without_a_reservation():
    """release() calls the limiter once; None reservation does nothing."""
    limiter = Limiter()
    await _engine(limiter=limiter).release(RES)
    await _engine(limiter=limiter).release(None)
    assert limiter.released == ["rid1"]


# ---- carry-forward pins (critical requirements 1, 3, 4, 5, 6, 7, 8) ----


async def test_anchor_day_itself_is_enforced():
    """Boundary: today == anchor starts cycle 0."""
    limiter = Limiter()
    store = Store(org=OrgQuotaConfig(2, True, TODAY, 1, ()))
    await _engine(limiter=limiter, store=store).reserve(User(), "m", MSG)
    assert limiter.reserved[0]["cycle"] == 0


class Boom(Exception):
    """Arbitrary non-quota failure."""


@pytest.mark.parametrize(
    "source",
    [
        "estimate_io",
        "estimate_credits",
        "config_generic",
        "config_quota",
        "limiter",
        "providers",
        "rates",
    ],
)
async def test_every_failopen_source_allows_faults_and_never_raises(source, monkeypatch):
    """Req 3: any exception source -> allow, fault emitted, no reservation, no 500."""
    tele, limiter, tm, store = Tele(), Limiter(), TM(), Store()
    if source == "estimate_io":
        monkeypatch.setattr(
            "shared.quota.engine.estimate_io_tokens", lambda m: (_ for _ in ()).throw(Boom())
        )
    elif source == "estimate_credits":
        tm.estimate_credits = lambda *a: (_ for _ in ()).throw(Boom())
    elif source == "providers":
        tm.providers_for_model = lambda *a: (_ for _ in ()).throw(Boom())
    elif source == "rates":
        tm.rates_stale = lambda: (_ for _ in ()).throw(Boom())
    elif source == "config_generic":
        store.error = Boom("x")
    elif source == "config_quota":
        store.error = QuotaConfigError("db down")
    else:

        async def bad(**kw):
            raise Boom("valkey down")

        limiter.reserve = bad
    out = await _engine(limiter=limiter, store=store, tm=tm, tele=tele).reserve(
        User(), "gpt-4o", MSG
    )
    assert (out.blocked, out.reservation) == (False, None)
    assert any(c[0] == "fault" for c in tele.calls)


async def test_failopen_after_grant_releases_the_lease_no_dangling_reservation():
    """Req 3: failure after the limiter granted a lease -> that lease is refunded."""
    limiter = Limiter()

    class BadTele(Tele):
        def __getattr__(self, name):
            if name == "reserved":

                def boom(*a):
                    raise Boom("exporter")

                return boom
            return super().__getattr__(name)

    out = await _engine(limiter=limiter, tele=BadTele()).reserve(User(), "gpt-4o", MSG)
    assert (out.blocked, out.reservation) == (False, None) and limiter.released == ["rid1"]


async def test_degraded_limiter_emits_a_fault():
    """Req 3: a limiter that failed open is visible as a fault, not silent."""
    tele = Tele()
    await _engine(limiter=Limiter(QuotaDecision(True, degraded=True)), tele=tele).reserve(
        User(), "m", MSG
    )
    assert ("fault", ("limiter_degraded",)) in tele.calls


async def test_overrides_never_include_a_none_id_and_pre_filter_applies():
    """Req 4: a rule with a None id never reaches reserve; non-matching users filtered."""
    org = OrgQuotaConfig(
        2,
        True,
        ANCHOR,
        1000,
        (OverrideRule(None, 5), OverrideRule(3, 5, user_id=99), OverrideRule(4, 5)),
    )
    limiter = Limiter()
    await _engine(limiter=limiter, store=Store(org=org)).reserve(User(), "m", MSG)
    assert [o.id for o in limiter.reserved[0]["overrides"]] == [4]


async def test_cache_provider_is_free_even_without_the_cache_hit_flag():
    """Req 5: provider == 'cache' costs 0 and debits no override."""
    limiter = Limiter(reconcile=ReconcileResult(True, 0, 0))
    await _engine(limiter=limiter).reconcile(RES, actual_credits=50, provider="cache", model="m")
    assert limiter.reconciled == [("rid1", 0, [])]


async def test_ledger_failure_is_logged_faulted_and_never_retried():
    """Req 6: one add attempt only (a retry double-counts); fault emitted; result still returned."""
    tele, ledger = Tele(), Ledger(fail=True)
    res = await _engine(
        limiter=Limiter(reconcile=ReconcileResult(True, 2, 1)), ledger=ledger, tele=tele
    ).reconcile(RES, actual_credits=3, provider="p", model="m")
    assert res.applied is True and ("fault", ("ledger_write",)) in tele.calls
    assert ledger.attempts == 1


async def test_reconcile_and_release_failures_fault_and_do_not_raise():
    """Req 5: reconcile/release errors are swallowed with a fault."""
    tele = Tele()
    assert (
        await _engine(limiter=Limiter(reconcile=Boom("x")), tele=tele).reconcile(
            RES, actual_credits=1, provider="p", model="m"
        )
        is None
    )
    assert ("fault", ("reconcile_error",)) in tele.calls

    class Bad(Limiter):
        async def release(self, rid):
            raise Boom("x")

    await _engine(limiter=Bad(), tele=tele).release(RES)
    assert ("fault", ("release_failed",)) in tele.calls


async def test_reconcile_survives_config_outage_without_caps():
    """Req 5: config down at reconcile -> caps skipped, debit still settles."""
    limiter = Limiter(reconcile=ReconcileResult(True, 3, 0))
    res = await _engine(limiter=limiter, store=Store(error=Boom("x"))).reconcile(
        RES, actual_credits=3, provider="openai", model="gpt-4o"
    )
    assert res.applied is True and limiter.reconciled == [("rid1", 3, [])]


async def test_negative_config_values_are_clamped_before_the_limiter():
    """Req 7: a corrupt/over-allocated config never feeds negatives to the limiter."""
    limiter = Limiter()
    store = Store(team=TeamQuotaConfig(4, 2, 100, -5, {7: UserReservation(7, -3, 6)}))
    await _engine(limiter=limiter, store=store).reserve(User(), "m", MSG)
    assert (limiter.reserved[0]["guaranteed"], limiter.reserved[0]["buffer_cap"]) == (0, 0)


async def test_unknown_limiter_reason_is_a_bounded_telemetry_label():
    """Req 8: an unexpected deny reason is labelled 'unknown'; no ids in any label."""
    tele = Tele()
    out = await _engine(limiter=Limiter(QuotaDecision(False, "user-7-weird")), tele=tele).reserve(
        User(), "m", MSG
    )
    assert out.blocked and ("rejection", ("unknown",)) in tele.calls
    labels = [a for name, args in tele.calls if name != "reserve_seconds" for a in args]
    assert all(isinstance(a, str) for a in labels) and "user-7-weird" not in labels


async def test_no_id_value_in_any_telemetry_call_on_allow_and_reconcile_paths():
    """Req 8: user/team/org ids (7/4/2) never appear as a label across allow + reconcile."""
    tele = Tele()
    eng = _engine(tele=tele)
    out = await eng.reserve(User(), "gpt-4o", MSG)
    await eng.reconcile(out.reservation, actual_credits=7, provider="openai", model="gpt-4o")
    names = {c[0] for c in tele.calls}
    assert {"decision", "reserved", "reconciled", "lease"} <= names
    label_calls = {"decision", "rejection", "fault"}
    labels = [a for n, args in tele.calls if n in label_calls for a in args]
    labels += [args[0] for n, args in tele.calls if n in {"overage", "reconciled"}]
    assert labels and all(isinstance(a, (str, bool)) for a in labels)
    assert not {str(a) for a in labels} & {"7", "4", "2"}
