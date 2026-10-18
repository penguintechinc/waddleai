# Elastic Pooled Quota — Phase 3d: Override Routes + Team-Layer Routes

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §8, D5, D11.

Team-layer routes authorize through `_team_authz.authorize_team` (live membership, same as the teams routes) with the Task 13 scopes `team:quota_read` / `team:quota_admin`; the org admin passes via `team:org_admin`. They carry no `require_scope`, so `_EXPECTED_ROUTE_COUNT` is unchanged by Task 18.

---

---

### Task 17: Override cap routes

**Files:**
- Modify: `services/management/app/api/v1/quota_pools.py` (append DTOs + 3 routes; add `from ._pagination import PageRequest` to the imports)
- Modify: `tests/unit/management/test_scope_authz.py` (`_EXPECTED_ROUTE_COUNT = 120`, comment `+3 pooled-quota overrides`)
- Test: `tests/unit/management/test_quota_override_routes.py`

**Interfaces:**
- Consumes: Task 14 `create_override`, `delete_override`, `list_overrides`; Task 16 helpers; `PageRequest.from_request()` with `.limitby` (start, stop) and `.meta(total)` (`tests`/teams usage: `**page.meta(total)` spreads a `pagination` key).
- Produces: `GET /pooled-quota/overrides` (paginated), `POST /pooled-quota/overrides` (201), `DELETE /pooled-quota/overrides/<int:override_id>`; DTOs `CreateOverrideRequest(user_id: int | None, model: str | None, provider: str | None, cap_credits: int | None)`, `OverrideItem(id, user_id, model, provider, cap_credits)`, `OverrideListResponse(overrides, total, pagination)`, `CreateOverrideResponse(id, message)`. Every write invalidates the org config blob (`invalidate_quota_config(org_id=...)`).

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_override_routes.py`:

```python
"""Override cap routes: validation, tenancy, response shape, invalidation."""

import importlib
from unittest.mock import AsyncMock

import pytest

ROUTES = importlib.import_module("services.management.app.api.v1.quota_pools")
URL = "/api/v1/pooled-quota/overrides"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def inval(monkeypatch):
    """Awaitable mock standing in for proxy-cache invalidation."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(ROUTES, "invalidate_quota_config", mock)
    return mock


async def test_create_list_delete_round_trip(real_db_client, team_headers, team_world, inval):
    """POST 201 -> GET shows exactly the DTO fields -> DELETE 200 -> gone; each write invalidates the org."""
    w, h = team_world, team_headers("boss")
    created = await real_db_client.post(URL, headers=h, json={"user_id": w.u["mia"], "cap_credits": 50})
    assert created.status_code == 201
    oid = (await created.get_json())["id"]
    inval.assert_awaited_with(org_id=w.org["a"])
    listing = await (await real_db_client.get(URL, headers=h)).get_json()
    assert listing["total"] == 1
    assert listing["overrides"] == [
        {"id": oid, "user_id": w.u["mia"], "model": None, "provider": None, "cap_credits": 50}
    ]
    assert (await real_db_client.delete(f"{URL}/{oid}", headers=h)).status_code == 200
    assert (await real_db_client.delete(f"{URL}/{oid}", headers=h)).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"cap_credits": 10},                                  # no dimension
        {"model": "gpt-4o"},                                  # no cap
        {"model": "gpt-4o", "cap_credits": 0},                # cap must be >= 1
        {"model": "x" * 300, "cap_credits": 5},               # over-long
    ],
)
async def test_create_rejects_bad_input(real_db_client, team_headers, payload):
    """Missing dimension/cap, zero cap, and over-long strings are 400."""
    assert (await real_db_client.post(URL, headers=team_headers("boss"), json=payload)).status_code == 400


async def test_cross_tenant_user_is_404_and_other_orgs_overrides_invisible(real_db_client, team_headers, team_world):
    """A user of another org cannot be capped; an org-b admin-less view never lists org-a rows."""
    w = team_world
    resp = await real_db_client.post(URL, headers=team_headers("boss"), json={"user_id": w.u["carl"], "cap_credits": 5})
    assert resp.status_code == 404
    await real_db_client.post(URL, headers=team_headers("boss"), json={"provider": "openai", "cap_credits": 5})
    other = w.db.quota_override.insert(organization_id=w.org["b"], provider="x", cap_credits=1)
    listing = await (await real_db_client.get(URL, headers=team_headers("boss"))).get_json()
    assert [o["provider"] for o in listing["overrides"]] == ["openai"]
    assert (await real_db_client.delete(f"{URL}/{int(other)}", headers=team_headers("boss"))).status_code == 404


async def test_member_cannot_manage_overrides(real_db_client, team_headers):
    """The org scope is required; a team Owner without it is 403."""
    assert (await real_db_client.get(URL, headers=team_headers("olive"))).status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_override_routes.py -v --no-cov`
Expected: FAIL — 404/405 (routes missing).

- [ ] **Step 3: Write minimal implementation**

Append to `quota_pools.py` (and add `from ._pagination import PageRequest` after the `from . import api_v1_bp` line):

```python
@dataclass(slots=True)
class CreateOverrideRequest:
    """Request body for POST /pooled-quota/overrides (tenant comes from the token)."""

    user_id: int | None = None
    model: str | None = None
    provider: str | None = None
    cap_credits: int | None = None


@dataclass(slots=True)
class OverrideItem:
    """An override as returned by the API -- ids, dimensions and the cap only."""

    id: int
    user_id: int | None
    model: str | None
    provider: str | None
    cap_credits: int


@dataclass(slots=True)
class OverrideListResponse:
    """Response body for GET /pooled-quota/overrides."""

    overrides: list[OverrideItem]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class CreateOverrideResponse:
    """Response body for POST /pooled-quota/overrides."""

    id: int
    message: str


def _override_item(row: Any) -> dict[str, Any]:
    """Project a ``quota_override`` row onto :class:`OverrideItem`."""
    return {
        "id": int(row.id),
        "user_id": None if row.user_id is None else int(row.user_id),
        "model": row.model or None,
        "provider": row.provider or None,
        "cap_credits": int(row.cap_credits),
    }


@api_v1_bp.route("/pooled-quota/overrides", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(OverrideListResponse, 200)
async def list_override_caps():
    """List the caller's org override caps (bounded window)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    page = PageRequest.from_request()
    rows = await asyncio.to_thread(qp.list_overrides, _db(), org_id)
    start, stop = page.limitby
    return {"overrides": [_override_item(r) for r in rows[start:stop]], "total": len(rows), **page.meta(len(rows))}


@api_v1_bp.route("/pooled-quota/overrides", methods=["POST"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(CreateOverrideResponse, 201)
@validate_request(CreateOverrideRequest)
async def create_override_cap(data: CreateOverrideRequest):
    """Create a hard cap on a user and/or model and/or provider (further-restrict only)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    if data.cap_credits is None:
        return jsonify({"error": "cap_credits is required"}), 400
    try:
        override_id = await asyncio.to_thread(
            lambda: qp.create_override(
                _db(), org_id, user_id=data.user_id, model=data.model,
                provider=data.provider, cap_credits=data.cap_credits,
            )
        )  # fmt: skip
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(org_id=org_id)
    return {"id": override_id, "message": "Override created"}, 201


@api_v1_bp.route("/pooled-quota/overrides/<int:override_id>", methods=["DELETE"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
async def delete_override_cap(override_id: int):
    """Delete an override of the caller's own org (another tenant's id is a 404)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    deleted = await asyncio.to_thread(qp.delete_override, _db(), org_id, override_id)
    if not deleted:
        return jsonify({"error": "Override not found"}), 404
    await invalidate_quota_config(org_id=org_id)
    return {"message": "Override deleted"}
```

Set `_EXPECTED_ROUTE_COUNT = 120` in `test_scope_authz.py` with the comment line `+3 pooled-quota overrides`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_override_routes.py tests/unit/management/test_quota_org_routes.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/quota_pools.py tests/unit/management/test_quota_override_routes.py tests/unit/management/test_scope_authz.py
git commit -m "feat(quota): override cap management routes"
git push
```

### Task 18: Team-layer routes — view quota, set buffer, set/delete reservations

**Files:**
- Create: `services/management/app/api/v1/team_quota.py`
- Modify: `services/management/app/api/v1/__init__.py` (append `team_quota,` after `quota_pools,`)
- Modify: `tests/unit/management/team_fixtures.py` (`_REAL_DB_ROUTE_MODULES` gains `"team_quota"`)
- Test: `tests/unit/management/test_team_quota_routes.py`

**Interfaces:**
- Consumes: `authorize_team(database, team_id, permission) -> (TeamAccess | None, ErrorResponse | None)` (`TeamAccess.team` is the `teams` row); Task 14 service (`set_team_buffer`, `set_user_reservation`, `delete_user_reservation`, `team_reserved`, `QuotaError`); Task 16 helpers imported from `.quota_pools`: `MessageResponse`, `PaginationMeta`, `_BEARER_AUTH`, `_db`, `_error`, `_quota_off`; Task 15 `invalidate_quota_config`.
- Produces: `GET /api/v1/teams/<int:team_id>/pooled-quota` (`team:quota_read`), `PUT .../pooled-quota/buffer` (`team:quota_admin`), `PUT|DELETE .../pooled-quota/reservations/<int:user_id>` (`team:quota_admin`); DTOs `SetBufferRequest(shared_buffer_credits: int | None)`, `SetReservationRequest(guaranteed_credits: int | None, daily_subcap: int | None)`, `ReservationItem(user_id, guaranteed_credits, daily_subcap)`, `TeamQuotaResponse(team_id, configured, pool_credits, shared_buffer_credits, reserved_credits, unallocated_credits, reservations, total, pagination)`. Task 24 extends `TeamQuotaResponse` with usage fields.

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_team_quota_routes.py`:

```python
"""Team-layer quota routes: live-membership authz, tenancy, invariants, invalidation."""

import importlib
from datetime import date
from unittest.mock import AsyncMock

import pytest

from services.management.app.services import quota_pools as qp

ROUTES = importlib.import_module("services.management.app.api.v1.team_quota")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def inval(monkeypatch):
    """Awaitable mock standing in for proxy-cache invalidation."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(ROUTES, "invalidate_quota_config", mock)
    return mock


@pytest.fixture
def pooled(team_world):
    """Org a with an enabled 1000-credit pool, team a1 allocated 100 with a 10-credit buffer."""
    w = team_world
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True)
    team = w.db(w.db.teams.id == w.t["a1"]).select().first()
    qp.set_team_pool(w.db, w.org["a"], team, 100)
    qp.set_team_buffer(w.db, team, 10)
    return w


def _url(w, tail="", key="a1"):
    """Route URL for a team_world team."""
    return f"/api/v1/teams/{w.t[key]}/pooled-quota{tail}"


async def test_get_shape_for_viewer_and_unconfigured_team(real_db_client, team_headers, pooled):
    """Viewer can read; the DTO has exactly the spec fields; an unallocated team reads as unconfigured zeros."""
    w = pooled
    body = await (await real_db_client.get(_url(w), headers=team_headers("vic"))).get_json()
    assert set(body) == {"team_id", "configured", "pool_credits", "shared_buffer_credits",
                         "reserved_credits", "unallocated_credits", "reservations", "total", "pagination"}  # fmt: skip
    assert (body["configured"], body["pool_credits"], body["shared_buffer_credits"], body["unallocated_credits"]) == (True, 100, 10, 90)
    empty = await (await real_db_client.get(_url(w, key="a2"), headers=team_headers("boss"))).get_json()
    assert (empty["configured"], empty["pool_credits"], empty["unallocated_credits"]) == (False, 0, 0)


async def test_authz_matrix(real_db_client, team_headers, pooled):
    """nat (other team) 403, carl (other tenant) 404, flag-off 404, anonymous 401."""
    w = pooled
    assert (await real_db_client.get(_url(w), headers=team_headers("nat"))).status_code == 403
    assert (await real_db_client.get(_url(w), headers=team_headers("carl"))).status_code == 404
    assert (await real_db_client.get(_url(w))).status_code == 401


async def test_flag_off_is_404(real_db_client, team_headers, pooled, monkeypatch):
    """Flag OFF hides the team routes too."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    assert (await real_db_client.get(_url(pooled), headers=team_headers("olive"))).status_code == 404


async def test_buffer_edit_needs_team_admin_and_respects_the_pool(real_db_client, team_headers, pooled, inval):
    """Admin/owner 200 (drops the team blob); member 403; over-pool 409; missing field 400."""
    w = pooled
    url = _url(w, "/buffer")
    assert (await real_db_client.put(url, headers=team_headers("adam"), json={"shared_buffer_credits": 20})).status_code == 200
    inval.assert_awaited_once_with(team_id=w.t["a1"])
    assert (await real_db_client.put(url, headers=team_headers("mia"), json={"shared_buffer_credits": 5})).status_code == 403
    assert (await real_db_client.put(url, headers=team_headers("olive"), json={"shared_buffer_credits": 101})).status_code == 409
    assert (await real_db_client.put(url, headers=team_headers("olive"), json={})).status_code == 400


async def test_reservation_crud_and_membership(real_db_client, team_headers, pooled, inval):
    """PUT upserts (listed by GET), non-member 404, member forbidden, DELETE then 404."""
    w = pooled
    url = _url(w, f"/reservations/{w.u['mia']}")
    body = {"guaranteed_credits": 40, "daily_subcap": 15}
    assert (await real_db_client.put(url, headers=team_headers("adam"), json=body)).status_code == 200
    got = await (await real_db_client.get(_url(w), headers=team_headers("adam"))).get_json()
    assert got["reservations"] == [{"user_id": w.u["mia"], "guaranteed_credits": 40, "daily_subcap": 15}]
    assert got["reserved_credits"] == 40 and got["unallocated_credits"] == 50
    assert (await real_db_client.put(url, headers=team_headers("mia"), json=body)).status_code == 403
    nat = _url(w, f"/reservations/{w.u['nat']}")
    assert (await real_db_client.put(nat, headers=team_headers("adam"), json=body)).status_code == 404
    assert (await real_db_client.put(url, headers=team_headers("adam"), json={"daily_subcap": 3})).status_code == 400
    assert (await real_db_client.delete(url, headers=team_headers("adam"))).status_code == 200
    assert (await real_db_client.delete(url, headers=team_headers("adam"))).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_team_quota_routes.py -v --no-cov`
Expected: FAIL — routes 404/405 (module missing).

- [ ] **Step 3: Write minimal implementation**

`services/management/app/api/v1/team_quota.py`:

```python
"""WaddleAI Management API v1 - Pooled quota, team layer (pooled-quota spec §8, D11).

View a team's quota, set its shared buffer, and manage per-user reservations. Authorization
re-derives the caller's team permissions from the LIVE membership row via ``authorize_team``
(a demotion bites immediately); the org admin passes via ``team:org_admin``. Behind the
``waddleai.pooled_quota`` flag. Responses carry user ids and credits only -- no PII.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from quart import jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission

from ...services import quota_pools as qp
from ...services.quota_invalidation import invalidate_quota_config
from ...services.quota_pools import QuotaError
from . import api_v1_bp
from ._pagination import PageRequest
from ._team_authz import authorize_team
from .auth import require_auth
from .quota_pools import (
    _BEARER_AUTH,
    MessageResponse,
    PaginationMeta,
    _db,
    _error,
    _quota_off,
)


@dataclass(slots=True)
class SetBufferRequest:
    """Request body for PUT /teams/<id>/pooled-quota/buffer."""

    shared_buffer_credits: int | None = None


@dataclass(slots=True)
class SetReservationRequest:
    """Request body for PUT /teams/<id>/pooled-quota/reservations/<user_id>."""

    guaranteed_credits: int | None = None
    daily_subcap: int | None = None


@dataclass(slots=True)
class ReservationItem:
    """A member's reservation: the user id (never a name or email) and credits."""

    user_id: int
    guaranteed_credits: int
    daily_subcap: int | None


@dataclass(slots=True)
class TeamQuotaResponse:
    """A team's quota as returned by the API -- exactly these fields, never the raw rows."""

    team_id: int
    configured: bool
    pool_credits: int
    shared_buffer_credits: int
    reserved_credits: int
    unallocated_credits: int
    reservations: list[ReservationItem]
    total: int
    pagination: PaginationMeta


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(TeamQuotaResponse, 200)
async def get_team_quota(team_id: int):
    """Show a team's pool, buffer, reserved/unallocated credits and per-user reservations."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_READ)
    if error is not None or access is None:
        return error
    page = PageRequest.from_request()
    database = _db()

    def _load() -> dict[str, Any]:
        pool = database(database.quota_team_pool.team_id == team_id).select().first()
        rows = list(
            database(database.quota_user_reservation.team_id == team_id).select(
                orderby=database.quota_user_reservation.id
            )
        )
        items = [
            {"user_id": int(r.user_id), "guaranteed_credits": int(r.guaranteed_credits or 0),
             "daily_subcap": None if r.daily_subcap is None else int(r.daily_subcap)}
            for r in rows
        ]  # fmt: skip
        reserved = sum(i["guaranteed_credits"] for i in items)
        pool_credits = int(pool.pool_credits or 0) if pool else 0
        buffer = int(pool.shared_buffer_credits or 0) if pool else 0
        start, stop = page.limitby
        return {
            "team_id": team_id, "configured": pool is not None, "pool_credits": pool_credits,
            "shared_buffer_credits": buffer, "reserved_credits": reserved,
            "unallocated_credits": max(0, pool_credits - reserved - buffer),
            "reservations": items[start:stop], "total": len(items), **page.meta(len(items)),
        }  # fmt: skip

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/buffer", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(SetBufferRequest)
async def put_team_buffer(team_id: int, data: SetBufferRequest):
    """Set the team's lendable shared buffer (team Admin+ or org admin)."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    if data.shared_buffer_credits is None:
        return jsonify({"error": "shared_buffer_credits is required"}), 400
    try:
        await asyncio.to_thread(
            lambda: qp.set_team_buffer(_db(), access.team, data.shared_buffer_credits)
        )
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    return {"message": "Team buffer updated"}


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/reservations/<int:user_id>", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
@validate_request(SetReservationRequest)
async def put_team_reservation(team_id: int, user_id: int, data: SetReservationRequest):
    """Upsert a member's guaranteed credits and optional daily sub-cap (team Admin+)."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    if data.guaranteed_credits is None:
        return jsonify({"error": "guaranteed_credits is required"}), 400
    try:
        await asyncio.to_thread(
            lambda: qp.set_user_reservation(
                _db(), access.team, user_id, data.guaranteed_credits, data.daily_subcap
            )
        )
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    return {"message": "Reservation updated"}


@api_v1_bp.route("/teams/<int:team_id>/pooled-quota/reservations/<int:user_id>", methods=["DELETE"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@validate_response(MessageResponse, 200)
async def delete_team_reservation(team_id: int, user_id: int):
    """Delete a member's reservation (team Admin+); 404 when none exists."""
    if (off := await _quota_off()) is not None:
        return off
    access, error = await authorize_team(_db(), team_id, Permission.TEAM_QUOTA_ADMIN)
    if error is not None or access is None:
        return error
    deleted = await asyncio.to_thread(qp.delete_user_reservation, _db(), access.team, user_id)
    if not deleted:
        return jsonify({"error": "Reservation not found"}), 404
    await invalidate_quota_config(team_id=team_id)
    return {"message": "Reservation deleted"}
```

Append `team_quota,` to the import tuple in `api/v1/__init__.py`; add `"team_quota"` to `_REAL_DB_ROUTE_MODULES`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_team_quota_routes.py tests/unit/management/test_quota_org_routes.py tests/unit/management/test_teams_routes.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/team_quota.py services/management/app/api/v1/__init__.py tests/unit/management/team_fixtures.py tests/unit/management/test_team_quota_routes.py
git commit -m "feat(quota): team-layer routes — view, buffer, per-user reservations"
git push
```

---
