"""
Usage Tracker — records per-request usage metrics and enforces license-gated
quotas.

Writes every completed request to the ``ailb_usage_records`` table and,
when the ``penguin_licensing`` client reports that the
``premium_usage_tracking`` feature is active, applies per-user quotas.
Free-tier deployments are limited to a single distinct user.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
)
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

Base = declarative_base()

# ---------------------------------------------------------------------------
# SQLAlchemy model
# ---------------------------------------------------------------------------


class AILBUsageRecord(Base):  # type: ignore[misc]
    """Persistent per-request usage record."""

    __tablename__ = "ailb_usage_records"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    user_id: str = Column(String(128), nullable=False, index=True)
    api_key_id: str = Column(String(128), nullable=True, index=True)
    model: str = Column(String(128), nullable=False)
    provider: str = Column(String(64), nullable=True)
    input_tokens: int = Column(Integer, nullable=False, default=0)
    output_tokens: int = Column(Integer, nullable=False, default=0)
    total_tokens: int = Column(Integer, nullable=False, default=0)
    latency_ms: float = Column(Float, nullable=True)
    request_id: str = Column(String(128), nullable=True, index=True)
    timestamp: datetime = Column(  # type: ignore[assignment]
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    created_at: datetime = Column(  # type: ignore[assignment]
        DateTime, nullable=False, default=datetime.utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<AILBUsageRecord user={self.user_id} model={self.model} "
            f"tokens={self.total_tokens}>"
        )


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
    api_key_id: Optional[str] = None
    provider: Optional[str] = None
    latency_ms: Optional[float] = None
    request_id: Optional[str] = None


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

    def __init__(self, db, license_client=None) -> None:  # type: ignore[type-arg]
        self._db = db
        self._license_client = license_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_usage(self, report: UsageReport) -> UsageAck:
        """Persist a usage record and check quotas.

        Steps:
            1. Verify the user is allowed (free-tier = max 1 distinct user).
            2. Check per-user quota if premium tracking is licensed.
            3. Insert the usage record into ``ailb_usage_records``.

        Args:
            report: The :class:`UsageReport` for the completed request.

        Returns:
            A :class:`UsageAck` indicating whether the record was accepted.
        """
        is_premium = self._has_premium()

        # --- free-tier user cap ---
        if not is_premium:
            exceeded, msg = self._check_free_tier_user_cap(report.user_id)
            if exceeded:
                return UsageAck(
                    accepted=False,
                    quota_exceeded=True,
                    message=msg,
                )

        # --- per-user quota (premium only) ---
        if is_premium:
            exceeded, msg = self._check_user_quota(report.user_id)
            if exceeded:
                return UsageAck(
                    accepted=False,
                    quota_exceeded=True,
                    message=msg,
                )

        # --- persist the record ---
        try:
            self._db.executesql(
                "INSERT INTO ailb_usage_records "
                "(user_id, api_key_id, model, provider, input_tokens, "
                " output_tokens, total_tokens, latency_ms, request_id, "
                " timestamp, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    report.user_id,
                    report.api_key_id,
                    report.model,
                    report.provider,
                    report.input_tokens,
                    report.output_tokens,
                    report.total_tokens,
                    report.latency_ms,
                    report.request_id,
                    datetime.utcnow(),
                    datetime.utcnow(),
                ),
            )
            logger.info(
                "Recorded usage: user=%s model=%s tokens=%d",
                report.user_id,
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
    # License helpers
    # ------------------------------------------------------------------

    def _has_premium(self) -> bool:
        """Return True when the premium_usage_tracking feature is licensed."""
        if self._license_client is None:
            return False
        try:
            return bool(
                self._license_client.has_feature("premium_usage_tracking")
            )
        except Exception as exc:
            logger.warning("License check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Quota checks
    # ------------------------------------------------------------------

    def _check_free_tier_user_cap(
        self, user_id: str
    ) -> tuple[bool, str]:
        """Enforce the free-tier single-user limit.

        Returns ``(exceeded, message)``.
        """
        try:
            rows = self._db.executesql(
                "SELECT DISTINCT user_id FROM ailb_usage_records "
                "LIMIT %s",
                (_FREE_TIER_MAX_USERS + 1,),
            )
            existing_users = {str(r[0]) for r in rows} if rows else set()

            if user_id in existing_users:
                return False, ""

            if len(existing_users) >= _FREE_TIER_MAX_USERS:
                return True, (
                    f"Free tier limited to {_FREE_TIER_MAX_USERS} user(s). "
                    "Upgrade your license for unlimited users."
                )
            return False, ""
        except Exception as exc:
            logger.error("Free-tier user cap check failed: %s", exc)
            # Fail open — allow the request rather than blocking on error
            return False, ""

    def _check_user_quota(
        self, user_id: str
    ) -> tuple[bool, str]:
        """Check per-user monthly token quota (premium feature).

        The quota is read from the ``users`` table
        (``token_quota_monthly`` column).  If no quota is configured the
        check passes.

        Returns ``(exceeded, message)``.
        """
        try:
            # Read the user's monthly quota
            user_rows = self._db.executesql(
                "SELECT token_quota_monthly FROM users WHERE id = %s "
                "OR username = %s LIMIT 1",
                (user_id, user_id),
            )
            if not user_rows or user_rows[0][0] is None:
                return False, ""

            monthly_quota: int = int(user_rows[0][0])

            # Sum tokens consumed this calendar month
            usage_rows = self._db.executesql(
                "SELECT COALESCE(SUM(total_tokens), 0) "
                "FROM ailb_usage_records "
                "WHERE user_id = %s "
                "  AND timestamp >= date_trunc('month', CURRENT_TIMESTAMP)",
                (user_id,),
            )
            used: int = int(usage_rows[0][0]) if usage_rows else 0

            if used >= monthly_quota:
                return True, (
                    f"Monthly token quota exceeded ({used}/{monthly_quota}). "
                    "Contact your administrator to increase the limit."
                )
            return False, ""
        except Exception as exc:
            logger.error("User quota check failed: %s", exc)
            return False, ""
