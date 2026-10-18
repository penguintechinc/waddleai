"""Quota configuration business rules (pooled-quota spec §5, §8; D3/D4/D5/D11).

Pure database logic with no HTTP: the routes authorize first, then call these. Allocation
invariants: ``sum(user guaranteed) + team buffer <= team pool_credits`` and
``sum(team pool_credits) <= org pool_credits``. They are enforced best-effort at config time,
last-writer-wins: penguin-dal has no multi-statement transaction, so two concurrent admin writes
can jointly over-allocate; the next write touching that pool re-validates and answers 409 until
it is shrunk, and the runtime lease remains the real spend guard. A caught ``IntegrityError``
needs no cleanup (each statement is its own transaction, no ``rollback()``).
"""

from datetime import date
from typing import Any

from penguin_dal.db import DB
from sqlalchemy.exc import IntegrityError

MAX_CREDITS = 10**12
_MAX_MODEL = 255
_MAX_PROVIDER = 100


class QuotaError(Exception):
    """A quota rule violation; carries the HTTP status the route should answer with."""

    def __init__(self, message: str, status: int) -> None:
        """Record the user-facing message and the HTTP status to answer with."""
        super().__init__(message)
        self.message = message
        self.status = status


def _credits(value: object, name: str, *, minimum: int = 0) -> int:
    """Validate a credit amount: a real int (not bool) within ``[minimum, MAX_CREDITS]``."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_CREDITS:
        raise QuotaError(f"{name} must be an integer between {minimum} and {MAX_CREDITS}", 400)
    return value


def get_org_pool_row(database: DB, org_id: int) -> Any:
    """Return the org's ``quota_org_pool`` row, or ``None``."""
    return database(database.quota_org_pool.org_id == org_id).select().first()


def _team_pool_row(database: DB, team_id: int) -> Any:
    """Return the team's ``quota_team_pool`` row, or ``None``."""
    return database(database.quota_team_pool.team_id == team_id).select().first()


def allocated_to_teams(database: DB, org_id: int) -> int:
    """Sum of every team pool in the org (what the org pool has already handed out)."""
    rows = database(database.quota_team_pool.organization_id == org_id).select()
    return sum(int(r.pool_credits or 0) for r in rows)


def team_reserved(database: DB, team_id: int, exclude_user_id: int | None = None) -> int:
    """Sum of guaranteed credits across the team's reservations, optionally excluding one user."""
    rows = database(database.quota_user_reservation.team_id == team_id).select()
    return sum(
        int(r.guaranteed_credits or 0)
        for r in rows
        if exclude_user_id is None or r.user_id != exclude_user_id
    )


def set_org_pool(
    database: DB,
    org_id: int,
    *,
    cycle_anchor_date: date | None,
    pool_credits: int | None,
    enabled: bool | None,
) -> None:
    """Update the org pool; ``None`` leaves a field unchanged. Enabling needs an anchor date."""
    row = get_org_pool_row(database, org_id)
    if row is None:
        try:
            database.quota_org_pool.insert(org_id=org_id, pool_credits=0, enabled=False)
        except IntegrityError as exc:
            raise QuotaError("Organization pool was created concurrently; retry", 409) from exc
        row = get_org_pool_row(database, org_id)
    fields: dict[str, Any] = {}
    if pool_credits is not None:
        credits = _credits(pool_credits, "pool_credits")
        if credits < allocated_to_teams(database, org_id):
            raise QuotaError("pool_credits is below the credits already allocated to teams", 409)
        fields["pool_credits"] = credits
    if cycle_anchor_date is not None:
        fields["cycle_anchor_date"] = cycle_anchor_date
    if enabled is not None:
        fields["enabled"] = enabled
    anchor = fields.get("cycle_anchor_date", row.cycle_anchor_date)
    if fields.get("enabled", bool(row.enabled)) and anchor is None:
        raise QuotaError("cycle_anchor_date is required to enable the pool", 400)
    if fields:
        database(database.quota_org_pool.id == row.id).update(**fields)


def set_team_pool(database: DB, org_id: int, team: Any, pool_credits: int) -> None:
    """Allocate ``pool_credits`` of the org pool to ``team`` (org-layer operation)."""
    if team.organization_id != org_id:
        raise QuotaError("Team not found", 404)
    org_row = get_org_pool_row(database, org_id)
    if org_row is None:
        raise QuotaError("Configure the organization pool first", 409)
    credits = _credits(pool_credits, "pool_credits")
    row = _team_pool_row(database, team.id)
    others = allocated_to_teams(database, org_id) - (int(row.pool_credits or 0) if row else 0)
    if others + credits > int(org_row.pool_credits or 0):
        raise QuotaError("pool_credits exceeds the organization pool", 409)
    committed = team_reserved(database, team.id) + (
        int(row.shared_buffer_credits or 0) if row else 0
    )
    if credits < committed:
        raise QuotaError("pool_credits is below the reservations and buffer already committed", 409)
    if row is None:
        try:
            database.quota_team_pool.insert(
                team_id=team.id, organization_id=team.organization_id,
                pool_credits=credits, shared_buffer_credits=0,
            )  # fmt: skip
        except IntegrityError as exc:
            raise QuotaError("Team pool was created concurrently; retry", 409) from exc
    else:
        database(database.quota_team_pool.id == row.id).update(pool_credits=credits)


def set_team_buffer(database: DB, team: Any, shared_buffer_credits: int) -> None:
    """Set the team's lendable shared buffer (team-admin operation); needs an allocated pool."""
    row = _team_pool_row(database, team.id)
    if row is None:
        raise QuotaError("No pool is allocated to this team; ask an organization admin", 409)
    buffer = _credits(shared_buffer_credits, "shared_buffer_credits")
    if team_reserved(database, team.id) + buffer > int(row.pool_credits or 0):
        raise QuotaError("reservations plus buffer would exceed the team pool", 409)
    database(database.quota_team_pool.id == row.id).update(shared_buffer_credits=buffer)


def set_user_reservation(
    database: DB, team: Any, user_id: int, guaranteed_credits: int, daily_subcap: int | None
) -> None:
    """Upsert a member's guaranteed credits (+ optional daily sub-cap) within the team pool."""
    pool = _team_pool_row(database, team.id)
    if pool is None:
        raise QuotaError("No pool is allocated to this team; ask an organization admin", 409)
    member = (
        database(
            (database.team_members.team_id == team.id) & (database.team_members.user_id == user_id)
        )
        .select()
        .first()
    )
    if member is None:
        raise QuotaError("User is not a member of this team", 404)
    guaranteed = _credits(guaranteed_credits, "guaranteed_credits")
    subcap = None if daily_subcap is None else _credits(daily_subcap, "daily_subcap", minimum=1)
    total = team_reserved(database, team.id, exclude_user_id=user_id) + guaranteed
    if total + int(pool.shared_buffer_credits or 0) > int(pool.pool_credits or 0):
        raise QuotaError("reservations plus buffer would exceed the team pool", 409)
    query = (database.quota_user_reservation.user_id == user_id) & (
        database.quota_user_reservation.organization_id == team.organization_id
    )
    values = {"team_id": team.id, "guaranteed_credits": guaranteed, "daily_subcap": subcap}
    if database(query).update(**values):
        return
    try:
        database.quota_user_reservation.insert(
            user_id=user_id, organization_id=team.organization_id, **values
        )
    except IntegrityError as exc:
        raise QuotaError("Reservation was created concurrently; retry", 409) from exc


def delete_user_reservation(database: DB, team: Any, user_id: int) -> bool:
    """Delete a member's reservation; True when one existed."""
    query = (database.quota_user_reservation.team_id == team.id) & (
        database.quota_user_reservation.user_id == user_id
    )
    return bool(database(query).delete())


def _clean_dim(value: object, name: str, limit: int) -> str | None:
    """Trim an optional dimension string; empty -> None; over-long -> 400."""
    cleaned = value.strip() if isinstance(value, str) else None
    if cleaned and len(cleaned) > limit:
        raise QuotaError(f"{name} must be at most {limit} characters", 400)
    return cleaned or None


def create_override(
    database: DB,
    org_id: int,
    *,
    user_id: int | None,
    model: str | None,
    provider: str | None,
    cap_credits: int,
) -> int:
    """Create an override cap on a user and/or model and/or provider; returns its id."""
    model = _clean_dim(model, "model", _MAX_MODEL)
    provider = _clean_dim(provider, "provider", _MAX_PROVIDER)
    if user_id is None and model is None and provider is None:
        raise QuotaError("An override needs at least one of user_id, model, provider", 400)
    cap = _credits(cap_credits, "cap_credits", minimum=1)
    if user_id is not None:
        owner = (
            database((database.users.id == user_id) & (database.users.organization_id == org_id))
            .select()
            .first()
        )
        if owner is None:
            raise QuotaError("User not found", 404)
    return int(
        database.quota_override.insert(
            organization_id=org_id, user_id=user_id, model=model, provider=provider, cap_credits=cap
        )
    )


def delete_override(database: DB, org_id: int, override_id: int) -> bool:
    """Delete an override of this org; False when it does not exist in this tenant."""
    query = (database.quota_override.id == override_id) & (
        database.quota_override.organization_id == org_id
    )
    return bool(database(query).delete())


def list_overrides(database: DB, org_id: int) -> list[Any]:
    """All override rows of the org, oldest first."""
    return list(
        database(database.quota_override.organization_id == org_id).select(
            orderby=database.quota_override.id
        )
    )
