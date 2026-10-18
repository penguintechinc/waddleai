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
