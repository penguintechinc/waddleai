# Elastic Pooled Quota — Phase 2d: Config Loaders + Valkey Read-Through Store

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Master plan: `2026-10-16-elastic-pooled-quota.md`. Spec §5, §9, D2/D3/D6.

Part of the data-model phase (spec §5, §9, D2/D3/D6); earlier tasks of this phase created the tables and migration.

---

### Task 12: Config loaders, JSON codec, and the Valkey read-through `QuotaConfigStore`

**Files:**
- Create: `shared/quota/config.py`
- Test: `tests/unit/quota/test_config.py`

**Interfaces:**
- Consumes: Task 9 tables; Task 11 types + `keys.org_config`/`keys.team_config`.
- Produces: `load_org_config(db, org_id: int) -> OrgQuotaConfig | None`; `load_team_config(db, team_id: int) -> TeamQuotaConfig | None` (None = no row = not configured); `org_config_to_json(cfg) -> str`, `org_config_from_json(raw: str) -> OrgQuotaConfig | None`, `team_config_to_json(cfg) -> str`, `team_config_from_json(raw: str) -> TeamQuotaConfig | None` (`"null"` decodes to None); `QuotaConfigError(Exception)`; `QuotaConfigStore(valkey, db, ttl_s: int = 300, negative_ttl_s: int = 30)` with `async get_org(org_id) -> OrgQuotaConfig | None` and `async get_team(team_id) -> TeamQuotaConfig | None` (Valkey GET -> else DB load in a thread -> SET with TTL; `valkey=None` or any Valkey error falls back to a direct DB load; a DB error raises `QuotaConfigError`; malformed cached JSON is treated as a miss).

- [ ] **Step 1: Write the failing test**

`tests/unit/quota/test_config.py`:

```python
"""DB loaders, JSON codec, and the read-through store (cache hit/miss/negative/outage)."""

from datetime import date

import pytest

from shared.quota import config as qc
from shared.quota import keys
from shared.quota.types import OrgQuotaConfig, OverrideRule, TeamQuotaConfig, UserReservation


class FakeValkey:
    """Async dict-backed Valkey stand-in recording SETs; can be told to fail."""

    def __init__(self, fail=False):
        self.data, self.sets, self.fail = {}, [], fail

    async def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value
        self.sets.append((key, ex))


def _seed(db):
    """org + team + two users + full quota config; returns (org, team, u1, u2)."""
    org = int(db.organizations.insert(name="acme"))
    team = int(db.teams.insert(organization_id=org, name="Alpha"))
    u1 = int(db.users.insert(username="a", email="a@x", password_hash="x", role="user", organization_id=org))
    u2 = int(db.users.insert(username="b", email="b@x", password_hash="x", role="user", organization_id=org))
    db.quota_org_pool.insert(org_id=org, cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True)
    db.quota_team_pool.insert(team_id=team, organization_id=org, pool_credits=500, shared_buffer_credits=100)
    db.quota_user_reservation.insert(team_id=team, user_id=u1, organization_id=org, guaranteed_credits=40, daily_subcap=15)
    db.quota_user_reservation.insert(team_id=team, user_id=u2, organization_id=org, guaranteed_credits=60)
    db.quota_override.insert(organization_id=org, user_id=u1, cap_credits=90)
    db.quota_override.insert(organization_id=org, provider="openai", model="gpt-4o", cap_credits=5)
    db.commit()
    return org, team, u1, u2


def test_loaders_read_the_tables(real_schema_db):
    """Org config carries anchor/enabled/overrides; team config carries pool/buffer/reservations."""
    org, team, u1, u2 = _seed(real_schema_db)
    o = qc.load_org_config(real_schema_db, org)
    assert (o.enabled, o.cycle_anchor_date, o.pool_credits) == (True, date(2026, 1, 1), 1000)
    assert [(r.user_id, r.model, r.provider, r.cap_credits) for r in o.overrides] == [
        (u1, None, None, 90), (None, "gpt-4o", "openai", 5)]
    t = qc.load_team_config(real_schema_db, team)
    assert (t.org_id, t.pool_credits, t.shared_buffer_credits) == (org, 500, 100)
    assert t.reservations[u1] == UserReservation(u1, 40, 15)
    assert t.reservations[u2] == UserReservation(u2, 60, None)


def test_missing_rows_load_as_none(real_schema_db):
    """No pool row means 'not configured' (None), never an error."""
    assert qc.load_org_config(real_schema_db, 999) is None
    assert qc.load_team_config(real_schema_db, 999) is None


def test_json_round_trip_and_null():
    """Codec is lossless; the literal 'null' decodes to None (negative cache entry)."""
    org = OrgQuotaConfig(1, True, date(2026, 1, 1), 10, (OverrideRule(3, 9, user_id=2),))
    team = TeamQuotaConfig(4, 1, 5, 2, {7: UserReservation(7, 3, None)})
    assert qc.org_config_from_json(qc.org_config_to_json(org)) == org
    assert qc.team_config_from_json(qc.team_config_to_json(team)) == team
    assert qc.org_config_from_json("null") is None and qc.team_config_from_json("null") is None


async def test_store_caches_after_first_load(real_schema_db):
    """Second get is served from Valkey: one DB load, one SET with the positive TTL."""
    org, *_ = _seed(real_schema_db)
    vk = FakeValkey()
    store = qc.QuotaConfigStore(vk, real_schema_db, ttl_s=300, negative_ttl_s=30)
    first = await store.get_org(org)
    assert keys.org_config(org) in vk.data
    real_schema_db(real_schema_db.quota_org_pool.org_id == org).update(pool_credits=1)
    second = await store.get_org(org)
    assert second == first and first.pool_credits == 1000
    assert vk.sets == [(keys.org_config(org), 300)]


async def test_store_negative_caches_a_missing_config(real_schema_db):
    """A missing config is cached as 'null' with the short TTL (no DB hammering for unconfigured orgs)."""
    vk = FakeValkey()
    store = qc.QuotaConfigStore(vk, real_schema_db, ttl_s=300, negative_ttl_s=30)
    assert await store.get_team(12345) is None
    assert vk.data[keys.team_config(12345)] == "null"
    assert vk.sets == [(keys.team_config(12345), 30)]


async def test_store_survives_valkey_outage_and_bad_json(real_schema_db):
    """Valkey down -> direct DB read; malformed cached JSON -> treated as a miss."""
    org, *_ = _seed(real_schema_db)
    assert (await qc.QuotaConfigStore(FakeValkey(fail=True), real_schema_db).get_org(org)).pool_credits == 1000
    assert (await qc.QuotaConfigStore(None, real_schema_db).get_org(org)).pool_credits == 1000
    vk = FakeValkey()
    vk.data[keys.org_config(org)] = "{not json"
    assert (await qc.QuotaConfigStore(vk, real_schema_db).get_org(org)).pool_credits == 1000


async def test_store_db_failure_raises_quota_config_error(monkeypatch):
    """A DB failure is a QuotaConfigError (the engine turns it into fail-open)."""
    def boom(db, org_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(qc, "load_org_config", boom)
    with pytest.raises(qc.QuotaConfigError):
        await qc.QuotaConfigStore(FakeValkey(), object()).get_org(1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/quota/test_config.py -v --no-cov`
Expected: FAIL — `ImportError: cannot import name 'config' from 'shared.quota'`.

- [ ] **Step 3: Write minimal implementation**

`shared/quota/config.py`:

```python
"""Quota config: DB loaders, JSON codec, and the Valkey read-through cache (spec §5, §6).

The proxy never queries config tables per request: :class:`QuotaConfigStore` reads a JSON blob
from Valkey, loading from the DB on a miss. Management deletes the blob on every config edit
(``services/management/app/services/quota_invalidation.py``); the TTL bounds staleness when
that delete cannot reach Valkey -- the teams ``auth_cache_invalidation`` pattern.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from shared.quota import keys
from shared.quota.types import OrgQuotaConfig, OverrideRule, TeamQuotaConfig, UserReservation

logger = logging.getLogger(__name__)


class QuotaConfigError(Exception):
    """The quota config could not be loaded from the database."""


def _opt_int(value: Any) -> int | None:
    """``None`` stays ``None``; anything else becomes ``int``."""
    return None if value is None else int(value)


def _as_date(value: Any) -> date | None:
    """Normalize a DB date value (date, datetime, ISO string, or None) to ``date | None``."""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def load_org_config(db: Any, org_id: int) -> OrgQuotaConfig | None:
    """Load an org's pool switch, anchor and overrides; ``None`` when no pool row exists."""
    pool = db(db.quota_org_pool.org_id == org_id).select().first()
    if pool is None:
        return None
    rows = db(db.quota_override.organization_id == org_id).select(orderby=db.quota_override.id)
    overrides = tuple(
        OverrideRule(
            id=int(r.id),
            cap_credits=int(r.cap_credits),
            user_id=_opt_int(r.user_id),
            model=r.model or None,
            provider=r.provider or None,
        )
        for r in rows
    )
    return OrgQuotaConfig(
        org_id=org_id,
        enabled=bool(pool.enabled),
        cycle_anchor_date=_as_date(pool.cycle_anchor_date),
        pool_credits=int(pool.pool_credits or 0),
        overrides=overrides,
    )


def load_team_config(db: Any, team_id: int) -> TeamQuotaConfig | None:
    """Load a team's pool/buffer and per-user reservations; ``None`` when no pool row exists."""
    pool = db(db.quota_team_pool.team_id == team_id).select().first()
    if pool is None:
        return None
    rows = db(db.quota_user_reservation.team_id == team_id).select()
    reservations = {
        int(r.user_id): UserReservation(
            user_id=int(r.user_id),
            guaranteed_credits=int(r.guaranteed_credits or 0),
            daily_subcap=_opt_int(r.daily_subcap),
        )
        for r in rows
    }
    return TeamQuotaConfig(
        team_id=team_id,
        org_id=int(pool.organization_id),
        pool_credits=int(pool.pool_credits or 0),
        shared_buffer_credits=int(pool.shared_buffer_credits or 0),
        reservations=reservations,
    )


def org_config_to_json(cfg: OrgQuotaConfig) -> str:
    """Serialize an org config for the Valkey cache."""
    return json.dumps(
        {
            "org_id": cfg.org_id,
            "enabled": cfg.enabled,
            "anchor": cfg.cycle_anchor_date.isoformat() if cfg.cycle_anchor_date else None,
            "pool": cfg.pool_credits,
            "overrides": [
                {"id": o.id, "cap": o.cap_credits, "user_id": o.user_id,
                 "model": o.model, "provider": o.provider}
                for o in cfg.overrides
            ],
        }
    )  # fmt: skip


def org_config_from_json(raw: str) -> OrgQuotaConfig | None:
    """Inverse of :func:`org_config_to_json`; the literal ``null`` decodes to ``None``."""
    data = json.loads(raw)
    if data is None:
        return None
    return OrgQuotaConfig(
        org_id=int(data["org_id"]),
        enabled=bool(data["enabled"]),
        cycle_anchor_date=date.fromisoformat(data["anchor"]) if data["anchor"] else None,
        pool_credits=int(data["pool"]),
        overrides=tuple(
            OverrideRule(int(o["id"]), int(o["cap"]), _opt_int(o["user_id"]), o["model"], o["provider"])
            for o in data["overrides"]
        ),
    )


def team_config_to_json(cfg: TeamQuotaConfig) -> str:
    """Serialize a team config for the Valkey cache."""
    return json.dumps(
        {
            "team_id": cfg.team_id,
            "org_id": cfg.org_id,
            "pool": cfg.pool_credits,
            "buffer": cfg.shared_buffer_credits,
            "reservations": [
                {"user_id": r.user_id, "guaranteed": r.guaranteed_credits, "daily": r.daily_subcap}
                for r in cfg.reservations.values()
            ],
        }
    )


def team_config_from_json(raw: str) -> TeamQuotaConfig | None:
    """Inverse of :func:`team_config_to_json`; the literal ``null`` decodes to ``None``."""
    data = json.loads(raw)
    if data is None:
        return None
    return TeamQuotaConfig(
        team_id=int(data["team_id"]),
        org_id=int(data["org_id"]),
        pool_credits=int(data["pool"]),
        shared_buffer_credits=int(data["buffer"]),
        reservations={
            int(r["user_id"]): UserReservation(int(r["user_id"]), int(r["guaranteed"]), _opt_int(r["daily"]))
            for r in data["reservations"]
        },
    )


class QuotaConfigStore:
    """Valkey read-through cache of org/team quota config (see module docstring)."""

    def __init__(self, valkey: Any, db: Any, ttl_s: int = 300, negative_ttl_s: int = 30) -> None:
        """Bind the (possibly ``None``) async Valkey client, the DAL handle and the TTLs."""
        self.valkey = valkey
        self.db = db
        self.ttl_s = ttl_s
        self.negative_ttl_s = negative_ttl_s

    async def get_org(self, org_id: int) -> OrgQuotaConfig | None:
        """Return the org's config (cached), or ``None`` when it has none."""
        return await self._get(
            keys.org_config(org_id), lambda: load_org_config(self.db, org_id),
            org_config_from_json, org_config_to_json,
        )  # fmt: skip

    async def get_team(self, team_id: int) -> TeamQuotaConfig | None:
        """Return the team's config (cached), or ``None`` when it has none."""
        return await self._get(
            keys.team_config(team_id), lambda: load_team_config(self.db, team_id),
            team_config_from_json, team_config_to_json,
        )  # fmt: skip

    async def _get(self, key: str, loader: Callable[[], Any], decode: Callable[[str], Any],
                   encode: Callable[[Any], str]) -> Any:  # fmt: skip
        """Cache hit -> decode; miss/outage/bad JSON -> DB load (thread) then best-effort SET."""
        if self.valkey is not None:
            try:
                cached = await self.valkey.get(key)
            except Exception as exc:  # noqa: BLE001 -- cache outage falls back to the DB
                logger.warning("quota config cache read failed (%s); using DB", exc)
                cached = None
            if cached is not None:
                try:
                    return decode(cached)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning("quota config cache entry %s malformed (%s); reloading", key, exc)
        try:
            value = await asyncio.to_thread(loader)
        except Exception as exc:
            raise QuotaConfigError(f"quota config load failed for {key}: {exc}") from exc
        if self.valkey is not None:
            try:
                await self.valkey.set(
                    key, "null" if value is None else encode(value),
                    ex=self.negative_ttl_s if value is None else self.ttl_s,
                )  # fmt: skip
            except Exception as exc:  # noqa: BLE001 -- failing to cache must not fail the request
                logger.warning("quota config cache write failed (%s)", exc)
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/unit/quota/test_config.py -v --no-cov`
Expected: PASS (7 tests). Note `test_store_caches_after_first_load` updates the DB after the first read to prove the second read comes from cache.

- [ ] **Step 5: Commit**

```bash
git add shared/quota/config.py tests/unit/quota/test_config.py
git commit -m "feat(quota): config loaders, JSON codec, Valkey read-through QuotaConfigStore"
git push
```
