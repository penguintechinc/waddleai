"""QuotaEngine: reserve-before / reconcile-after policy for the pooled quota (spec §6, §7).

Thin callers (``QuotaStage``, the ``_finalize`` hook) delegate here. Every failure that could
otherwise affect availability fails OPEN (D10): the request is allowed, an ERROR is logged and a
``fault`` telemetry signal is emitted so the bypass is visible, never silent. Reconcile and
release never raise. Reserve estimates with the REQUESTED model (provider unknown pre-dispatch);
reconcile uses the actual provider:model and is authoritative for override caps. A request is a
strict no-op (zero Valkey writes, never a 429) whenever the pool is off or unconfigured.
Telemetry labels are closed constants only: ids never reach a metric or span attribute.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

from shared.quota.config import QuotaConfigError
from shared.quota.credits import estimate_io_tokens
from shared.quota.cycle import cycle_index, cycle_started, day_stamp, utc_today
from shared.quota.flags import pooled_quota_enabled
from shared.quota.ledger import SCOPE_BUFFER, SCOPE_OVERRIDE, SCOPE_USER, LedgerEntry
from shared.quota.telemetry import QuotaTelemetry
from shared.quota.types import (
    REASON_DAILY,
    REASON_EXHAUSTED,
    REASON_OVERRIDE,
    QuotaOutcome,
    QuotaReservation,
    ReconcileResult,
)

logger = logging.getLogger(__name__)

_KNOWN_REASONS = frozenset({REASON_EXHAUSTED, REASON_DAILY, REASON_OVERRIDE})


class QuotaEngine:
    """Orchestrates config lookup, credit estimate, limiter calls, ledger writes and telemetry."""

    def __init__(
        self,
        limiter: Any,
        config_store: Any,
        ledger: Any,
        token_manager: Any,
        telemetry: QuotaTelemetry | None = None,
        today: Callable[[], date] = utc_today,
        flag: Callable[[str], bool] = pooled_quota_enabled,
    ) -> None:
        """Bind collaborators; ``today`` and ``flag`` are injectable for tests."""
        self.limiter = limiter
        self.config_store = config_store
        self.ledger = ledger
        self.token_manager = token_manager
        self.telemetry = telemetry or QuotaTelemetry()
        self._today = today
        self._flag = flag

    def _fault(self, kind: str) -> None:
        """Emit a fault signal; telemetry trouble must never reach the request path."""
        try:
            self.telemetry.fault(kind)
        except Exception:  # noqa: BLE001 -- telemetry is best-effort
            logger.debug("quota fault emission failed (%s)", kind, exc_info=True)

    def _noop(self, why: str) -> QuotaOutcome:
        """Record a no-op decision (nothing configured / not applicable) and proceed."""
        logger.debug("quota noop: %s", why)
        try:
            self.telemetry.decision("noop", why)
        except Exception:  # noqa: BLE001 -- telemetry is best-effort
            logger.debug("quota noop emission failed", exc_info=True)
        return QuotaOutcome(blocked=False)

    async def reserve(
        self, user: Any, model: str | None, messages: Sequence[Mapping[str, Any]]
    ) -> QuotaOutcome:
        """Reserve credits for a request, or report why it is blocked (or that nothing applies).

        Total: any internal error fails open (allow, fault, ERROR log) and releases a lease the
        request would otherwise believe it did not hold.
        """
        started = time.perf_counter()
        granted: list[str] = []
        try:
            with self.telemetry.span("quota.reserve") as span:
                outcome = await self._reserve(user, model, messages, granted)
                span.set_attribute("quota.blocked", outcome.blocked)
                span.set_attribute("quota.reserved", outcome.reservation is not None)
            self.telemetry.reserve_seconds(time.perf_counter() - started)
            return outcome
        except Exception as exc:  # noqa: BLE001 -- D10: the quota must never 500 a request
            logger.error(
                "quota reserve failed; failing open: %s: %s", type(exc).__name__, exc, exc_info=True
            )
            self._fault("fail_open")
            for rid in granted:  # no dangling lease: the caller gets no reservation to settle
                await self._release_id(rid)
            return QuotaOutcome(blocked=False)

    async def _release_id(self, reservation_id: str) -> None:
        """Best-effort refund of a lease granted to a request that then failed open."""
        try:
            await self.limiter.release(reservation_id)
        except Exception as exc:  # noqa: BLE001 -- the expiry sweeper is the backstop
            logger.error("quota release of orphaned lease failed: %s: %s", type(exc).__name__, exc)
            self._fault("release_failed")

    async def _reserve(
        self,
        user: Any,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        granted: list[str],
    ) -> QuotaOutcome:
        """Policy body of :meth:`reserve`; ``granted`` collects ids for failure cleanup."""
        user_id = getattr(user, "user_id", None)
        org_id = getattr(user, "organization_id", None)
        team_id = getattr(user, "team_id", None)
        if team_id is None or org_id is None or user_id is None:
            return self._noop("teamless")
        if not self._flag(str(org_id)):
            return self._noop("flag_off")
        try:
            org_cfg = await self.config_store.get_org(org_id)
            if org_cfg is None or not org_cfg.enabled or org_cfg.cycle_anchor_date is None:
                return self._noop("org_unconfigured")
            team_cfg = await self.config_store.get_team(team_id)
        except QuotaConfigError as exc:
            logger.error("quota config unavailable; failing open: %s", exc)
            self._fault("config_error")
            self.telemetry.decision("degraded", "config_error")
            return QuotaOutcome(blocked=False)
        if team_cfg is None or team_cfg.org_id != org_id:
            return self._noop("team_unconfigured")
        today = self._today()
        if not cycle_started(org_cfg.cycle_anchor_date, today):
            return self._noop("cycle_not_started")
        cycle = cycle_index(org_cfg.cycle_anchor_date, today)

        if self.token_manager.rates_stale():
            await asyncio.to_thread(self.token_manager.refresh_rates_if_stale)
        in_tokens, out_tokens = estimate_io_tokens(messages)
        credits = max(
            1, int(self.token_manager.estimate_credits(model or "", in_tokens, out_tokens))
        )
        providers = self.token_manager.providers_for_model(model or "")
        reservation = team_cfg.reservations.get(user_id)
        decision = await self.limiter.reserve(
            user_id=user_id, team_id=team_id, cycle=cycle, day=day_stamp(today), credits=credits,
            guaranteed=max(0, reservation.guaranteed_credits) if reservation else 0,
            buffer_cap=max(0, team_cfg.shared_buffer_credits),
            daily_subcap=reservation.daily_subcap if reservation else None,
            overrides=[
                o for o in org_cfg.overrides
                if o.id is not None and o.matches_reserve(user_id, model, providers)
            ],
        )  # fmt: skip
        if decision.reservation_id is not None:
            granted.append(decision.reservation_id)
        if decision.degraded or (decision.allowed and decision.reservation_id is None):
            self._fault("limiter_degraded")
            self.telemetry.decision("degraded", "limiter_error")
            return QuotaOutcome(blocked=False)
        if not decision.allowed:
            reason = decision.reason if decision.reason in _KNOWN_REASONS else "unknown"
            self.telemetry.rejection(reason)
            self.telemetry.decision("denied", reason)
            logger.info("quota denied (%s) cycle=%s credits=%d", reason, cycle, credits)
            return QuotaOutcome(blocked=True, reason=decision.reason)
        self.telemetry.decision("allowed")
        self.telemetry.reserved(credits)
        if decision.from_buffer:
            self.telemetry.lease(decision.from_buffer)
        assert decision.reservation_id is not None  # noqa: S101 -- narrowed above for mypy
        return QuotaOutcome(
            blocked=False,
            reservation=QuotaReservation(
                reservation_id=decision.reservation_id,
                org_id=org_id,
                team_id=team_id,
                user_id=user_id,
                cycle=cycle,
                credits_reserved=credits,
                from_reservation=decision.from_reservation,
                from_buffer=decision.from_buffer,
                model_requested=model,
            ),  # fmt: skip
        )

    async def reconcile(
        self,
        reservation: QuotaReservation | None,
        *,
        actual_credits: int,
        provider: str,
        model: str,
        cache_hit: bool = False,
    ) -> ReconcileResult | None:
        """Settle ``reservation`` against actual usage; never raises (returns ``None`` on error)."""
        if reservation is None:
            return None
        try:
            return await self._reconcile(reservation, actual_credits, provider, model, cache_hit)
        except Exception as exc:  # noqa: BLE001 -- reconcile must never fail the response
            logger.error("quota reconcile failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            self._fault("reconcile_error")
            return None

    async def _override_caps(
        self, res: QuotaReservation, provider: str, model: str
    ) -> list[tuple[int, int]]:
        """Caps of the overrides matching the ACTUAL provider:model (empty if config is down)."""
        try:
            org_cfg = await self.config_store.get_org(res.org_id)
        except Exception as exc:  # noqa: BLE001 -- caps are best-effort; the debit still settles
            logger.error("quota override caps skipped (config unavailable): %s", exc)
            self._fault("config_error")
            return []
        if org_cfg is None:
            return []
        return [
            (o.id, o.cap_credits)
            for o in org_cfg.overrides
            if o.id is not None and o.matches_actual(res.user_id, provider, model)
        ]

    async def _reconcile(
        self, res: QuotaReservation, actual_credits: int, provider: str, model: str, cache_hit: bool
    ) -> ReconcileResult:
        """Body of :meth:`reconcile`: caps on the actual provider:model, then the ledger."""
        actual = 0 if cache_hit or provider == "cache" else max(0, int(actual_credits))
        caps = await self._override_caps(res, provider, model) if actual > 0 else []
        result = await self.limiter.reconcile(res.reservation_id, actual, caps)
        self.telemetry.reconciled(result.applied, actual)
        if not result.applied:
            logger.warning(
                "quota reconcile found no reservation (expired or swept); estimate stands"
            )
            return result
        entries = [
            LedgerEntry(SCOPE_USER, res.user_id, result.from_reservation),
            LedgerEntry(SCOPE_BUFFER, res.team_id, result.from_buffer),
            *[LedgerEntry(SCOPE_OVERRIDE, oid, actual) for oid, _cap in caps],
        ]
        entries = [e for e in entries if e.credits > 0]
        if entries:
            try:
                await asyncio.to_thread(self.ledger.add, res.org_id, res.cycle, entries)
            except Exception as exc:  # noqa: BLE001 -- ledger is durable reporting, not enforcement
                # Never retry the whole add: it is not transactional, a retry double-counts.
                logger.error("quota ledger write failed: %s: %s", type(exc).__name__, exc)
                self._fault("ledger_write")
        if result.overage:
            self.telemetry.overage("buffer", result.overage)
            logger.warning("quota buffer overage of %d credits recorded", result.overage)
        if result.over_cap_override_ids:
            self.telemetry.overage("override", len(result.over_cap_override_ids))
            logger.warning(
                "quota override cap exceeded for %d rule(s)", len(result.over_cap_override_ids)
            )
        return result

    async def release(self, reservation: QuotaReservation | None) -> None:
        """Refund a reservation in full (request blocked after reserve); never raises."""
        if reservation is None:
            return
        try:
            result = await self.limiter.release(reservation.reservation_id)
            self.telemetry.reconciled(result.applied, 0)
        except Exception as exc:  # noqa: BLE001 -- release is best-effort; the sweeper is the backstop
            logger.error("quota release failed: %s: %s", type(exc).__name__, exc)
            self._fault("release_failed")
