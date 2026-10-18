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
