"""Cycle math (spec D6), override matching (spec §7) and Valkey key shapes."""

from datetime import date

import pytest

from shared.quota import cycle, keys
from shared.quota.types import OverrideRule

ANCHOR = date(2026, 1, 1)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), 0),  # anchor day
        (date(2026, 1, 30), 0),  # day 29: last day of cycle 0
        (date(2026, 1, 31), 1),  # day 30: exact boundary starts cycle 1
        (date(2026, 3, 2), 2),  # day 60
        (date(2025, 12, 31), -1),  # before the anchor floors negative, deterministically
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
    """Provider is unknown pre-dispatch: provider rules match only if the model has ONE provider."""
    by_user = OverrideRule(id=1, cap_credits=10, user_id=5)
    by_model = OverrideRule(id=2, cap_credits=10, model="gpt-4o")
    by_provider = OverrideRule(id=3, cap_credits=10, provider="openai")
    by_user_provider = OverrideRule(id=4, cap_credits=10, user_id=5, provider="openai")
    only_openai, ambiguous = frozenset({"openai"}), frozenset({"openai", "azure"})
    assert by_user.matches_reserve(5, "x") and not by_user.matches_reserve(6, "x")
    assert by_model.matches_reserve(1, "gpt-4o") and not by_model.matches_reserve(1, "other")
    assert by_model.matches_reserve(1, None) is False
    assert by_provider.matches_reserve(5, "gpt-4o", only_openai)
    assert not by_provider.matches_reserve(5, "gpt-4o")  # no provider info
    assert not by_provider.matches_reserve(5, "gpt-4o", ambiguous)  # model served by several
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
