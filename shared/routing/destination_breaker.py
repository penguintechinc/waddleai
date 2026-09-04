"""Per-destination circuit breaker (spec §5.6).

Reuses request_router.ProviderStats's closed->open->half-open state shape, keyed
``dest:{id}``, with its OWN parameters (failure_threshold=3, cooldown=60s) and fed
by FailoverDispatcher on every attempt — the first live consumer of the breaker.
In-process per replica; the Valkey-shared breaker (platform-spec §5.3.4) is a
documented follow-up, and each replica still fails over correctly on its own evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from shared.utils.request_router import ProviderStats


@dataclass(slots=True)
class DestinationBreaker:
    """Closed/open/half-open breaker per destination id, fed by the dispatcher."""

    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    clock: Callable[[], datetime] = field(default=datetime.utcnow)
    _stats: dict[int, ProviderStats] = field(default_factory=dict)

    def _s(self, dest_id: int) -> ProviderStats:
        return self._stats.setdefault(dest_id, ProviderStats())

    def _in_cooldown(self, s: ProviderStats) -> bool:
        return (
            s.last_failure is not None
            and (not s.last_success or s.last_failure > s.last_success)
            and (self.clock() - s.last_failure) < timedelta(seconds=self.cooldown_seconds)
        )

    def is_open(self, dest_id: int) -> bool:
        """True while the destination is tripped and inside its cooldown window."""
        s = self._s(dest_id)
        if s.consecutive_failures < self.failure_threshold:
            return False
        return self._in_cooldown(s)

    def reserve_probe(self, dest_id: int) -> bool:
        """Reserve the single half-open probe once cooldown has elapsed; False if unavailable."""
        s = self._s(dest_id)
        if s.consecutive_failures < self.failure_threshold:
            return True  # closed -> not gated
        if self._in_cooldown(s):
            return False  # open
        if s.half_open_probe_in_flight:
            return False  # probe already taken
        s.half_open_probe_in_flight = True
        return True

    def record_success(self, dest_id: int) -> None:
        """Reset the breaker for this destination after a successful attempt."""
        s = self._s(dest_id)
        s.consecutive_failures = 0
        s.last_success = self.clock()
        s.half_open_probe_in_flight = False

    def record_failure(self, dest_id: int) -> None:
        """Record a retryable failure; trips the breaker at the threshold."""
        s = self._s(dest_id)
        s.consecutive_failures += 1
        s.last_failure = self.clock()
        s.half_open_probe_in_flight = False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Serialisable breaker state for /api/routing/stats (no secrets)."""
        return {
            f"dest:{dest_id}": {
                "consecutive_failures": s.consecutive_failures,
                "open": self.is_open(dest_id),
                "last_failure": s.last_failure.isoformat() if s.last_failure else None,
                "last_success": s.last_success.isoformat() if s.last_success else None,
            }
            for dest_id, s in self._stats.items()
        }
