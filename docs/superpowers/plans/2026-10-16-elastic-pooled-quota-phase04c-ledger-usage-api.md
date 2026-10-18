# Elastic Pooled Quota — Phase 4c: Durable Ledger + Usage/Rebuild API

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5 (`quota_usage_ledger`), §6 (reconcile writes it), D7.

---

### Task 23: `QuotaLedger` — durable rollup, optimistic add, Valkey counter rebuild

`quota_usage_ledger` is the durable record that survives a Valkey flush: reconcile writes the per-request final split into it (Task 26), management reports from it (Task 24) and rebuilds missing Valkey counters from it. penguin-dal has no atomic `field = field + n` update, so `add` uses an optimistic compare-and-set on the previous value with a bounded retry.

**Files:**
- Create: `shared/quota/ledger.py`
- Test: `tests/unit/quota/test_ledger.py`, `tests/integration/quota/test_ledger_rebuild.py`

**Interfaces:**
- Consumes: Task 9 `quota_usage_ledger` table; `keys.user_counter/buffer_counter/override_counter`; `QuotaLimiter.COUNTER_TTL_S`; `real_schema_db`, `valkey_sync` fixtures.
- Produces: constants `SCOPE_USER = "user"`, `SCOPE_BUFFER = "team_buffer"`, `SCOPE_OVERRIDE = "override"`; `LedgerEntry(scope_type: str, scope_id: int, credits: int)` (frozen, slots); `QuotaLedger(db)` with `add(org_id: int, cycle: int, entries: Sequence[LedgerEntry]) -> None` (entries with `credits <= 0` are skipped; accumulates into one row per scope+cycle; raises `RuntimeError` only if the CAS cannot converge in 8 tries), `usage(org_id: int, cycle: int) -> list[LedgerEntry]` (ordered by scope_type, scope_id), `rebuild_counters(valkey, org_id: int, cycle: int) -> int` (sync Valkey client; `SET NX` each ledger row into its counter key with the 35-day TTL, never overwriting a live counter; returns how many keys were created).

- [ ] **Step 1: Write the failing tests**

`tests/unit/quota/test_ledger.py`:

```python
"""Ledger accumulation, CAS retry, ordering and org scoping."""

import pytest

from shared.quota.ledger import SCOPE_BUFFER, SCOPE_USER, LedgerEntry, QuotaLedger


def _org(db, name="acme"):
    """Insert an organization and return its id."""
    org = int(db.organizations.insert(name=name))
    db.commit()
    return org


def test_add_accumulates_per_scope_and_cycle(real_schema_db):
    """Two adds to the same scope+cycle sum; another cycle or scope is a separate row."""
    ledger, org = QuotaLedger(real_schema_db), _org(real_schema_db)
    ledger.add(org, 3, [LedgerEntry(SCOPE_USER, 7, 10), LedgerEntry(SCOPE_BUFFER, 4, 2)])
    ledger.add(org, 3, [LedgerEntry(SCOPE_USER, 7, 5)])
    ledger.add(org, 4, [LedgerEntry(SCOPE_USER, 7, 1)])
    assert ledger.usage(org, 3) == [LedgerEntry(SCOPE_BUFFER, 4, 2), LedgerEntry(SCOPE_USER, 7, 15)]
    assert ledger.usage(org, 4) == [LedgerEntry(SCOPE_USER, 7, 1)]


@pytest.mark.parametrize("credits", [0, -3])
def test_non_positive_entries_are_skipped(real_schema_db, credits):
    """Zero/negative credits write nothing (the engine also filters; this is the backstop)."""
    ledger, org = QuotaLedger(real_schema_db), _org(real_schema_db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 7, credits)])
    assert ledger.usage(org, 1) == []


def test_usage_is_org_scoped(real_schema_db):
    """One tenant never sees another's ledger rows."""
    ledger = QuotaLedger(real_schema_db)
    a, b = _org(real_schema_db, "a"), _org(real_schema_db, "b")
    ledger.add(a, 1, [LedgerEntry(SCOPE_USER, 1, 5)])
    assert ledger.usage(b, 1) == []


def test_cas_conflict_is_retried_and_no_credit_is_lost(real_schema_db):
    """A concurrent writer wins between our read and our update: the retry re-reads and adds on top."""
    db, org = real_schema_db, _org(real_schema_db)

    class Racy(QuotaLedger):
        """Returns a STALE row on the first read, as if another writer had just updated."""

        stale_once = True

        def _read(self, query):
            row = super()._read(query)
            if Racy.stale_once and row is not None:
                Racy.stale_once = False
                query.update(credits_used=int(row.credits_used) + 100)  # the competing writer
            return row

    ledger = Racy(db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])  # creates the row (value 1)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 5)])  # stale read -> CAS miss -> retry
    assert ledger.usage(org, 1) == [LedgerEntry(SCOPE_USER, 9, 106)]


def test_cas_gives_up_loudly_after_bounded_retries(real_schema_db, monkeypatch):
    """A permanently losing CAS raises instead of looping forever or silently dropping credits."""
    db, org = real_schema_db, _org(real_schema_db)
    ledger = QuotaLedger(db)
    ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])
    real_read = ledger._read

    def always_stale(query):
        row = real_read(query)
        query.update(credits_used=int(row.credits_used) + 1)
        return row

    monkeypatch.setattr(ledger, "_read", always_stale)
    with pytest.raises(RuntimeError, match="did not converge"):
        ledger.add(org, 1, [LedgerEntry(SCOPE_USER, 9, 1)])
```

`tests/integration/quota/test_ledger_rebuild.py`:

```python
"""Rebuilding Valkey counters from the durable ledger (real Valkey, real schema)."""

from shared.quota import keys
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry, QuotaLedger


def test_rebuild_restores_missing_counters_and_never_overwrites_live_ones(real_schema_db, valkey_sync):
    """Flushed counters come back from the ledger; a live counter keeps its (higher) value."""
    db = real_schema_db
    org = int(db.organizations.insert(name="acme"))
    db.commit()
    ledger = QuotaLedger(db)
    ledger.add(org, 7, [LedgerEntry(SCOPE_USER, 11, 30), LedgerEntry(SCOPE_BUFFER, 4, 12),
                        LedgerEntry(SCOPE_OVERRIDE, 5, 9)])  # fmt: skip
    valkey_sync.set(keys.user_counter(11, 7), 99)
    assert ledger.rebuild_counters(valkey_sync, org, 7) == 2
    assert int(valkey_sync.get(keys.user_counter(11, 7))) == 99
    assert int(valkey_sync.get(keys.buffer_counter(4, 7))) == 12
    assert int(valkey_sync.get(keys.override_counter(5, 7))) == 9
    assert 0 < valkey_sync.ttl(keys.buffer_counter(4, 7)) <= 35 * 86400
    assert ledger.rebuild_counters(valkey_sync, org, 7) == 0  # idempotent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/quota/test_ledger.py -v --no-cov`
Expected: FAIL — `ModuleNotFoundError: shared.quota.ledger`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/ledger.py`:

```python
"""Durable per-scope, per-cycle credit rollup (pooled-quota spec §5, D7).

Written once per reconciled request with the FINAL reservation/buffer/override split, read by the
management usage report, and used to rebuild Valkey counters after a flush. penguin-dal has no
atomic ``col = col + n`` update, so ``add`` is an optimistic compare-and-set on the previous
value with a bounded retry; each statement is its own transaction (no rollback after a caught
``IntegrityError``). In-flight (unreconciled) requests are not in the ledger, so a rebuild can
under-count by at most the credits currently in flight.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from shared.quota import keys

logger = logging.getLogger(__name__)

SCOPE_USER = "user"
SCOPE_BUFFER = "team_buffer"
SCOPE_OVERRIDE = "override"
_COUNTER_TTL_S = 35 * 86400
_MAX_TRIES = 8


@dataclass(slots=True, frozen=True)
class LedgerEntry:
    """Credits consumed by one scope (user / team_buffer / override) in one cycle."""

    scope_type: str
    scope_id: int
    credits: int


class QuotaLedger:
    """Read/write access to ``quota_usage_ledger`` over a penguin-dal handle."""

    def __init__(self, db: Any) -> None:
        """Bind the DAL handle."""
        self.db = db

    def _query(self, org_id: int, cycle: int, entry: LedgerEntry) -> Any:
        """Query selecting the single ledger row for one scope in one cycle."""
        t = self.db.quota_usage_ledger
        return self.db(
            (t.scope_type == entry.scope_type) & (t.scope_id == entry.scope_id)
            & (t.organization_id == org_id) & (t.cycle_index == cycle)
        )  # fmt: skip

    def _read(self, query: Any) -> Any:
        """First row matching ``query`` (a seam the CAS-retry test overrides)."""
        return query.select().first()

    def _add_one(self, org_id: int, cycle: int, entry: LedgerEntry) -> None:
        """Insert-or-CAS-add one entry, retrying on a lost race."""
        for _ in range(_MAX_TRIES):
            query = self._query(org_id, cycle, entry)
            row = self._read(query)
            now = datetime.utcnow()
            if row is None:
                try:
                    self.db.quota_usage_ledger.insert(
                        scope_type=entry.scope_type, scope_id=entry.scope_id,
                        organization_id=org_id, cycle_index=cycle,
                        credits_used=entry.credits, updated_at=now,
                    )  # fmt: skip
                    return
                except IntegrityError:
                    continue  # a concurrent writer created it first: re-read and CAS
            current = int(row.credits_used or 0)
            if self._cas(org_id, cycle, entry, current, now):
                return
        raise RuntimeError(f"quota ledger add did not converge for {entry.scope_type}:{entry.scope_id}")

    def _cas(self, org_id: int, cycle: int, entry: LedgerEntry, current: int, now: datetime) -> bool:
        """Update ``credits_used`` only if it still equals ``current``; True when it applied."""
        t = self.db.quota_usage_ledger
        guarded = self.db(
            (t.scope_type == entry.scope_type) & (t.scope_id == entry.scope_id)
            & (t.organization_id == org_id) & (t.cycle_index == cycle) & (t.credits_used == current)
        )  # fmt: skip
        return bool(guarded.update(credits_used=current + entry.credits, updated_at=now))

    def add(self, org_id: int, cycle: int, entries: Sequence[LedgerEntry]) -> None:
        """Accumulate ``entries`` into the ledger; non-positive credits are skipped."""
        for entry in entries:
            if entry.credits > 0:
                self._add_one(org_id, cycle, entry)

    def usage(self, org_id: int, cycle: int) -> list[LedgerEntry]:
        """All ledger rows of the org for ``cycle``, ordered by scope type then id."""
        t = self.db.quota_usage_ledger
        rows = self.db((t.organization_id == org_id) & (t.cycle_index == cycle)).select()
        entries = [LedgerEntry(r.scope_type, int(r.scope_id), int(r.credits_used or 0)) for r in rows]
        return sorted(entries, key=lambda e: (e.scope_type, e.scope_id))

    def rebuild_counters(self, valkey: Any, org_id: int, cycle: int) -> int:
        """``SET NX`` each ledger row into its Valkey counter; return how many keys were created."""
        builders = {
            SCOPE_USER: keys.user_counter,
            SCOPE_BUFFER: keys.buffer_counter,
            SCOPE_OVERRIDE: keys.override_counter,
        }
        created = 0
        for entry in self.usage(org_id, cycle):
            build = builders.get(entry.scope_type)
            if build is None:
                continue
            if valkey.set(build(entry.scope_id, cycle), entry.credits, nx=True, ex=_COUNTER_TTL_S):
                created += 1
        return created
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/quota/test_ledger.py -v --no-cov && make test-valkey`
Expected: PASS. In `test_cas_conflict_is_retried...` the `Racy._read` bumps the value by 100 AFTER reading (so the read is stale), the CAS on the old value misses, the retry reads fresh (`stale_once` now False) and adds 5 on top of 101 = 106.

- [ ] **Step 5: Commit**

```bash
git add shared/quota/ledger.py tests/unit/quota/test_ledger.py tests/integration/quota/test_ledger_rebuild.py
git commit -m "feat(quota): QuotaLedger — CAS add, usage report, Valkey counter rebuild"
git push
```

---

### Task 24: Usage report + counter rebuild endpoints (and team usage fields)

**Files:**
- Modify: `services/management/app/api/v1/quota_pools.py` (append DTOs + 2 routes; add `from quart import request` to the existing quart import; add `from shared.quota.ledger import QuotaLedger`; add `from ... import extensions as _ext`)
- Modify: `services/management/app/api/v1/team_quota.py` (`TeamQuotaResponse` gains 3 fields; `get_team_quota._load` fills them)
- Modify: `tests/unit/management/test_team_quota_routes.py` (expected key set gains the 3 fields)
- Modify: `tests/unit/management/test_scope_authz.py` (`_EXPECTED_ROUTE_COUNT = 124`, comment `+2 pooled-quota usage/rebuild`)
- Test: `tests/unit/management/test_quota_usage_routes.py`

**Interfaces:**
- Consumes: `QuotaLedger.usage/rebuild_counters` (Task 23); Task 16 helpers `_db`, `_quota_off`, `_error`, `PaginationMeta`; `extensions.redis_client` (sync Valkey, may be `None`); `_org_pool_view` current-cycle logic.
- Produces: `GET /pooled-quota/usage?cycle=<int>` (`quota:org_update`; default = current cycle) -> `UsageResponse(org_id, cycle: int | None, entries: list[UsageEntry(scope_type, scope_id, credits_used)], total, pagination)`; `POST /pooled-quota/rebuild` body `RebuildRequest(cycle: int | None)` -> `RebuildResponse(cycle, counters_restored)` (503 when no cache client, 409 when no current cycle); `TeamQuotaResponse` adds `usage_cycle: int | None`, `reservation_used_credits: int`, `buffer_used_credits: int` (ledger-backed, so they lag in-flight requests).

- [ ] **Step 1: Write the failing test**

`tests/unit/management/test_quota_usage_routes.py`:

```python
"""Usage report + rebuild: current-cycle default, tenant scoping, cache outage."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from services.management.app import extensions
from services.management.app.services import quota_pools as qp
from shared.quota import keys
from shared.quota.cycle import cycle_index, utc_today
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_USER, LedgerEntry, QuotaLedger

BASE = "/api/v1/pooled-quota"
ANCHOR = utc_today() - timedelta(days=35)  # current cycle = 1


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Flag ON for every test in this module."""
    monkeypatch.setenv("WADDLEAI_FLAG_POOLED_QUOTA", "1")


@pytest.fixture
def seeded(team_world):
    """Org a enabled with a cycle-1 anchor and ledger rows in cycles 1 and 0 (+ a foreign org row)."""
    w = team_world
    qp.set_org_pool(w.db, w.org["a"], cycle_anchor_date=ANCHOR, pool_credits=1000, enabled=True)
    ledger = QuotaLedger(w.db)
    ledger.add(w.org["a"], 1, [LedgerEntry(SCOPE_USER, w.u["mia"], 30), LedgerEntry(SCOPE_BUFFER, w.t["a1"], 7)])
    ledger.add(w.org["a"], 0, [LedgerEntry(SCOPE_USER, w.u["mia"], 99)])
    ledger.add(w.org["b"], 1, [LedgerEntry(SCOPE_USER, w.u["carl"], 5)])
    return w


async def test_usage_defaults_to_the_current_cycle_and_is_org_scoped(real_db_client, team_headers, seeded):
    """Cycle 1 rows only, no foreign-org rows; ?cycle=0 returns the previous cycle."""
    h = team_headers("boss")
    body = await (await real_db_client.get(f"{BASE}/usage", headers=h)).get_json()
    assert body["cycle"] == cycle_index(ANCHOR, utc_today()) == 1
    assert {(e["scope_type"], e["credits_used"]) for e in body["entries"]} == {("user", 30), ("team_buffer", 7)}
    prev = await (await real_db_client.get(f"{BASE}/usage?cycle=0", headers=h)).get_json()
    assert [(e["scope_id"], e["credits_used"]) for e in prev["entries"]] == [(seeded.u["mia"], 99)]


async def test_usage_without_a_configured_cycle_is_empty_not_an_error(real_db_client, team_headers, team_world):
    """No anchor yet: cycle None and no entries."""
    body = await (await real_db_client.get(f"{BASE}/usage", headers=team_headers("boss"))).get_json()
    assert (body["cycle"], body["entries"], body["total"]) == (None, [], 0)


async def test_rebuild_restores_counters_and_reports_the_count(real_db_client, team_headers, seeded, monkeypatch):
    """POST /rebuild SET-NXs the current cycle's ledger rows into Valkey via the management client."""
    client = MagicMock()
    client.set.return_value = True
    monkeypatch.setattr(extensions, "redis_client", client)
    resp = await real_db_client.post(f"{BASE}/rebuild", headers=team_headers("boss"), json={})
    assert resp.status_code == 200
    assert await resp.get_json() == {"cycle": 1, "counters_restored": 2}
    assert {c.args[0] for c in client.set.call_args_list} == {
        keys.user_counter(seeded.u["mia"], 1), keys.buffer_counter(seeded.t["a1"], 1)}


async def test_rebuild_without_a_cache_client_is_503_and_without_a_cycle_409(real_db_client, team_headers, team_world, monkeypatch):
    """No Valkey client -> 503; no configured cycle (and no explicit one) -> 409."""
    monkeypatch.setattr(extensions, "redis_client", None)
    h = team_headers("boss")
    assert (await real_db_client.post(f"{BASE}/rebuild", headers=h, json={"cycle": 1})).status_code == 503
    monkeypatch.setattr(extensions, "redis_client", MagicMock())
    assert (await real_db_client.post(f"{BASE}/rebuild", headers=h, json={})).status_code == 409


async def test_team_view_reports_ledger_backed_usage(real_db_client, team_headers, seeded):
    """GET /teams/<id>/pooled-quota adds the cycle and reservation/buffer usage for the team's members."""
    team = seeded.db(seeded.db.teams.id == seeded.t["a1"]).select().first()
    qp.set_team_pool(seeded.db, seeded.org["a"], team, 100)
    url = f"/api/v1/teams/{seeded.t['a1']}/pooled-quota"
    body = await (await real_db_client.get(url, headers=team_headers("adam"))).get_json()
    assert (body["usage_cycle"], body["reservation_used_credits"], body["buffer_used_credits"]) == (1, 30, 7)


async def test_scope_required(real_db_client, team_headers):
    """The org scope gates both routes."""
    assert (await real_db_client.get(f"{BASE}/usage", headers=team_headers("olive"))).status_code == 403
    assert (await real_db_client.post(f"{BASE}/rebuild", headers=team_headers("olive"), json={})).status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/management/test_quota_usage_routes.py -v --no-cov`
Expected: FAIL — 404/405 (routes missing).

- [ ] **Step 3: Write minimal implementation**

In `quota_pools.py` change `from quart import g, jsonify` to `from quart import g, jsonify, request`, add `from shared.quota.ledger import QuotaLedger` and `from ... import extensions as _ext`, then append:

```python
@dataclass(slots=True)
class UsageEntry:
    """Credits consumed by one scope in one cycle (ids and credits only)."""

    scope_type: str
    scope_id: int
    credits_used: int


@dataclass(slots=True)
class UsageResponse:
    """Response body for GET /pooled-quota/usage."""

    org_id: int
    cycle: int | None
    entries: list[UsageEntry]
    total: int
    pagination: PaginationMeta


@dataclass(slots=True)
class RebuildRequest:
    """Request body for POST /pooled-quota/rebuild; ``cycle`` defaults to the current one."""

    cycle: int | None = None


@dataclass(slots=True)
class RebuildResponse:
    """Response body for POST /pooled-quota/rebuild."""

    cycle: int
    counters_restored: int


def current_cycle_for(database: DB, org_id: int) -> int | None:
    """The org's current cycle index, or ``None`` when it has no started anchor."""
    return _org_pool_view(org_id, qp.get_org_pool_row(database, org_id), 0)["current_cycle"]


@api_v1_bp.route("/pooled-quota/usage", methods=["GET"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(UsageResponse, 200)
async def get_usage():
    """Per-scope credit usage for a cycle (default current), from the durable ledger."""
    if (off := await _quota_off()) is not None:
        return off
    org_id = g.user.get("organization_id")
    database = _db()
    requested = request.args.get("cycle", type=int)
    page = PageRequest.from_request()

    def _load() -> dict[str, Any]:
        cycle = requested if requested is not None else current_cycle_for(database, org_id)
        rows = QuotaLedger(database).usage(org_id, cycle) if cycle is not None else []
        start, stop = page.limitby
        entries = [
            {"scope_type": e.scope_type, "scope_id": e.scope_id, "credits_used": e.credits}
            for e in rows[start:stop]
        ]
        return {"org_id": org_id, "cycle": cycle, "entries": entries, "total": len(rows),
                **page.meta(len(rows))}  # fmt: skip

    return await asyncio.to_thread(_load)


@api_v1_bp.route("/pooled-quota/rebuild", methods=["POST"])
@tag(["PooledQuota"])
@security_scheme(_BEARER_AUTH)
@require_auth
@require_scope(Permission.QUOTA_ORG_UPDATE)
@validate_response(RebuildResponse, 200)
@validate_request(RebuildRequest)
async def rebuild_counters(data: RebuildRequest):
    """Restore missing Valkey counters from the ledger (after a Valkey flush); never overwrites live ones."""
    if (off := await _quota_off()) is not None:
        return off
    client = getattr(_ext, "redis_client", None)
    if client is None:
        return jsonify({"error": "Cache is unavailable"}), 503
    org_id = g.user.get("organization_id")
    database = _db()
    cycle = data.cycle if data.cycle is not None else await asyncio.to_thread(current_cycle_for, database, org_id)
    if cycle is None:
        return jsonify({"error": "No active cycle; configure the org pool anchor first"}), 409
    restored = await asyncio.to_thread(QuotaLedger(database).rebuild_counters, client, org_id, cycle)
    return {"cycle": cycle, "counters_restored": restored}
```

(also add `from ._pagination import PageRequest` if Task 17 has not already — it has.)

In `team_quota.py`: import `from shared.quota.ledger import SCOPE_BUFFER, SCOPE_USER, QuotaLedger` and `from .quota_pools import current_cycle_for`; add to `TeamQuotaResponse` the fields `usage_cycle: int | None`, `reservation_used_credits: int`, `buffer_used_credits: int` (before `reservations`); and in `_load` before building the return dict add:

```python
        cycle = current_cycle_for(database, access.team.organization_id)
        used_resv = used_buf = 0
        if cycle is not None:
            member_ids = {
                int(m.user_id)
                for m in database(database.team_members.team_id == team_id).select()
            }
            for entry in QuotaLedger(database).usage(access.team.organization_id, cycle):
                if entry.scope_type == SCOPE_USER and entry.scope_id in member_ids:
                    used_resv += entry.credits
                elif entry.scope_type == SCOPE_BUFFER and entry.scope_id == team_id:
                    used_buf += entry.credits
```

and add `"usage_cycle": cycle, "reservation_used_credits": used_resv, "buffer_used_credits": used_buf,` to the returned dict. Update the key set in `test_team_quota_routes.py::test_get_shape_for_viewer_and_unconfigured_team` to include those three names. Set `_EXPECTED_ROUTE_COUNT = 124`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/management/test_quota_usage_routes.py tests/unit/management/test_team_quota_routes.py tests/unit/management/test_scope_authz.py -v --no-cov`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/management/app/api/v1/quota_pools.py services/management/app/api/v1/team_quota.py tests/unit/management/test_quota_usage_routes.py tests/unit/management/test_team_quota_routes.py tests/unit/management/test_scope_authz.py
git commit -m "feat(quota): usage report + counter rebuild endpoints; ledger-backed team usage fields"
git push
```
