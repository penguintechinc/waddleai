# Elastic Pooled Quota — Phase 3c: Cache Invalidation + Org-Layer Routes

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §6, §8, D11, D12.

Org-layer routes live in `services/management/app/api/v1/quota_pools.py`, gated by `require_scope(Permission.QUOTA_ORG_UPDATE)` (admin-only org scope, reused per D11). Every route is behind the `waddleai.pooled_quota` flag (404 when OFF, `distinct_id` = the caller's org id) so an org can be configured as part of its per-org rollout, and every response goes through a dataclass DTO (ids + credits only).

The route-scope guard `tests/unit/management/test_scope_authz.py` counts every `require_scope` route (`_EXPECTED_ROUTE_COUNT`, currently 114): each task below bumps it by the number of scoped routes it adds (final value after Task 24: 124).

---

---

### Task 15: Proxy-cache invalidation on quota config edits

Teams `auth_cache_invalidation` pattern: delete the proxy's Valkey config blobs (best-effort, TTL-bounded).

**Files:**
- Create: `services/management/app/services/quota_invalidation.py`
- Test: `tests/unit/management/test_quota_invalidation.py`

**Interfaces:**
- Consumes: `shared.quota.keys.org_config/team_config`; `services.management.app.extensions.redis_client` (sync client, may be `None`).
- Produces: `async invalidate_quota_config(*, org_id: int | None = None, team_id: int | None = None) -> int` — deletes the targeted blobs, returns how many keys were targeted (0 when both are None), never raises (missing client or Valkey error is logged at WARNING; the proxy TTL bounds staleness).

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_invalidation.py`:

```python
"""Quota config invalidation is best-effort: it deletes the right keys and never raises."""

from unittest.mock import MagicMock

from services.management.app import extensions
from services.management.app.services.quota_invalidation import invalidate_quota_config
from shared.quota import keys


async def test_deletes_exactly_the_targeted_blobs(monkeypatch):
    """org + team ids map to their two cache keys in one DELETE."""
    client = MagicMock()
    monkeypatch.setattr(extensions, "redis_client", client)
    assert await invalidate_quota_config(org_id=2, team_id=5) == 2
    client.delete.assert_called_once_with(keys.org_config(2), keys.team_config(5))


async def test_nothing_to_do_touches_nothing(monkeypatch):
    """No ids -> 0 targeted and the client is never used."""
    client = MagicMock()
    monkeypatch.setattr(extensions, "redis_client", client)
    assert await invalidate_quota_config() == 0
    client.delete.assert_not_called()


async def test_missing_client_is_logged_not_raised(monkeypatch, caplog):
    """No cache client: report the count, warn, rely on the proxy TTL."""
    monkeypatch.setattr(extensions, "redis_client", None)
    with caplog.at_level("WARNING"):
        assert await invalidate_quota_config(org_id=1) == 1
    assert "skipped" in caplog.text


async def test_valkey_error_is_swallowed(monkeypatch, caplog):
    """A Valkey failure must never fail the config write that triggered it."""
    client = MagicMock()
    client.delete.side_effect = ConnectionError("down")
    monkeypatch.setattr(extensions, "redis_client", client)
    with caplog.at_level("WARNING"):
        assert await invalidate_quota_config(team_id=9) == 1
    assert "failed to invalidate" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_invalidation.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: ...quota_invalidation`.

- [ ] **Step 3: Write minimal implementation**

`services/management/app/services/quota_invalidation.py`:

```python
"""Invalidate the proxy's quota-config cache when an admin edits quota config (spec §6, D11).

The proxy reads org/team quota config through a Valkey read-through cache
(``shared.quota.config.QuotaConfigStore``, TTL 300s). Every management write deletes the
affected blobs so the next proxy request reloads from the DB -- best-effort: a missing or
unreachable cache is logged and the proxy's TTL becomes the staleness bound, exactly like
``auth_cache_invalidation``. Live consumption counters are never touched here.
"""

import asyncio
import logging

from shared.quota import keys

from .. import extensions as _ext

logger = logging.getLogger(__name__)


async def invalidate_quota_config(*, org_id: int | None = None, team_id: int | None = None) -> int:
    """Delete the org and/or team config blob; return how many keys were targeted. Never raises."""
    targets: list[str] = []
    if org_id is not None:
        targets.append(keys.org_config(org_id))
    if team_id is not None:
        targets.append(keys.team_config(team_id))
    if not targets:
        return 0
    client = getattr(_ext, "redis_client", None)
    if client is None:
        logger.warning(
            "quota: no cache client configured; config invalidation for %d key(s) skipped "
            "(bounded by the proxy's config TTL instead)", len(targets),
        )  # fmt: skip
        return len(targets)
    try:
        await asyncio.to_thread(client.delete, *targets)
    except Exception as exc:  # noqa: BLE001 -- cache outage must not fail the config write
        logger.warning("quota: failed to invalidate %d config key(s): %s", len(targets), exc)
    return len(targets)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_invalidation.py -v --no-cov`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/management/app/services/quota_invalidation.py tests/unit/management/test_quota_invalidation.py
git commit -m "feat(quota): best-effort proxy config-cache invalidation on quota edits"
git push
```

### Task 16: Pooled-quota flag helper + org pool and team pool routes

**Files:**
- Create: `shared/quota/flags.py`
- Create: `services/management/app/api/v1/quota_pools.py`
- Modify: `services/management/app/api/v1/__init__.py` (append `quota_pools,` to the import tuple after `teams,`)
- Modify: `tests/unit/management/team_fixtures.py:13` (`_REAL_DB_ROUTE_MODULES` gains `"quota_pools"`)
- Modify: `tests/unit/management/test_scope_authz.py` (`_EXPECTED_ROUTE_COUNT = 117`, add a `+3 pooled-quota org pool/team pool routes` line to its comment)
- Test: `tests/unit/quota/test_flags.py`, `tests/unit/management/test_quota_org_routes.py`

**Interfaces:**
- Consumes: Task 14 `quota_pools` service (`get_org_pool_row`, `allocated_to_teams`, `set_org_pool`, `set_team_pool`, `QuotaError`); Task 15 `invalidate_quota_config`; Task 11 cycle helpers; `team_world`, `real_db_client`, `team_headers` fixtures.
- Produces: `shared.quota.flags.POOLED_QUOTA_FLAG = "waddleai.pooled_quota"` and `pooled_quota_enabled(distinct_id: str) -> bool` (default OFF; any lookup failure -> False, logged); routes `GET|PUT /api/v1/pooled-quota/org-pool`, `PUT /api/v1/pooled-quota/teams/<int:team_id>/pool`; module-level helpers in `quota_pools.py` reused by Tasks 17, 19, 24: `_db() -> DB`, `_quota_off()`, `_error(exc)`, `_BEARER_AUTH`, `MessageResponse`, `PaginationMeta`; DTOs `OrgPoolResponse(org_id, enabled, cycle_anchor_date, pool_credits, allocated_to_teams, current_cycle, cycle_start, cycle_end)`, `SetOrgPoolRequest(cycle_anchor_date: str | None, pool_credits: int | None, enabled: bool | None)`, `SetTeamPoolRequest(pool_credits: int | None)`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/quota/test_flags.py`:

```python
"""The pooled-quota flag is default-OFF and fails to OFF."""

import pytest

from shared.quota import flags


def test_default_off_without_env(monkeypatch):
    """Unseen flag -> OFF (house rule: new flags default OFF)."""
    monkeypatch.delenv("WADDLEAI_FLAG_POOLED_QUOTA", raising=False)
    assert flags.pooled_quota_enabled("1") is False


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False), ("no", False)])
def test_env_override_wins(monkeypatch, raw, expected):
    """WADDLEAI_FLAG_POOLED_QUOTA is the test/alpha override."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", raw)
    assert flags.pooled_quota_enabled("7") is expected


def test_lookup_failure_fails_to_off(monkeypatch):
    """A raising flag layer must read as OFF, never as ON and never propagate."""
    def boom(*a, **k):
        raise RuntimeError("posthog down")

    monkeypatch.setattr(flags, "is_feature_enabled", boom)
    assert flags.pooled_quota_enabled("7") is False
```

`tests/unit/management/test_quota_org_routes.py`:

```python
"""Org-layer pooled-quota routes: org pool + team pool (flag, scope, tenancy, shape, invalidation)."""

import importlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from shared.quota.cycle import cycle_bounds, cycle_index, utc_today

ROUTES = importlib.import_module("services.management.app.api.v1.quota_pools")
BASE = "/api/v1/pooled-quota"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Every test runs flag-ON unless it says otherwise."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def inval(monkeypatch):
    """Replace the proxy-cache invalidation with an awaitable mock to assert on."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(ROUTES, "invalidate_quota_config", mock)
    return mock


async def test_flag_off_is_404(real_db_client, team_headers, monkeypatch):
    """With waddleai.pooled_quota OFF the whole surface 404s."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "0")
    resp = await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))
    assert resp.status_code == 404


async def test_requires_auth_and_org_admin_scope(real_db_client, team_headers):
    """No token -> 401; a team Owner without the org scope -> 403."""
    assert (await real_db_client.get(f"{BASE}/org-pool")).status_code == 401
    assert (await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("olive"))).status_code == 403


async def test_unconfigured_org_reads_as_disabled_defaults(real_db_client, team_headers, team_world):
    """No row yet: a fully-shaped, disabled, zero pool (and no row is created by a GET)."""
    resp = await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))
    assert resp.status_code == 200
    assert await resp.get_json() == {
        "org_id": team_world.org["a"], "enabled": False, "cycle_anchor_date": None,
        "pool_credits": 0, "allocated_to_teams": 0,
        "current_cycle": None, "cycle_start": None, "cycle_end": None,
    }  # fmt: skip


async def test_put_org_pool_then_get_reports_the_current_cycle(real_db_client, team_headers, team_world, inval):
    """PUT sets anchor/pool/enabled; GET derives the current cycle; the org cache blob is dropped."""
    anchor = utc_today() - timedelta(days=35)
    resp = await real_db_client.put(
        f"{BASE}/org-pool", headers=team_headers("boss"),
        json={"cycle_anchor_date": anchor.isoformat(), "pool_credits": 5000, "enabled": True},
    )  # fmt: skip
    assert resp.status_code == 200
    inval.assert_awaited_once_with(org_id=team_world.org["a"])
    body = await (await real_db_client.get(f"{BASE}/org-pool", headers=team_headers("boss"))).get_json()
    start, end = cycle_bounds(anchor, cycle_index(anchor, utc_today()))
    assert (body["enabled"], body["pool_credits"], body["current_cycle"]) == (True, 5000, 1)
    assert (body["cycle_start"], body["cycle_end"]) == (start.isoformat(), end.isoformat())


@pytest.mark.parametrize("payload", [{"cycle_anchor_date": "not-a-date"}, {"enabled": True}, {"pool_credits": -5}])
async def test_put_org_pool_rejects_bad_input(real_db_client, team_headers, payload):
    """Bad date, enabling without an anchor, and negative credits are all 400."""
    resp = await real_db_client.put(f"{BASE}/org-pool", headers=team_headers("boss"), json=payload)
    assert resp.status_code == 400


async def test_put_team_pool_allocates_and_invalidates_only_the_team(real_db_client, team_headers, team_world, inval):
    """Allocation is bounded by the org pool (409) and drops only that team's blob."""
    w, h = team_world, team_headers("boss")
    await real_db_client.put(
        f"{BASE}/org-pool", headers=h,
        json={"cycle_anchor_date": "2026-01-01", "pool_credits": 1000, "enabled": True},
    )  # fmt: skip
    inval.reset_mock()
    url = f"{BASE}/teams/{w.t['a1']}/pool"
    assert (await real_db_client.put(url, headers=h, json={"pool_credits": 600})).status_code == 200
    inval.assert_awaited_once_with(team_id=w.t["a1"])
    assert (await real_db_client.put(f"{BASE}/teams/{w.t['a2']}/pool", headers=h, json={"pool_credits": 500})).status_code == 409
    assert (await real_db_client.put(url, headers=h, json={})).status_code == 400


async def test_team_pool_is_tenant_scoped(real_db_client, team_headers, team_world):
    """Another tenant's team and a missing team are both 404 (never leaking ids)."""
    h = team_headers("boss")
    assert (await real_db_client.put(f"{BASE}/teams/{team_world.t['b1']}/pool", headers=h, json={"pool_credits": 1})).status_code == 404
    assert (await real_db_client.put(f"{BASE}/teams/999999/pool", headers=h, json={"pool_credits": 1})).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/quota/test_flags.py tests/unit/management/test_quota_org_routes.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.flags` / `...api.v1.quota_pools`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/flags.py`:

```python
"""The ``waddleai.pooled_quota`` PostHog flag (pooled-quota spec D12): default OFF, per-org rollout."""

import logging

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

#: Env override is ``WADDLEAI_FLAG_POOLED_QUOTA`` (tests/alpha); ``distinct_id`` = ``str(org_id)``.
POOLED_QUOTA_FLAG = "waddleai.pooled_quota"


def pooled_quota_enabled(distinct_id: str) -> bool:
    """True only when the flag is ON for this org; any failure resolving it reads as OFF."""
    try:
        return bool(is_feature_enabled(POOLED_QUOTA_FLAG, distinct_id=distinct_id, default=False))
    except Exception:  # noqa: BLE001 -- flag layer is documented never-raises; belt and braces
        logger.warning("quota: %s lookup failed; defaulting OFF", POOLED_QUOTA_FLAG)
        return False
```

`services/management/app/api/v1/quota_pools.py`:

```python
"""WaddleAI Management API v1 - Pooled quota, org layer (pooled-quota spec §8, D11).

Org pool + cycle anchor, team pool allocation, override caps, conversion rates, usage and
rebuild. Authorization is scope-only (``quota:org_update`` for org config, ``quota:admin`` for the
global rate table); tenant comes from the token, never the body; cross-tenant ids are 404. Every
route is behind the ``waddleai.pooled_quota`` flag (404 when OFF). Responses are dataclass DTOs
carrying ids and credits only -- no PII. Every config write drops the proxy's cached config blob.
"""

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from penguin_dal.db import DB
from quart import g, jsonify
from quart_schema import security_scheme, tag, validate_request, validate_response

from shared.auth.rbac import Permission
from shared.quota.cycle import cycle_bounds, cycle_index, cycle_started, utc_today
from shared.quota.flags import pooled_quota_enabled

from ...extensions import db
from ...services import quota_pools as qp
from ...services.quota_invalidation import invalidate_quota_config
from ...services.quota_pools import QuotaError
from . import api_v1_bp
from .auth import require_auth, require_scope

_BEARER_AUTH: list[dict[str, list[str]]] = [{"bearerAuth": []}]


def _db() -> DB:
    """Return the process-wide penguin-dal handle, narrowed away from ``None``."""
    if db is None:
        raise RuntimeError("database not initialized")
    return db


@dataclass(slots=True)
class MessageResponse:
    """Generic ``{"message": str}`` envelope."""

    message: str


@dataclass(slots=True)
class PaginationMeta:
    """Bounded-window metadata attached to every list response."""

    page: int
    limit: int
    total: int | None
    pages: int | None


@dataclass(slots=True)
class SetOrgPoolRequest:
    """Request body for PUT /pooled-quota/org-pool; ``None`` leaves a field unchanged."""

    cycle_anchor_date: str | None = None
    pool_credits: int | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class SetTeamPoolRequest:
    """Request body for PUT /pooled-quota/teams/<id>/pool."""

    pool_credits: int | None = None


@dataclass(slots=True)
class OrgPoolResponse:
    """The org pool as returned by the API -- exactly these fields, never the raw row."""

    org_id: int
    enabled: bool
    cycle_anchor_date: str | None
    pool_credits: int
    allocated_to_teams: int
    current_cycle: int | None
    cycle_start: str | None
    cycle_end: str | None


async def _quota_off() -> tuple[Any, int] | None:
    """Return a 404 response when ``waddleai.pooled_quota`` is OFF for the caller's org."""
    if await asyncio.to_thread(pooled_quota_enabled, str(g.user.get("organization_id"))):
        return None
    return jsonify({"error": "Pooled quota is disabled"}), 404


def _error(exc: QuotaError) -> tuple[Any, int]:
    """Render a service-layer :class:`QuotaError` as a JSON error response."""
    return jsonify({"error": exc.message}), exc.status


def _parse_anchor(raw: str | None) -> date | None:
    """Parse an ISO ``YYYY-MM-DD`` anchor; ``None`` stays ``None``; garbage is a 400."""
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise QuotaError("cycle_anchor_date must be an ISO date (YYYY-MM-DD)", 400) from exc


def _org_pool_view(org_id: int, row: Any, allocated: int) -> dict[str, Any]:
    """Project a ``quota_org_pool`` row (or its absence) onto :class:`OrgPoolResponse`."""
    anchor = row.cycle_anchor_date if row is not None else None
    if isinstance(anchor, str):
        anchor = date.fromisoformat(anchor[:10])
    cycle = start = end = None
    if anchor is not None and cycle_started(anchor, utc_today()):
        cycle = cycle_index(anchor, utc_today())
        bounds = cycle_bounds(anchor, cycle)
        start, end = bounds[0].isoformat(), bounds[1].isoformat()
    return {
        "org_id": org_id,
        "enabled": bool(row.enabled) if row is not None else False,
        "cycle_anchor_date": anchor.isoformat() if anchor else None,
        "pool_credits": int(row.pool_credits or 0) if row is not None else 0,
        "allocated_to_teams": allocated,
        "current_cycle": cycle,
        "cycle_start": start,
        "cycle_end": end,
    }


@api_v1_bp.route("/pooled-quota/org-pool", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(OrgPoolResponse, 200)
async def get_org_pool():
    """Return the caller's org pool, anchor, and current cycle (disabled zeros when unset)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()

    def _load() -> dict[str, Any]:
        return _org_pool_view(
            org_id, qp.get_org_pool_row(database, org_id), qp.allocated_to_teams(database, org_id)
        )

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/org-pool", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
@validate_request(SetOrgPoolRequest)
async def put_org_pool(data: SetOrgPoolRequest):
    """Set the org pool size, cycle anchor and enabled switch (any subset)."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    try:
        anchor = _parse_anchor(data.cycle_anchor_date)
        await asyncio.to_thread(
            lambda: qp.set_org_pool(
                _db(), org_id, cycle_anchor_date=anchor,
                pool_credits=data.pool_credits, enabled=data.enabled,
            )
        )  # fmt: skip
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(org_id=org_id)
    return {"message": "Org pool updated"}


@api_v1_bp.route("/pooled-quota/teams/<int:team_id>/pool", methods=["PUT"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(MessageResponse, 200)
@validate_request(SetTeamPoolRequest)
async def put_team_pool(team_id: int, data: SetTeamPoolRequest):
    """Allocate ``pool_credits`` of the org pool to a team in the caller's own tenant."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()
    team = await asyncio.to_thread(lambda: database(database.teams.id == team_id).select().first())
    if team is None or team.organization_id != org_id:
        return jsonify({"error": "Team not found"}), 404
    if data.pool_credits is None:
        return jsonify({"error": "pool_credits is required"}), 400
    try:
        await asyncio.to_thread(lambda: qp.set_team_pool(database, org_id, team, data.pool_credits))
    except QuotaError as exc:
        return _error(exc)
    await invalidate_quota_config(team_id=team_id)
    return {"message": "Team pool updated"}
```

`api/v1/__init__.py`: add `quota_pools,` after `teams,`. `team_fixtures.py`: `_REAL_DB_ROUTE_MODULES = ("auth", "users", "organizations", "keys", "proxy_keys", "teams", "quota_pools")`. `test_scope_authz.py`: set `_EXPECTED_ROUTE_COUNT = 117` and add the comment line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/quota/test_flags.py tests/unit/management/test_quota_org_routes.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS. In `test_scope_authz.py`'s `_MIGRATED_SCOPES` no change is needed yet (`QUOTA_ORG_UPDATE` is already present).

- [ ] **Step 5: Commit**

```bash
git add shared/quota/flags.py services/management/app/api/v1/quota_pools.py services/management/app/api/v1/__init__.py tests/unit/management/team_fixtures.py tests/unit/management/test_scope_authz.py tests/unit/quota/test_flags.py tests/unit/management/test_quota_org_routes.py
git commit -m "feat(quota): flag helper + org pool and team pool management routes"
git push
```

---
