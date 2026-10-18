"""DB loaders, JSON codec, and the read-through store (cache hit/miss/negative/outage)."""

from datetime import date, datetime

import pytest

from shared.quota import config as qc
from shared.quota import keys
from shared.quota.types import OrgQuotaConfig, OverrideRule, TeamQuotaConfig, UserReservation


class FakeValkey:
    """Async dict-backed Valkey stand-in recording SETs; can be told to fail."""

    def __init__(self, fail=False):
        """Start empty; ``fail`` makes every call raise."""
        self.data, self.sets, self.fail = {}, [], fail

    async def get(self, key):
        """Return the stored value or raise when failing."""
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        """Store the value and record (key, ttl)."""
        if self.fail:
            raise ConnectionError("down")
        self.data[key] = value
        self.sets.append((key, ex))


def _seed(db):
    """Org + team + two users + full quota config; returns (org, team, u1, u2)."""
    org = int(db.organizations.insert(name="acme"))
    team = int(db.teams.insert(organization_id=org, name="Alpha"))
    u1 = int(
        db.users.insert(
            username="a",
            email="a@x",
            password_hash="x",  # noqa: S106
            role="user",
            organization_id=org,
        )
    )
    u2 = int(
        db.users.insert(
            username="b",
            email="b@x",
            password_hash="x",  # noqa: S106
            role="user",
            organization_id=org,
        )
    )
    db.quota_org_pool.insert(
        org_id=org, cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True
    )
    db.quota_team_pool.insert(
        team_id=team, organization_id=org, pool_credits=500, shared_buffer_credits=100
    )
    db.quota_user_reservation.insert(
        team_id=team, user_id=u1, organization_id=org, guaranteed_credits=40, daily_subcap=15
    )
    db.quota_user_reservation.insert(
        team_id=team, user_id=u2, organization_id=org, guaranteed_credits=60
    )
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
        (u1, None, None, 90),
        (None, "gpt-4o", "openai", 5),
    ]
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
    """A missing config is cached as 'null' with the short TTL (no DB hammering)."""
    vk = FakeValkey()
    store = qc.QuotaConfigStore(vk, real_schema_db, ttl_s=300, negative_ttl_s=30)
    assert await store.get_team(12345) is None
    assert vk.data[keys.team_config(12345)] == "null"
    assert vk.sets == [(keys.team_config(12345), 30)]


async def test_store_survives_valkey_outage_and_bad_json(real_schema_db):
    """Valkey down -> direct DB read; malformed cached JSON -> treated as a miss."""
    org, *_ = _seed(real_schema_db)
    assert (
        await qc.QuotaConfigStore(FakeValkey(fail=True), real_schema_db).get_org(org)
    ).pool_credits == 1000
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


def test_as_date_normalizes_db_values():
    """date/datetime/ISO string/None all normalize to date | None."""
    assert qc._as_date(None) is None
    assert qc._as_date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert qc._as_date(datetime(2026, 1, 2, 3, 4)) == date(2026, 1, 2)
    assert qc._as_date("2026-01-02 00:00:00") == date(2026, 1, 2)
