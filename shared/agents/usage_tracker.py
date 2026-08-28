"""Usage Tracker — records per-request usage metrics and enforces license-gated quotas.

Writes every completed request into the ``token_usage`` table via
penguin-dal (query-builder calls, never raw SQL) and, when the
``penguin_licensing`` client reports that the ``premium_usage_tracking``
feature is active, applies per-user quotas. Free-tier deployments are
limited to a single distinct user **per organization**.

Historically this wrote to a dedicated AILB per-request usage table with a
global (deployment-wide, not tenant-scoped) free-tier user cap. Migration
007 dropped that table -- it had no successor raw-event log, only the
``token_usage`` aggregate this module now reads and writes directly. The
free-tier/quota checks are now organization-scoped: every query filters on
the caller's resolved ``organization_id`` so one tenant's usage/user-count
can never leak into another tenant's quota decision. Folded historical rows
(``token_usage.source == 'ailb_import'``) are ordinary rows to every query
here -- there is no ``source`` filter, so they count toward both checks.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.licensing.gate_cache import LicenseGateCacheEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageReport:
    """Inbound usage report submitted after a request completes."""

    user_id: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    api_key_id: str | None = None
    provider: str | None = None
    latency_ms: float | None = None
    request_id: str | None = None


@dataclass(slots=True, frozen=True)
class UsageAck:
    """Acknowledgement returned to the caller after recording usage."""

    accepted: bool
    quota_exceeded: bool
    message: str


# ---------------------------------------------------------------------------
# Free-tier limit
# ---------------------------------------------------------------------------

_FREE_TIER_MAX_USERS = 1

# premium_usage_tracking is a deployment-wide licence entitlement, not
# per-org, so a single cached result covers every org -- unlike
# ContentFilter's NER-tier gate, which also folds in a per-org PostHog flag.
# The check itself is a blocking HTTP round trip (penguin_licensing.
# LicenseClient.check_feature), so it's cached for this TTL rather than
# paid on every recorded usage report.
_PREMIUM_TIER_CACHE_TTL = 60.0
_PREMIUM_TIER_FEATURE = "premium_usage_tracking"


# ---------------------------------------------------------------------------
# UsageTracker
# ---------------------------------------------------------------------------


class UsageTracker:
    """Record usage and enforce license-based quotas.

    Args:
        db: A penguin-dal (PyDAL-compatible) database connection.
        license_client: Optional ``penguin_licensing`` client instance.
            When provided, premium features (per-user quotas, unlimited
            users) are gated behind the ``premium_usage_tracking`` feature
            flag.

    """

    def __init__(self, db: Any, license_client: Any = None) -> None:
        """Bind the tracker to *db* and an optional license client.

        See class docstring for argument details.
        """
        self._db = db
        self._license_client = license_client
        self._premium_cache: LicenseGateCacheEntry | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_usage(self, report: UsageReport) -> UsageAck:
        """Persist a usage record and check quotas.

        The premium-tier licence gate is resolved first (``_has_premium`` --
        async, TTL-cached), so a cache hit costs nothing beyond a dict
        lookup and only a cache miss dispatches a thread. DB access is
        synchronous (penguin-dal/PyDAL) so that part of the work always
        runs in a thread via ``asyncio.to_thread`` to avoid blocking the
        event loop.

        Args:
            report: The :class:`UsageReport` for the completed request.

        Returns:
            A :class:`UsageAck` indicating whether the record was accepted.

        """
        is_premium = await self._has_premium()
        return await asyncio.to_thread(self._record_usage_sync, report, is_premium)

    # ------------------------------------------------------------------
    # Synchronous core (runs off the event loop via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _record_usage_sync(self, report: UsageReport, is_premium: bool) -> UsageAck:
        user_id, organization_id = self._resolve_identity(report.user_id)

        if user_id is None or organization_id is None:
            # Fail closed: without a resolved tenant we cannot scope a
            # quota/free-tier query safely, and a global fallback query
            # would leak across organizations.
            logger.warning(
                "record_usage: could not resolve organization for user_id=%s",
                report.user_id,
            )
            return UsageAck(
                accepted=False,
                quota_exceeded=False,
                message="Unable to resolve organization for usage report.",
            )

        if not is_premium:
            exceeded, msg = self._check_free_tier_user_cap(organization_id, user_id)
            if exceeded:
                return UsageAck(accepted=False, quota_exceeded=True, message=msg)
        else:
            exceeded, msg = self._check_user_quota(organization_id, user_id)
            if exceeded:
                return UsageAck(accepted=False, quota_exceeded=True, message=msg)

        try:
            llm_tokens = None
            if report.model:
                llm_tokens = json.dumps(
                    {
                        f"{report.provider}:{report.model}": {
                            "input_tokens": report.input_tokens,
                            "output_tokens": report.output_tokens,
                        }
                    }
                )

            self._db.token_usage.insert(
                virtual_key_id=None,
                user_id=user_id,
                organization_id=organization_id,
                date=datetime.utcnow(),
                waddleai_tokens=0,  # Normalized-token conversion is TokenManager's job, not ours.
                llm_tokens=llm_tokens,
                tokens_input_total=report.input_tokens,
                tokens_output_total=report.output_tokens,
                request_count=1,
                cost_usd_total=0,
                last_updated=datetime.utcnow(),
                source="aiproxy",
                estimated=False,
            )
            self._db.commit()

            logger.info(
                "Recorded usage: user_id=%s org_id=%s model=%s tokens=%d",
                user_id,
                organization_id,
                report.model,
                report.total_tokens,
            )
            return UsageAck(
                accepted=True,
                quota_exceeded=False,
                message="Usage recorded successfully.",
            )
        except Exception as exc:
            logger.error("Failed to record usage: %s", exc)
            return UsageAck(
                accepted=False,
                quota_exceeded=False,
                message=f"Database error: {exc}",
            )

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    def _resolve_identity(self, user_id_str: str) -> tuple[int | None, int | None]:
        """Resolve a report's external ``user_id`` string to (users.id, organization_id).

        Tries a numeric ``users.id`` match first, then falls back to
        ``users.username``. Fails closed to ``(None, None)`` on any lookup
        failure or unresolved identity -- callers must never run an
        unscoped, cross-tenant query when identity can't be established.
        """
        if not user_id_str:
            return None, None
        try:
            user = None
            try:
                uid = int(user_id_str)
            except (TypeError, ValueError):
                uid = None

            if uid is not None:
                user = self._db(self._db.users.id == uid).select().first()
            if user is None:
                user = self._db(self._db.users.username == user_id_str).select().first()
            if user is None:
                return None, None

            return user.id, user.organization_id
        except Exception as exc:
            logger.error("Failed to resolve identity for user_id=%s: %s", user_id_str, exc)
            return None, None

    # ------------------------------------------------------------------
    # License helpers
    # ------------------------------------------------------------------

    async def _has_premium(self) -> bool:
        """Return True when the premium_usage_tracking feature is licensed.

        Mirrors ``ContentFilter._ner_tier_enabled``'s cache + last-known
        fallback pattern (``shared.licensing.gate_cache.LicenseGateCacheEntry``):
        the actual check is a blocking HTTP round trip (``penguin_licensing.
        LicenseClient.check_feature`` -- there is no ``has_feature`` method),
        so it runs off the event loop via ``asyncio.to_thread`` and is cached
        for ``_PREMIUM_TIER_CACHE_TTL`` seconds -- otherwise every recorded
        usage report would pay for a licence-server round trip.

        Unlike the NER-tier gate, the safe failure direction here is the
        *stricter* one: on a checker failure with nothing cached yet, this
        returns False (free-tier caps apply) rather than True -- granting
        unearned premium quota on an error would be the wrong default for a
        quota check, the opposite of content_filter's PII-detection gate. A
        checker failure *after* a successful prior check still serves the
        last-known value rather than snapping every transient licence-server
        hiccup straight to the free-tier cap.
        """
        if self._license_client is None:
            return False

        now = time.monotonic()
        cached = self._premium_cache
        if cached is not None and now - cached.checked_at < _PREMIUM_TIER_CACHE_TTL:
            return cached.enabled

        try:
            enabled = await asyncio.to_thread(self._check_premium_entitlement)
        except Exception as exc:
            fallback = cached.enabled if cached is not None else False
            if cached is not None:
                fallback_desc = f"cached value ({fallback})"
            else:
                fallback_desc = "not-premium (never checked)"
            logger.warning(
                "Premium usage-tracking licence check failed; falling back to %s: %s",
                fallback_desc,
                exc,
            )
            return fallback

        self._premium_cache = LicenseGateCacheEntry(enabled=enabled, checked_at=now)
        return enabled

    def _check_premium_entitlement(self) -> bool:
        """Synchronous licence check -- call only via ``asyncio.to_thread`` from ``_has_premium``.

        Callers must check ``self._license_client is not None`` first.
        """
        return bool(self._license_client.check_feature(_PREMIUM_TIER_FEATURE))

    # ------------------------------------------------------------------
    # Quota checks (organization-scoped)
    # ------------------------------------------------------------------

    def _check_free_tier_user_cap(self, organization_id: int, user_id: int) -> tuple[bool, str]:
        """Enforce the free-tier single-user-per-organization limit.

        Scoped to ``organization_id``: distinct users are counted within
        this tenant's ``token_usage`` rows only, never across the whole
        deployment.

        Returns ``(exceeded, message)``.
        """
        try:
            rows = self._db(self._db.token_usage.organization_id == organization_id).select(
                self._db.token_usage.user_id,
                distinct=True,
                limitby=(0, _FREE_TIER_MAX_USERS + 1),
            )
            existing_users = {r.user_id for r in rows} if rows else set()

            if user_id in existing_users:
                return False, ""

            if len(existing_users) >= _FREE_TIER_MAX_USERS:
                return True, (
                    f"Free tier limited to {_FREE_TIER_MAX_USERS} user(s) per organization. "
                    "Upgrade your license for unlimited users."
                )
            return False, ""
        except Exception as exc:
            logger.error("Free-tier user cap check failed: %s", exc)
            # Fail open — allow the request rather than blocking on error
            return False, ""

    def _check_user_quota(self, organization_id: int, user_id: int) -> tuple[bool, str]:
        """Check per-user monthly token quota (premium feature), organization-scoped.

        The quota is read from ``users.token_quota_monthly``; usage is
        summed from ``token_usage`` (``tokens_input_total +
        tokens_output_total``) for the current calendar month, filtered to
        this ``user_id`` *and* this ``organization_id``. If no quota is
        configured the check passes.

        Returns ``(exceeded, message)``.
        """
        try:
            user_query = (self._db.users.id == user_id) & (
                self._db.users.organization_id == organization_id
            )
            user = self._db(user_query).select().first()
            if user is None or not user.token_quota_monthly:
                return False, ""

            monthly_quota = int(user.token_quota_monthly)
            now = datetime.utcnow()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            usage_query = (
                (self._db.token_usage.user_id == user_id)
                & (self._db.token_usage.organization_id == organization_id)
                & (self._db.token_usage.date >= month_start)
            )
            rows = self._db(usage_query).select(
                self._db.token_usage.tokens_input_total,
                self._db.token_usage.tokens_output_total,
            )

            used = sum((r.tokens_input_total or 0) + (r.tokens_output_total or 0) for r in rows)

            if used >= monthly_quota:
                return True, (
                    f"Monthly token quota exceeded ({used}/{monthly_quota}). "
                    "Contact your administrator to increase the limit."
                )
            return False, ""
        except Exception as exc:
            logger.error("User quota check failed: %s", exc)
            return False, ""
