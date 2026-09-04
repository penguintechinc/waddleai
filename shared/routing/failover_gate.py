"""Two-layer gate for provider-destination failover on the proxy hot path (spec §5.1/§7).

PostHog flag ``waddleai.provider_failover`` (default OFF, fail-safe) AND Enterprise
entitlement ``waddleai_provider_failover`` (fail-closed). Result memoised per org for
``ttl_seconds`` so the request path never blocks on the license server; a lapsed
entitlement degrades to today's behaviour, never to an error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

FAILOVER_FLAG_KEY = "waddleai.provider_failover"
FAILOVER_LICENSE_FEATURE = "waddleai_provider_failover"


def _default_license_getter() -> Any:
    """Instantiate the default license client from environment variables."""
    from penguin_licensing import LicenseClient

    return LicenseClient(
        license_key=os.environ.get("LICENSE_KEY", ""),
        product="waddleai",
        base_url=os.environ.get("LICENSE_SERVER_URL", "https://license.penguintech.io"),
    )


class FailoverGate:
    """Flag + Enterprise entitlement check, memoised per org (spec §5.1)."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        license_getter: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind TTL, an injectable license-client getter, and an injectable clock."""
        self._ttl = ttl_seconds
        self._license_getter = license_getter or _default_license_getter
        self._clock = clock
        self._cache: dict[int, tuple[float, tuple[bool, str]]] = {}

    async def evaluate(self, org_id: int) -> tuple[bool, str]:
        """Return (enabled, reason); reason in ok|flag_off|not_entitled. Never raises."""
        cached = self._cache.get(org_id)
        now = self._clock()
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        result = await self._compute(org_id)
        self._cache[org_id] = (now, result)
        return result

    async def _compute(self, org_id: int) -> tuple[bool, str]:
        """Evaluate flag and entitlement; never raises (fail-closed on error)."""
        if not is_feature_enabled(FAILOVER_FLAG_KEY, distinct_id=str(org_id), default=False):
            return (False, "flag_off")

        def _check() -> bool:
            """Check entitlement; return False on any error (fail-closed)."""
            try:
                return bool(self._license_getter().check_feature(FAILOVER_LICENSE_FEATURE))
            except Exception as exc:
                logger.warning("failover_gate: entitlement check failed (fail-closed): %s", exc)
                return False

        entitled = await asyncio.to_thread(_check)
        return (True, "ok") if entitled else (False, "not_entitled")
