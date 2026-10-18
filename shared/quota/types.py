"""Quota value types shared by the management and proxy planes (pooled-quota spec §5-§7)."""

from dataclasses import dataclass, field
from datetime import date

REASON_EXHAUSTED = "quota_exhausted"
REASON_DAILY = "daily_subcap_exceeded"
REASON_OVERRIDE = "override_exceeded"


@dataclass(slots=True, frozen=True)
class OverrideRule:
    """A hard cap on a user and/or model and/or provider (further-restrict only, spec D5)."""

    id: int
    cap_credits: int
    user_id: int | None = None
    model: str | None = None
    provider: str | None = None

    def matches_reserve(
        self, user_id: int, model: str | None, providers: frozenset[str] = frozenset()
    ) -> bool:
        """Best-effort pre-dispatch match (spec §7).

        The actual provider is unknown, so a rule naming a provider matches only when
        ``providers`` (those serving the requested model) is exactly that provider; otherwise
        it is enforced at reconcile only.
        """
        if self.provider is not None and providers != frozenset({self.provider}):
            return False
        if self.user_id is not None and self.user_id != user_id:
            return False
        return not (self.model is not None and self.model != model)

    def matches_actual(self, user_id: int, provider: str, model: str) -> bool:
        """Exact match against the actual provider:model once dispatch has happened."""
        return (
            self.user_id in (None, user_id)
            and self.model in (None, model)
            and self.provider in (None, provider)
        )


@dataclass(slots=True, frozen=True)
class UserReservation:
    """A user's private guaranteed credits and optional per-day sub-cap."""

    user_id: int
    guaranteed_credits: int
    daily_subcap: int | None = None


@dataclass(slots=True, frozen=True)
class OrgQuotaConfig:
    """An org's pool switch, cycle anchor, and override rules."""

    org_id: int
    enabled: bool
    cycle_anchor_date: date | None
    pool_credits: int
    overrides: tuple[OverrideRule, ...] = ()


@dataclass(slots=True, frozen=True)
class TeamQuotaConfig:
    """A team's sub-pool, lendable buffer, and per-user reservations (keyed by user id)."""

    team_id: int
    org_id: int
    pool_credits: int
    shared_buffer_credits: int
    reservations: dict[int, UserReservation] = field(default_factory=dict)


@dataclass(slots=True)
class QuotaDecision:
    """Result of one atomic reserve; ``degraded`` means the limiter failed open."""

    allowed: bool
    reason: str | None = None
    reservation_id: str | None = None
    from_reservation: int = 0
    from_buffer: int = 0
    degraded: bool = False


@dataclass(slots=True)
class ReconcileResult:
    """Result of reconciling/releasing one reservation."""

    applied: bool
    from_reservation: int = 0
    from_buffer: int = 0
    overage: int = 0
    over_cap_override_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class QuotaReservation:
    """What the proxy remembers about an admitted request until reconcile (carries the cycle)."""

    reservation_id: str
    org_id: int
    team_id: int
    user_id: int
    cycle: int
    credits_reserved: int
    from_reservation: int
    from_buffer: int
    model_requested: str | None


@dataclass(slots=True)
class QuotaOutcome:
    """What the engine tells the stage: block with a reason, or proceed (reservation or no-op)."""

    blocked: bool
    reason: str | None = None
    reservation: QuotaReservation | None = None
