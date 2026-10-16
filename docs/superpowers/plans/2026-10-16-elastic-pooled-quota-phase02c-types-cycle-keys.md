# Elastic Pooled Quota — Phase 2c: Shared Types, Cycle Math, Key Builders

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5, §9, D2/D3/D6.

Part of the data-model phase (spec §5, §9, D2/D3/D6); earlier tasks of this phase created the tables and migration.

---

### Task 11: Shared quota types, cycle math, Valkey key builders

Pure, dependency-free modules every later task imports.

**Files:**
- Create: `shared/quota/__init__.py` (empty docstring-only package marker)
- Create: `shared/quota/types.py`
- Create: `shared/quota/cycle.py`
- Create: `shared/quota/keys.py`
- Test: `tests/unit/quota/__init__.py` (empty), `tests/unit/quota/test_types_cycle_keys.py`

**Interfaces:**
- Consumes: nothing.
- Produces (exact names later tasks import):
  - `shared.quota.types`: `OverrideRule(id: int, cap_credits: int, user_id: int | None = None, model: str | None = None, provider: str | None = None)` with `.matches_reserve(user_id: int, model: str | None, providers: frozenset[str] = frozenset()) -> bool` (best-effort pre-dispatch match: a rule naming a `provider` matches only when `providers` — the providers known to serve the requested model — is exactly `{rule.provider}`; with an empty or ambiguous set it never matches here and is enforced at reconcile only) and `.matches_actual(user_id: int, provider: str, model: str) -> bool`; `UserReservation(user_id: int, guaranteed_credits: int, daily_subcap: int | None)`; `OrgQuotaConfig(org_id: int, enabled: bool, cycle_anchor_date: date | None, pool_credits: int, overrides: tuple[OverrideRule, ...])`; `TeamQuotaConfig(team_id: int, org_id: int, pool_credits: int, shared_buffer_credits: int, reservations: dict[int, UserReservation])`; `QuotaDecision(allowed: bool, reason: str | None = None, reservation_id: str | None = None, from_reservation: int = 0, from_buffer: int = 0, degraded: bool = False)`; `ReconcileResult(applied: bool, from_reservation: int = 0, from_buffer: int = 0, overage: int = 0, over_cap_override_ids: list[int] = [])`; `QuotaReservation(reservation_id: str, org_id: int, team_id: int, user_id: int, cycle: int, credits_reserved: int, from_reservation: int, from_buffer: int, model_requested: str | None)`; `QuotaOutcome(blocked: bool, reason: str | None = None, reservation: QuotaReservation | None = None)`; reason constants `REASON_EXHAUSTED = "quota_exhausted"`, `REASON_DAILY = "daily_subcap_exceeded"`, `REASON_OVERRIDE = "override_exceeded"`.
  - `shared.quota.cycle`: `CYCLE_DAYS = 30`; `utc_today() -> date`; `cycle_index(anchor: date, today: date) -> int` (floor division, negative before the anchor); `cycle_started(anchor: date, today: date) -> bool`; `cycle_bounds(anchor: date, index: int) -> tuple[date, date]` (start inclusive, end exclusive); `day_stamp(d: date) -> str` (`YYYYMMDD`).
  - `shared.quota.keys`: `user_counter(user_id, cycle)`, `buffer_counter(team_id, cycle)`, `override_counter(override_id, cycle)`, `daily_counter(user_id, day)`, `reservation(reservation_id)`, `org_config(org_id)`, `team_config(team_id)`, constants `RESERVATION_INDEX`, `OVERRIDE_PREFIX`.

- [ ] **Step 1: Write the failing test**

`tests/unit/quota/test_types_cycle_keys.py`:

```python
"""Cycle math (spec D6), override matching (spec §7) and Valkey key shapes."""

from datetime import date

import pytest

from shared.quota import cycle, keys
from shared.quota.types import OverrideRule

ANCHOR = date(2026, 1, 1)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), 0),    # anchor day
        (date(2026, 1, 30), 0),   # day 29: last day of cycle 0
        (date(2026, 1, 31), 1),   # day 30: exact boundary starts cycle 1
        (date(2026, 3, 2), 2),    # day 60
        (date(2025, 12, 31), -1), # before the anchor floors negative, deterministically
    ],
)
def test_cycle_index_boundaries(today, expected):
    """30-day anchored cycles, floor semantics, UTC dates only."""
    assert cycle.cycle_index(ANCHOR, today) == expected


def test_cycle_started_and_bounds():
    """A future anchor means 'not started'; bounds are [start, start+30)."""
    assert cycle.cycle_started(ANCHOR, ANCHOR) is True
    assert cycle.cycle_started(ANCHOR, date(2025, 12, 31)) is False
    assert cycle.cycle_bounds(ANCHOR, 1) == (date(2026, 1, 31), date(2026, 3, 2))


def test_day_stamp_and_utc_today_type():
    """Daily sub-cap keys use YYYYMMDD; utc_today returns a date."""
    assert cycle.day_stamp(date(2026, 2, 3)) == "20260203"
    assert isinstance(cycle.utc_today(), date)


def test_override_matching_reserve_infers_provider_only_when_unambiguous():
    """Provider is unknown pre-dispatch: a provider rule matches only if the model has ONE provider."""
    by_user = OverrideRule(id=1, cap_credits=10, user_id=5)
    by_model = OverrideRule(id=2, cap_credits=10, model="gpt-4o")
    by_provider = OverrideRule(id=3, cap_credits=10, provider="openai")
    by_user_provider = OverrideRule(id=4, cap_credits=10, user_id=5, provider="openai")
    only_openai, ambiguous = frozenset({"openai"}), frozenset({"openai", "azure"})
    assert by_user.matches_reserve(5, "x") and not by_user.matches_reserve(6, "x")
    assert by_model.matches_reserve(1, "gpt-4o") and not by_model.matches_reserve(1, "other")
    assert by_model.matches_reserve(1, None) is False
    assert by_provider.matches_reserve(5, "gpt-4o", only_openai)
    assert not by_provider.matches_reserve(5, "gpt-4o")               # no provider info
    assert not by_provider.matches_reserve(5, "gpt-4o", ambiguous)    # model served by several
    assert not by_provider.matches_reserve(5, "gpt-4o", frozenset({"azure"}))
    assert by_user_provider.matches_reserve(5, "gpt-4o", only_openai)
    assert not by_user_provider.matches_reserve(6, "gpt-4o", only_openai)


def test_override_matching_actual_is_exact():
    """At reconcile every set dimension must match the actual provider:model."""
    rule = OverrideRule(id=1, cap_credits=10, user_id=5, model="gpt-4o", provider="openai")
    assert rule.matches_actual(5, "openai", "gpt-4o")
    assert not rule.matches_actual(5, "azure", "gpt-4o")
    assert not rule.matches_actual(6, "openai", "gpt-4o")
    assert OverrideRule(id=2, cap_credits=1, provider="openai").matches_actual(9, "openai", "any")


def test_key_shapes_are_stable():
    """Key names are an operational contract (rebuild, sweeper, Lua) -- pin them."""
    assert keys.user_counter(7, 3) == "wa:quota:user:7:3"
    assert keys.buffer_counter(4, 3) == "wa:quota:team_buffer:4:3"
    assert keys.override_counter(9, 3) == "wa:quota:override:9:3"
    assert keys.daily_counter(7, "20260203") == "wa:quota:userday:7:20260203"
    assert keys.reservation("abc") == "wa:quota:resv:abc"
    assert keys.org_config(2) == "wa:quota:cfg:org:2"
    assert keys.team_config(4) == "wa:quota:cfg:team:4"
    assert keys.OVERRIDE_PREFIX == "wa:quota:override:"
    assert keys.RESERVATION_INDEX == "wa:quota:resv_idx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/quota/test_types_cycle_keys.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/__init__.py`:

```python
"""Elastic pooled token-quota: types, cycle math, Valkey lease/reconcile, engine."""
```

`shared/quota/cycle.py`:

```python
"""Anchored fixed 30-day cycle math (pooled-quota spec D6). All dates are UTC calendar dates."""

from datetime import UTC, date, datetime, timedelta

CYCLE_DAYS = 30


def utc_today() -> date:
    """Today's date in UTC (the only clock the cycle ever reads)."""
    return datetime.now(UTC).date()


def cycle_index(anchor: date, today: date) -> int:
    """Cycle number of ``today`` relative to ``anchor``: ``floor((today - anchor).days / 30)``.

    Floors toward negative infinity, so dates before the anchor map to negative indexes
    deterministically (callers gate on :func:`cycle_started` first).
    """
    return (today - anchor).days // CYCLE_DAYS


def cycle_started(anchor: date, today: date) -> bool:
    """True once ``today`` has reached the anchor date (a future anchor means not yet enforced)."""
    return today >= anchor


def cycle_bounds(anchor: date, index: int) -> tuple[date, date]:
    """``(start, end)`` of cycle ``index``: start inclusive, end exclusive."""
    start = anchor + timedelta(days=index * CYCLE_DAYS)
    return start, start + timedelta(days=CYCLE_DAYS)


def day_stamp(day: date) -> str:
    """``YYYYMMDD`` stamp used by the per-user daily sub-cap counter key."""
    return day.strftime("%Y%m%d")
```

`shared/quota/keys.py`:

```python
"""Valkey key names for the pooled quota (an operational contract: Lua, rebuild, sweeper)."""

PREFIX = "wa:quota"
OVERRIDE_PREFIX = f"{PREFIX}:override:"
RESERVATION_INDEX = f"{PREFIX}:resv_idx"


def user_counter(user_id: int, cycle: int) -> str:
    """Reservation consumption of one user in one cycle."""
    return f"{PREFIX}:user:{user_id}:{cycle}"


def buffer_counter(team_id: int, cycle: int) -> str:
    """Shared-buffer consumption of one team in one cycle."""
    return f"{PREFIX}:team_buffer:{team_id}:{cycle}"


def override_counter(override_id: int, cycle: int) -> str:
    """Consumption counted against one override rule in one cycle."""
    return f"{OVERRIDE_PREFIX}{override_id}:{cycle}"


def daily_counter(user_id: int, day: str) -> str:
    """A user's total credits on one UTC day (``day`` = ``YYYYMMDD``), for the daily sub-cap."""
    return f"{PREFIX}:userday:{user_id}:{day}"


def reservation(reservation_id: str) -> str:
    """The stored reservation record used by reconcile/release."""
    return f"{PREFIX}:resv:{reservation_id}"


def org_config(org_id: int) -> str:
    """Read-through cache of an org's quota config (anchor, enabled, overrides)."""
    return f"{PREFIX}:cfg:org:{org_id}"


def team_config(team_id: int) -> str:
    """Read-through cache of a team's quota config (pool, buffer, reservations)."""
    return f"{PREFIX}:cfg:team:{team_id}"
```

`shared/quota/types.py`:

```python
"""Quota value types shared by the management and proxy planes (pooled-quota spec §5-§7)."""

from dataclasses import dataclass, field
from datetime import date

REASON_EXHAUSTED = "quota_exhausted"
REASON_DAILY = "daily_subcap_exceeded"
REASON_OVERRIDE = "override_exceeded"


@dataclass(slots=True, frozen=True)
class OverrideRule:
    """A hard cap on a user and/or model and/or provider (further-restrict only, spec D5)."""

    id: int
    cap_credits: int
    user_id: int | None = None
    model: str | None = None
    provider: str | None = None

    def matches_reserve(
        self, user_id: int, model: str | None, providers: frozenset[str] = frozenset()
    ) -> bool:
        """Best-effort pre-dispatch match (spec §7): the actual provider is unknown, so a rule
        naming a provider matches only when ``providers`` (those serving the requested model) is
        exactly that provider; otherwise it is enforced at reconcile only.
        """
        if self.provider is not None and providers != frozenset({self.provider}):
            return False
        if self.user_id is not None and self.user_id != user_id:
            return False
        return not (self.model is not None and self.model != model)

    def matches_actual(self, user_id: int, provider: str, model: str) -> bool:
        """Exact match against the actual provider:model once dispatch has happened."""
        return (
            self.user_id in (None, user_id)
            and self.model in (None, model)
            and self.provider in (None, provider)
        )


@dataclass(slots=True, frozen=True)
class UserReservation:
    """A user's private guaranteed credits and optional per-day sub-cap."""

    user_id: int
    guaranteed_credits: int
    daily_subcap: int | None = None


@dataclass(slots=True, frozen=True)
class OrgQuotaConfig:
    """An org's pool switch, cycle anchor, and override rules."""

    org_id: int
    enabled: bool
    cycle_anchor_date: date | None
    pool_credits: int
    overrides: tuple[OverrideRule, ...] = ()


@dataclass(slots=True, frozen=True)
class TeamQuotaConfig:
    """A team's sub-pool, lendable buffer, and per-user reservations (keyed by user id)."""

    team_id: int
    org_id: int
    pool_credits: int
    shared_buffer_credits: int
    reservations: dict[int, UserReservation] = field(default_factory=dict)


@dataclass(slots=True)
class QuotaDecision:
    """Result of one atomic reserve; ``degraded`` means the limiter failed open."""

    allowed: bool
    reason: str | None = None
    reservation_id: str | None = None
    from_reservation: int = 0
    from_buffer: int = 0
    degraded: bool = False


@dataclass(slots=True)
class ReconcileResult:
    """Result of reconciling/releasing one reservation."""

    applied: bool
    from_reservation: int = 0
    from_buffer: int = 0
    overage: int = 0
    over_cap_override_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class QuotaReservation:
    """What the proxy remembers about an admitted request until reconcile (carries the cycle)."""

    reservation_id: str
    org_id: int
    team_id: int
    user_id: int
    cycle: int
    credits_reserved: int
    from_reservation: int
    from_buffer: int
    model_requested: str | None


@dataclass(slots=True)
class QuotaOutcome:
    """What the engine tells the stage: block with a reason, or proceed (reservation or no-op)."""

    blocked: bool
    reason: str | None = None
    reservation: QuotaReservation | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/quota/test_types_cycle_keys.py -v --no-cov`
Expected: PASS (8 tests incl. parametrized).

- [ ] **Step 5: Commit**

```bash
git add shared/quota/__init__.py shared/quota/types.py shared/quota/cycle.py shared/quota/keys.py tests/unit/quota/__init__.py tests/unit/quota/test_types_cycle_keys.py
git commit -m "feat(quota): shared types, anchored 30-day cycle math, Valkey key builders"
git push
```

---
