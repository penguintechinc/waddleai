# Elastic Pooled Quota — Phase 3b: Quota Configuration Service

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5, §8, D3/D4/D5.

Pure database logic: the routes (Tasks 16-19) authorize first, then call it.

---

### Task 14: Quota configuration service (rules, no HTTP)

Pure database logic: the routes (Tasks 16-19) authorize first, then call these. Model after `services/management/app/services/teams.py` (`TeamError`, penguin-dal, no rollback after `IntegrityError`).

Allocation invariants (this plan's reading of spec §5/D3 "pool_credits + shared_buffer_credits"): a team's `pool_credits` is its total allotment; `sum(user guaranteed) + shared_buffer_credits <= pool_credits`; `sum(team pool_credits) <= org pool_credits`. Violations are 409. This keeps reservations truly guaranteed and the org cap real without an org-level runtime counter.

**Files:**
- Create: `services/management/app/services/quota_pools.py`
- Modify: `services/management/app/services/teams.py:133-140` (`delete_team`) and `:280-296` (`remove_member`) — also delete the quota rows
- Test: `tests/unit/management/test_quota_pools_service.py`

**Interfaces:**
- Consumes: `team_world` fixture (`TeamWorld.db/.org/.u/.t`), tables from Task 9.
- Produces (all sync, first arg `database: DB`): `QuotaError(message: str, status: int)`; `MAX_CREDITS = 10**12`; `get_org_pool_row(database, org_id) -> Any | None`; `allocated_to_teams(database, org_id) -> int`; `team_reserved(database, team_id) -> int`; `set_org_pool(database, org_id, *, cycle_anchor_date: date | None, pool_credits: int | None, enabled: bool | None) -> None` (None = unchanged; creates the disabled row if missing); `set_team_pool(database, org_id, team, pool_credits: int) -> None`; `set_team_buffer(database, team, shared_buffer_credits: int) -> None`; `set_user_reservation(database, team, user_id: int, guaranteed_credits: int, daily_subcap: int | None) -> None`; `delete_user_reservation(database, team, user_id: int) -> bool`; `create_override(database, org_id, *, user_id: int | None, model: str | None, provider: str | None, cap_credits: int) -> int`; `delete_override(database, org_id, override_id: int) -> bool`; `list_overrides(database, org_id) -> list[Any]`.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_pools_service.py`:

```python
"""Quota config rules: validation, allocation invariants, upserts, tenancy, cleanup."""

from datetime import date

import pytest

from services.management.app.services import quota_pools as qp
from services.management.app.services import teams as team_svc
from services.management.app.services.quota_pools import QuotaError


def _team(w, key="a1"):
    """The ``teams`` row for a team_world team key."""
    return w.db(w.db.teams.id == w.t[key]).select().first()


def _org_ready(w, pool=1000):
    """Org a with an anchor and an enabled pool of ``pool`` credits."""
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=date(2026, 1, 1), pool_credits=pool, enabled=True)


@pytest.mark.parametrize("bad", [True, -1, "5", 1.5, 10**12 + 1])
def test_credit_values_must_be_bounded_non_negative_integers(team_world, bad):
    """bool/str/float/negative/huge are all refused with 400 (and never persisted)."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=bad, enabled=None)
    assert exc.value.status == 400
    assert int(qp.get_org_pool_row(w.db, w.org["a"]).pool_credits) == 0


def test_enabling_requires_an_anchor_and_all_none_is_a_noop(team_world):
    """Enable without an anchor is 400; setting anchor+enable works; an empty call changes nothing."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=10, enabled=True)
    assert exc.value.status == 400
    _org_ready(w)
    row = qp.get_org_pool_row(w.db, w.org["a"])
    assert (bool(row.enabled), int(row.pool_credits)) == (True, 1000)
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=None, enabled=None)
    assert int(qp.get_org_pool_row(w.db, w.org["a"]).pool_credits) == 1000


def test_org_pool_cannot_drop_below_allocated_team_pools(team_world):
    """Lowering the org pool under the sum of team pools is a 409."""
    w = team_world
    _org_ready(w)
    qp.set_team_pool(w.db, w.org["a"], _team(w), 600)
    with pytest.raises(QuotaError) as exc:
        qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=None, pool_credits=500, enabled=None)
    assert exc.value.status == 409


def test_team_pool_needs_org_pool_and_respects_org_total(team_world):
    """Team pools: org pool must exist & be configured; the sum of team pools stays within it."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w), 10)
    assert exc.value.status == 409
    _org_ready(w, 1000)
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a1"), 700)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 400)
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], _team(w, "a2"), 300)


def test_buffer_and_reservations_share_the_team_pool(team_world):
    """sum(guaranteed) + buffer <= team pool; a team pool cannot drop below what is committed."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    with pytest.raises(QuotaError) as exc:
        qp.set_team_buffer(w.db, team, 10)  # no team pool yet
    assert exc.value.status == 409
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_team_buffer(w.db, team, 30)
    qp.set_user_reservation(w.db, team, w.u["mia"], 50, 20)
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["adam"], 30, None)  # 50+30+30 > 100
    assert exc.value.status == 409
    with pytest.raises(QuotaError) as exc:
        qp.set_team_buffer(w.db, team, 60)  # 50+60 > 100
    assert exc.value.status == 409
    with pytest.raises(QuotaError) as exc:
        qp.set_team_pool(w.db, w.org["a"], team, 70)  # below 50+30 committed
    assert exc.value.status == 409
    assert qp.team_reserved(w.db, team.id) == 50


def test_reservation_upserts_validates_membership_and_deletes(team_world):
    """Second PUT updates the same row; non-members are 404; subcap must be >= 1; delete is idempotent."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_user_reservation(w.db, team, w.u["mia"], 10, None)
    qp.set_user_reservation(w.db, team, w.u["mia"], 25, 5)
    rows = list(w.db(w.db.quota_user_reservation.user_id == w.u["mia"]).select())
    assert [(int(r.guaranteed_credits), int(r.daily_subcap)) for r in rows] == [(25, 5)]
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["nat"], 1, None)  # nat is in team a2
    assert exc.value.status == 404
    with pytest.raises(QuotaError) as exc:
        qp.set_user_reservation(w.db, team, w.u["mia"], 1, 0)
    assert exc.value.status == 400
    assert qp.delete_user_reservation(w.db, team, w.u["mia"]) is True
    assert qp.delete_user_reservation(w.db, team, w.u["mia"]) is False


def test_overrides_need_a_dimension_same_tenant_user_and_are_org_scoped(team_world):
    """No dimension -> 400; user of another org -> 404; delete by another org's admin finds nothing."""
    w = team_world
    with pytest.raises(QuotaError) as exc:
        qp.create_override(w.db, w.org["a"], user_id=None, model=" ", provider=None, cap_credits=10)
    assert exc.value.status == 400
    with pytest.raises(QuotaError) as exc:
        qp.create_override(w.db, w.org["a"], user_id=w.u["carl"], model=None, provider=None, cap_credits=10)
    assert exc.value.status == 404
    oid = qp.create_override(w.db, w.org["a"], user_id=w.u["mia"], model=None, provider="openai", cap_credits=10)
    assert [int(r.id) for r in qp.list_overrides(w.db, w.org["a"])] == [oid]
    assert qp.list_overrides(w.db, w.org["b"]) == []
    assert qp.delete_override(w.db, w.org["b"], oid) is False
    assert qp.delete_override(w.db, w.org["a"], oid) is True


def test_removing_a_member_or_deleting_a_team_cleans_quota_rows(team_world):
    """A removed member keeps no stale reservation; a deleted team leaves no pool/reservation rows."""
    w = team_world
    _org_ready(w)
    team = _team(w)
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_user_reservation(w.db, team, w.u["mia"], 10, None)
    team_svc.remove_member(w.db, team, w.u["mia"], can_manage_owner=True)
    assert list(w.db(w.db.quota_user_reservation.user_id == w.u["mia"]).select()) == []
    team_svc.delete_team(w.db, team)
    assert list(w.db(w.db.quota_team_pool.team_id == w.t["a1"]).select()) == []
```
- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_pools_service.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'quota_pools'`.

- [ ] **Step 3: Write minimal implementation**

`services/management/app/services/quota_pools.py`:

```python
"""Quota configuration business rules (pooled-quota spec §5, §8; D3/D4/D5/D11).

Pure database logic with no HTTP: the routes authorize first, then call these. Allocation
invariants: ``sum(user guaranteed) + team buffer <= team pool_credits`` and
``sum(team pool_credits) <= org pool_credits`` -- so reservations are truly guaranteed and the
org cap is real without an org-level runtime counter. penguin-dal runs each statement in its own
transaction (no ``rollback()``), so a caught ``IntegrityError`` needs no cleanup.
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
        int(r.guaranteed_credits or 0) for r in rows if exclude_user_id is None or r.user_id != exclude_user_id
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
        database.quota_org_pool.insert(org_id=org_id, pool_credits=0, enabled=False)
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
    org_row = get_org_pool_row(database, org_id)
    if org_row is None:
        raise QuotaError("Configure the organization pool first", 409)
    credits = _credits(pool_credits, "pool_credits")
    row = _team_pool_row(database, team.id)
    others = allocated_to_teams(database, org_id) - (int(row.pool_credits or 0) if row else 0)
    if others + credits > int(org_row.pool_credits or 0):
        raise QuotaError("pool_credits exceeds the organization pool", 409)
    committed = team_reserved(database, team.id) + (int(row.shared_buffer_credits or 0) if row else 0)
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
```

In `services/management/app/services/teams.py` `delete_team`, before `database(database.teams.id == team.id).delete()` add:

```python
    database(database.quota_user_reservation.team_id == team.id).delete()
    database(database.quota_team_pool.team_id == team.id).delete()
```

and in `remove_member`, before `database(database.team_members.id == member.id).delete()` add:

```python
    database(
        (database.quota_user_reservation.team_id == team.id)
        & (database.quota_user_reservation.user_id == user_id)
    ).delete()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_pools_service.py tests/unit/management/test_team_service.py tests/unit/management/test_team_service_roles.py -v --no-cov`
Expected: PASS. Existing team-service tests run against `team_world` (full `create_all` schema), so the extra deletes are safe.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/quota_pools.py services/management/app/services/teams.py tests/unit/management/test_quota_pools_service.py
git commit -m "feat(quota): config service — pools, buffer, reservations, overrides with allocation invariants"
git push
```

---
