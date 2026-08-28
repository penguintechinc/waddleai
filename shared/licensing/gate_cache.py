"""Shared cache-entry shape for per-key TTL-cached license/feature-flag gates.

Any request-hot license-gated check (``ContentFilter``'s NER tier,
``UsageTracker``'s premium-tier check, ...) is a blocking HTTP round trip
(PostHog and/or the license server) that must run off the event loop via
``asyncio.to_thread`` and must not pay that round trip on every single
request. Both call sites cache "was this entitled, and when did we last
check" the same way -- this module holds only the shared entry shape, not
the caching control flow itself (each call site's fail-open/fail-closed
direction differs, so that logic stays local; see ``ContentFilter.
_ner_tier_enabled`` and ``UsageTracker._has_premium`` for the two mirrored
implementations).
"""

from dataclasses import dataclass


@dataclass(slots=True)
class LicenseGateCacheEntry:
    """One cached license/feature-flag gate result, keyed by caller (e.g. org id).

    ``checked_at`` is a ``time.monotonic()`` timestamp, compared against a
    caller-chosen TTL to decide whether to re-check or serve this value.
    """

    enabled: bool
    checked_at: float


__all__ = ["LicenseGateCacheEntry"]
