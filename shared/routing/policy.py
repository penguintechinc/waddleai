"""Org routing policy -- filter + mode-sort = the fallback chain (spec §7.1, §7.3).

``routing_policies`` per org: mode, escalation thresholds, classifier_prompt
(absorbs the legacy NL routing-instructions UX), de_escalation, sensitivity,
budget-pressure toggle, provider_failover. Policy filters (allow-lists, tier
caps) then sorts qualified candidates by mode -- the sorted list *is* the
fallback chain, so failover never lands on a model that couldn't serve the
request.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from shared.routing.capability import ModelOffer

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "waddleai:route:policy"
_DEFAULT_CACHE_TTL = 300

_VALID_MODES = frozenset({"local_only", "local_first", "commercial_only", "cost", "latency"})


@dataclass(slots=True)
class RoutingPolicyConfig:
    """Resolved org routing policy (defaults applied when no row exists)."""

    mode: str = "local_first"
    escalation_threshold: int = 3
    escalation_target: str | None = None
    classifier_prompt: str | None = None
    de_escalation: str = "idle_reset"
    idle_reset_minutes: int = 10
    sensitivity_routing: str = "local_only"
    budget_pressure_enabled: bool = True
    provider_failover: str = "off"


_FIELDS = (
    "mode",
    "escalation_threshold",
    "escalation_target",
    "classifier_prompt",
    "de_escalation",
    "idle_reset_minutes",
    "sensitivity_routing",
    "budget_pressure_enabled",
    "provider_failover",
)


def _cache_key(org_id: int) -> str:
    """Build the Valkey cache key for an org's resolved policy."""
    return f"{_CACHE_PREFIX}:{org_id}"


class PolicyResolver:
    """Resolves routing_policies rows into RoutingPolicyConfig, Valkey-cached."""

    def __init__(self, db: Any, valkey: Any = None, cache_ttl: int = _DEFAULT_CACHE_TTL) -> None:
        """Initialize the resolver.

        Args:
            db: penguin-dal DB instance exposing a ``routing_policies`` table.
            valkey: Optional redis.asyncio-compatible client for caching.
            cache_ttl: Cache entry TTL in seconds.
        """
        self.db = db
        self.valkey = valkey
        self.cache_ttl = cache_ttl

    async def resolve(self, org_id: int) -> RoutingPolicyConfig:
        """Resolve an org's routing policy, defaulting when no row exists."""
        cache_key = _cache_key(org_id)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        config = await asyncio.to_thread(self._fetch, org_id)
        await self._cache_set(cache_key, config)
        return config

    def _fetch(self, org_id: int) -> RoutingPolicyConfig:
        """Synchronous penguin-dal lookup; missing row returns engine defaults."""
        row = self.db(self.db.routing_policies.organization_id == org_id).select().first()
        if row is None:
            return RoutingPolicyConfig()
        overrides = {f: getattr(row, f) for f in _FIELDS if getattr(row, f, None) is not None}
        return RoutingPolicyConfig(**overrides)

    async def invalidate(self, org_id: int) -> None:
        """Clear the cached policy for an org, called on Management writes."""
        if self.valkey is None:
            return
        try:
            await self.valkey.delete(_cache_key(org_id))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PolicyResolver: cache invalidation failed: %s", exc)

    async def _cache_get(self, key: str) -> RoutingPolicyConfig | None:
        if self.valkey is None:
            return None
        try:
            raw = await self.valkey.get(key)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PolicyResolver: cache read failed: %s", exc)
            return None
        if raw is None:
            return None
        try:
            return RoutingPolicyConfig(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    async def _cache_set(self, key: str, config: RoutingPolicyConfig) -> None:
        if self.valkey is None:
            return
        try:
            payload = json.dumps({f: getattr(config, f) for f in _FIELDS})
            await self.valkey.set(key, payload, ex=self.cache_ttl)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PolicyResolver: cache write failed: %s", exc)


def _passes_filters(
    offer: ModelOffer,
    allowed_models: set[str] | None,
    tier_cap: float | None,
) -> bool:
    """True when offer survives the org allow-list and tier-cost cap filters."""
    if allowed_models is not None and offer.model_name not in allowed_models:
        return False
    if tier_cap is not None and offer.cost_per_token > tier_cap:
        return False
    return True


def _mode_sort_key(mode: str, latency_by_model: dict[str, float]):
    """Build the sort key function for a given policy mode."""
    if mode == "cost":
        return lambda o: (o.cost_per_token,)
    if mode == "latency":
        return lambda o: (latency_by_model.get(o.model_name, float("inf")),)
    if mode == "local_first":
        return lambda o: (0 if o.location == "local" else 1, o.cost_per_token)
    if mode == "commercial_only":
        return lambda o: (o.cost_per_token,)
    if mode == "local_only":
        return lambda o: (o.cost_per_token,)
    # Unknown mode: fall back to local_first ordering rather than raising.
    return lambda o: (0 if o.location == "local" else 1, o.cost_per_token)


def filter_and_sort(
    candidates: list[ModelOffer],
    policy: RoutingPolicyConfig,
    allowed_models: set[str] | None = None,
    tier_cap: float | None = None,
    latency_by_model: dict[str, float] | None = None,
) -> list[ModelOffer]:
    """Filter candidates by allow-list/tier cap, then sort by policy mode.

    The returned, ordered list *is* the fallback chain -- failover never
    lands on a model that couldn't serve the request because every entry
    already passed capability matching before reaching this function.

    Args:
        candidates: Capability-qualified offers (post capability.qualifies()).
        policy: The resolved org policy.
        allowed_models: Optional org allow-list; None means no allow-list filter.
        tier_cap: Optional max cost-per-token for the org's license tier.
        latency_by_model: EMA latency lookup for mode="latency" sorting.

    Returns:
        Ordered offers: mode="local_only" drops commercial entries entirely,
        mode="commercial_only" drops local entries, other modes keep both
        but order by the mode's ranking key.
    """
    mode = policy.mode if policy.mode in _VALID_MODES else "local_first"
    filtered = [o for o in candidates if _passes_filters(o, allowed_models, tier_cap)]

    if mode == "local_only":
        filtered = [o for o in filtered if o.location == "local"]
    elif mode == "commercial_only":
        filtered = [o for o in filtered if o.location == "commercial"]

    key = _mode_sort_key(mode, latency_by_model or {})
    return sorted(filtered, key=key)
