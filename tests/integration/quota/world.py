"""Shared builder for the quota integration tests: real Valkey + real schema DB + real engine."""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from typing import Any

from services.management.app.services import quota_pools as qp
from shared.quota.config import QuotaConfigStore
from shared.quota.engine import QuotaEngine
from shared.quota.lease import QuotaLimiter
from shared.quota.ledger import QuotaLedger

MSG = [{"role": "user", "content": "hello quota"}]


@dataclass
class Clock:
    """A mutable 'today' so a test can cross a cycle boundary."""

    value: date


class FixedTM:
    """TokenManager stand-in: a fixed credit estimate and a single-provider model."""

    def __init__(self, credits: int = 4) -> None:
        """Fix the credit estimate every call returns."""
        self.credits = credits

    def rates_stale(self) -> bool:
        """Rates are never stale."""
        return False

    def estimate_credits(self, model: str, input_tokens: int, output_tokens: int) -> int:
        """Return the fixed credit estimate."""
        return self.credits

    def providers_for_model(self, model: str) -> frozenset[str]:
        """Only gpt-4o resolves, to the openai provider."""
        return frozenset({"openai"}) if model == "gpt-4o" else frozenset()


@dataclass
class World:
    """Everything an end-to-end quota test needs."""

    db: Any
    org: int
    team: int
    u: dict[str, int]
    engine: QuotaEngine
    limiter: QuotaLimiter
    ledger: QuotaLedger
    store: QuotaConfigStore
    clock: Clock
    tm: FixedTM
    valkey: Any


def build_world(team_world, valkey, *, credits=4, tele=None, start=date(2026, 1, 15)) -> World:
    """Configure org a / team a1 through the real service layer and wire a real engine."""
    db, org, team_id = team_world.db, team_world.org["a"], team_world.t["a1"]
    qp.set_org_pool(db, org, cycle_anchor_date=date(2026, 1, 1), pool_credits=1000, enabled=True)
    team = db(db.teams.id == team_id).select().first()
    qp.set_team_pool(db, org, team, 100)
    qp.set_team_buffer(db, team, 10)
    qp.set_user_reservation(db, team, team_world.u["mia"], 20, None)
    qp.set_user_reservation(db, team, team_world.u["olive"], 30, None)
    limiter, store, ledger = QuotaLimiter(valkey), QuotaConfigStore(valkey, db), QuotaLedger(db)
    clock, tm = Clock(start), FixedTM(credits)
    engine = QuotaEngine(
        limiter, store, ledger, tm, tele, today=lambda: clock.value, flag=lambda _org: True
    )
    return World(
        db, org, team_id, dict(team_world.u), engine, limiter, ledger, store, clock, tm, valkey
    )


def user_of(w: World, name: str) -> SimpleNamespace:
    """A UserContext-shaped identity for one team_world user in team a1."""
    return SimpleNamespace(user_id=w.u[name], organization_id=w.org, team_id=w.team)
