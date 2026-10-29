"""Flag + license gate for the classification service's licensed NER tier.

Decides per request whether NER may run: PostHog flag AND license entitlement,
cached per distinct id and fail-safe to DISABLED. Never raises into callers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from services.classification.app.obs import get_logger
from shared.licensing.gate_cache import LicenseGateCacheEntry

log = get_logger(__name__)

NER_TIER_FLAG = "waddleai.pii_ner"
NER_TIER_LICENSE_FEATURE = "pii_ner_detection"
NER_TIER_CACHE_TTL = 60.0


class NerTierGate:
    """Cached, off-loop NER-tier gate.

    Semantics are COPIED from ``ContentFilter._ner_tier_enabled`` /
    ``_check_ner_tier_entitlement`` in shared/security/content_filter.py.
    The copy is deliberate and temporary: both services run until P6 deletes
    the Python original; the T12 parity oracle is the drift guard.
    """

    def __init__(self, license_client: Any = None, features: Any = None) -> None:
        """Bind a ``penguin_licensing`` client and flag helper (None = unlicensed/no flag)."""
        self._license_client = license_client
        self._features = features
        self._cache: dict[str, LicenseGateCacheEntry] = {}

    async def enabled(self, flag_distinct_id: str) -> bool:
        """Return True only if the flag AND license entitlement are both on.

        Blocking checks run via ``asyncio.to_thread``; results are cached 60 s per id.
        Any checker error yields the last cached value, else disabled; never raises.
        """
        now = time.monotonic()
        cached = self._cache.get(flag_distinct_id)
        if cached is not None and now - cached.checked_at < NER_TIER_CACHE_TTL:
            return cached.enabled
        try:
            enabled = await asyncio.to_thread(self._check, flag_distinct_id)
        except Exception as exc:
            fallback = cached.enabled if cached is not None else False
            log.warning(
                "ner_tier_gate_check_failed",
                error_type=type(exc).__name__,
                fallback=fallback,
                had_cache=cached is not None,
            )
            return fallback
        self._cache[flag_distinct_id] = LicenseGateCacheEntry(enabled=enabled, checked_at=now)
        return enabled

    def _check(self, flag_distinct_id: str) -> bool:
        """Synchronous (blocking) flag + entitlement check; call only via to_thread."""
        if self._features is not None and not self._features.is_feature_enabled(
            NER_TIER_FLAG, distinct_id=flag_distinct_id
        ):
            return False
        if self._license_client is None:
            return False
        return bool(self._license_client.check_feature(NER_TIER_LICENSE_FEATURE))
