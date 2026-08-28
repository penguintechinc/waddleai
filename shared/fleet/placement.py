"""Placement engine: session affinity, hot-model pinning, lazy pull, origin deny-list.

Spec §10.4: fleet backends report per-node loaded models + VRAM headroom;
the §7 routing engine consults ``endpoints_for(model)`` and balances with
session affinity (KV-cache reuse, §6.3); placement pins hot models per node
class and lazy-pulls cold models; ``place_model`` validates the §2.2 origin
deny-list and enforces the Task 6 (``shared.fleet.caps``) Free-tier caps +
VRAM capacity before ever calling a backend.

This module deliberately does not re-implement capability scoring or model
*selection* -- that remains ``shared.routing`` (``RoutingEngine``,
``shared.routing.capability.ModelOffer``) territory (spec §7.6). What this
module feeds into that machinery is ``annotate_offers``: for a
``location="local"`` offer, whether any fleet endpoint currently serves the
model becomes ``ModelOffer.available``, so a model with no live local
capacity is correctly vetoed/rerouted by the existing capability-veto path
(``shared.routing.capability.veto_and_reroute``) rather than this module
duplicating that veto logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from shared.fleet.base import (
    Endpoint,
    InferenceFleetBackend,
    ManagementScope,
    ModelPlacement,
    NodeInfo,
)
from shared.fleet.caps import (
    CapEnforcer,
    InsufficientCapacityError,
    select_capable_node,
)
from shared.routing.aliases import split_provider_prefix
from shared.routing.capability import ModelOffer

logger = logging.getLogger(__name__)

_AFFINITY_TTL_SECONDS = 1800  # 30 minutes, matches shared.cache.affinity's default window
_AFFINITY_KEY_PREFIX = "waddleai:fleet:affinity"

# §2.2 supply-chain origin policy: no models of Chinese origin. Matched
# case-insensitively as a substring of model_registry.origin. This is a
# hard, unconditional deny -- the §2.2a admin-gated generative-media
# acknowledged-risk exception is explicitly out of scope here (it requires
# its own per-model acceptance/audit flow, not a placement-time bypass) and
# is NOT implemented by this list.
#
# Includes both the organizations spec §2.2 names directly (Alibaba/Qwen,
# DeepSeek, Zhipu AI/GLM/ChatGLM/CogVideoX, 01.AI/Yi, Moonshot/Kimi,
# MiniMax, Kuaishou/Kolors) and the two entities spec §2.2 explicitly calls
# out for treatment despite non-PRC incorporation (HPC-AI Tech / Open-Sora,
# Beijing Luchen Technology) plus other major PRC AI labs likely to appear
# in a model_registry.origin field (Baidu/ERNIE, Tencent, ByteDance,
# ByteDance's ByteDance-Seed, iFlytek, SenseTime, Baichuan, StepFun).
_DENIED_ORIGINS: frozenset[str] = frozenset(
    o.lower()
    for o in (
        "Alibaba",
        "Qwen",
        "DeepSeek",
        "Zhipu AI",
        "Z.ai",
        "GLM",
        "ChatGLM",
        "CogVideoX",
        "01.AI",
        "01AI",
        "Moonshot AI",
        "Kimi",
        "MiniMax",
        "Kuaishou",
        "Kolors",
        "HPC-AI Tech",
        "Beijing Luchen Technology",
        "Baidu",
        "ERNIE",
        "Tencent",
        "ByteDance",
        "iFlytek",
        "SenseTime",
        "Baichuan",
        "StepFun",
    )
)


def is_denied_origin(origin: str | None) -> bool:
    """True when ``origin`` matches the §2.2 PRC-origin deny-list (case-insensitive substring)."""
    if not origin:
        return False
    lowered = origin.lower()
    return any(denied in lowered for denied in _DENIED_ORIGINS)


@dataclass(slots=True)
class ModelRegistryEntry:
    """The subset of a ``model_registry`` row placement decisions need."""

    name: str
    origin: str
    is_utility: bool
    min_vram: int | None


def _bare_model_name(model: str) -> str:
    """Strip a provider prefix (e.g. ``ollama:gemma4:e2b`` -> ``gemma4:e2b``).

    Used for registry/cap lookups; the unstripped ``model`` is still what
    gets passed through to the backend.
    """
    _, bare = split_provider_prefix(model)
    return bare


class PlacementEngine:
    """Aggregates fleet-backend endpoints and enforces placement-time policy.

    Constructed once and shared across every organization's requests --
    same lifecycle as ``shared.routing.engine.RoutingEngine``, which this
    class deliberately mirrors: nothing org-specific is bound at
    construction, ``org_id`` is a per-call argument (``place_model``)
    instead, so one long-lived instance is safe to reuse across tenants
    rather than needing to be rebuilt per request/org.

    Args:
        db: penguin-dal DB instance exposing ``model_registry``,
            ``fleet_backends``, ``ollama_deployments``/``ollama_models``,
            and ``llamacpp_deployments`` (the tables ``shared.fleet.caps``
            queries).
        valkey: Optional redis.asyncio-compatible client for the session
            affinity map; affinity is skipped (falls through to plain
            load-balancing) when ``None``.
        license_client: A ``penguin_licensing.LicenseClient`` (or any
            object exposing a synchronous ``validate()``), passed through
            to a fresh org-scoped ``shared.fleet.caps.CapEnforcer`` on each
            ``place_model`` call.
        hot_model_pins: Optional ``{model_name: node_id}`` map of
            operator-configured "keep this model resident on this node"
            pins (§10.4 "pin hot models per node class"). The landed §10.1
            ``NodeInfo``/``Endpoint`` dataclasses carry no node-class field,
            so a pin targets a specific node id rather than a class of
            nodes -- the coarser grouping the spec describes is achieved by
            an operator pointing multiple hot models at nodes provisioned
            from the same node class.
        ttl_seconds: Session-affinity TTL, sliding on every successful
            lookup (mirrors ``shared.cache.affinity.SessionAffinityMap``).

    """

    def __init__(
        self,
        db: Any,
        valkey: Any,
        license_client: Any,
        hot_model_pins: dict[str, str] | None = None,
        ttl_seconds: int = _AFFINITY_TTL_SECONDS,
    ) -> None:
        """Bind shared (non-org-specific) dependencies -- see the class docstring."""
        self.db = db
        self.valkey = valkey
        self.license_client = license_client
        self.hot_model_pins = hot_model_pins or {}
        self.ttl_seconds = ttl_seconds

    def _cap_enforcer(self, org_id: int) -> CapEnforcer:
        """Build a fresh org-scoped CapEnforcer -- cheap, no I/O until called."""
        return CapEnforcer(self.db, self.license_client, org_id)

    # -- endpoint aggregation -------------------------------------------------

    async def endpoints_for(
        self, model: str, backends: list[InferenceFleetBackend]
    ) -> list[Endpoint]:
        """Aggregate ``endpoints_for(model)`` across every given backend (§10.4).

        A single backend failing (network error, unhealthy cluster) never
        fails the whole aggregation -- it just contributes no endpoints.
        """
        endpoints: list[Endpoint] = []
        for backend in backends:
            try:
                endpoints.extend(await backend.endpoints_for(model))
            except Exception as exc:  # pragma: no cover - defensive, backend I/O failure
                logger.warning(
                    "PlacementEngine: endpoints_for failed for backend_id=%s: %s",
                    getattr(backend, "fleet_backend_id", "?"),
                    exc,
                )
        return endpoints

    # -- endpoint selection: affinity -> hot-pin -> load-balanced -----------

    async def select_endpoint(
        self, model: str, session_id: str | None, endpoints: list[Endpoint]
    ) -> Endpoint | None:
        """Pick one endpoint from ``endpoints`` for this request.

        Priority: (1) the session's pinned node, if present and healthy;
        (2) the operator-configured hot-model pin for ``model``, if present
        and healthy; (3) the least-loaded healthy endpoint. A newly-made
        choice is recorded as the session's affinity target (when a
        ``session_id`` was given) so subsequent turns of the same
        conversation land on the same node's KV cache.
        """
        if not endpoints:
            return None

        if session_id:
            pinned_node_id = await self._affinity_lookup(session_id)
            if pinned_node_id is not None:
                for endpoint in endpoints:
                    if endpoint.node_id == pinned_node_id and endpoint.healthy:
                        await self._affinity_record(session_id, endpoint.node_id)
                        return endpoint

        chosen = self._choose(model, endpoints)
        if session_id and chosen is not None:
            await self._affinity_record(session_id, chosen.node_id)
        return chosen

    def _choose(self, model: str, endpoints: list[Endpoint]) -> Endpoint | None:
        healthy = [e for e in endpoints if e.healthy]
        pool = healthy or endpoints
        if not pool:
            return None

        pinned_node_id = self.hot_model_pins.get(model)
        if pinned_node_id is not None:
            for endpoint in pool:
                if endpoint.node_id == pinned_node_id:
                    return endpoint

        return min(pool, key=lambda e: len(e.loaded_models))

    @staticmethod
    def _affinity_key(session_id: str) -> str:
        return f"{_AFFINITY_KEY_PREFIX}:{session_id}"

    async def _affinity_lookup(self, session_id: str) -> str | None:
        if self.valkey is None:
            return None
        try:
            raw = await self.valkey.get(self._affinity_key(session_id))
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PlacementEngine: affinity lookup failed: %s", exc)
            return None
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def _affinity_record(self, session_id: str, node_id: str) -> None:
        if self.valkey is None:
            return
        try:
            await self.valkey.set(self._affinity_key(session_id), node_id, ex=self.ttl_seconds)
        except Exception as exc:  # pragma: no cover - defensive, Valkey I/O failure
            logger.warning("PlacementEngine: affinity record failed: %s", exc)

    # -- lazy pull -------------------------------------------------------------

    async def ensure_placed(
        self, model: str, backends: list[InferenceFleetBackend]
    ) -> list[Endpoint]:
        """Lazy-pull a cold model: if no backend currently serves it, place it on one.

        Returns the (possibly still empty, if every backend's ``place_model``
        raised) endpoint list after the attempt. ``register_and_route``
        backends (EXO, admin-registered cloud endpoints WaddleAI doesn't
        lifecycle) are skipped -- WaddleAI cannot push a placement onto a
        backend it doesn't manage the lifecycle of.
        """
        existing = await self.endpoints_for(model, backends)
        if existing:
            return existing

        for backend in backends:
            if backend.management_scope != ManagementScope.FULL_LIFECYCLE:
                continue
            try:
                await backend.place_model(model, {"lazy": True})
            except Exception as exc:
                logger.warning(
                    "PlacementEngine: lazy pull of %r failed on backend_id=%s: %s",
                    model,
                    getattr(backend, "fleet_backend_id", "?"),
                    exc,
                )
                continue
            return await self.endpoints_for(model, backends)

        return []

    # -- place_model: origin deny-list + caps + capacity, then delegate ----

    async def place_model(
        self,
        org_id: int,
        model: str,
        constraints: dict[str, Any],
        backend: InferenceFleetBackend,
        registry_entry: ModelRegistryEntry | None = None,
    ) -> ModelPlacement:
        """Validate a placement request, then delegate to ``backend.place_model``.

        Args:
            org_id: The requesting organization -- scopes the Free-tier
                model-cap check (a fresh ``CapEnforcer`` is built for this
                call, see the class docstring for why it isn't bound at
                construction).
            model: The model to place (provider prefix, if any, is stripped
                for registry/cap lookups but passed through unstripped to
                the backend, matching the rest of ``shared.routing``'s
                convention of stripping exactly once).
            constraints: Backend-specific placement constraints, passed
                through unmodified except a resolved ``node_id`` may be
                added when a VRAM-capacity pick was needed.
            backend: The already-selected backend to place on.
            registry_entry: The model's ``model_registry`` row, pre-fetched
                by the caller (avoids this module owning yet another DB
                query path); ``None`` is treated as "not in the registry" --
                origin/utility/VRAM checks are skipped and the placement is
                allowed to proceed (an unregistered model is a registration
                gap, not something this call should silently deny).

        Returns:
            ``ModelPlacement(status="denied")`` for an origin-deny-listed
            model -- ``backend.place_model`` is never called in that case.

        Raises:
            CapExceededError: The org's Free-tier registered-model cap would be
                exceeded.
            InsufficientCapacityError: No node reachable via ``backend`` has
                enough free VRAM for the registry's ``min_vram``.

        """
        bare_model = _bare_model_name(model)

        if registry_entry is not None and is_denied_origin(registry_entry.origin):
            logger.warning(
                "PlacementEngine: place_model denied for %r (origin=%r, §2.2 deny-list)",
                model,
                registry_entry.origin,
            )
            return ModelPlacement(model=model, node_id="", status="denied")

        if registry_entry is None or not registry_entry.is_utility:
            await self._cap_enforcer(org_id).enforce_model_cap(bare_model)

        min_vram_gb = registry_entry.min_vram if registry_entry is not None else None
        resolved_constraints = constraints
        if min_vram_gb is not None and "node_id" not in constraints:
            nodes: list[NodeInfo] = await backend.list_nodes()
            capable = select_capable_node(nodes, min_vram_gb)
            if capable is None:
                raise InsufficientCapacityError(
                    f"No fleet node has >= {min_vram_gb}GB free VRAM for model {model!r}"
                )
            resolved_constraints = {**constraints, "node_id": capable.node_id}

        return await backend.place_model(model, resolved_constraints)

    # -- capability/offer machinery integration (spec §7.6) -----------------

    async def annotate_offers(
        self, offers: list[ModelOffer], backends: list[InferenceFleetBackend]
    ) -> list[ModelOffer]:
        """Mark local offers unavailable when no fleet endpoint currently serves them.

        This is the "feed the engine's existing capability/offer machinery"
        integration point (spec §10.4/§7.6): it does not re-score or
        re-rank candidates -- it only corrects ``ModelOffer.available`` for
        ``location="local"`` offers against live fleet state, so
        ``shared.routing.capability.veto_and_reroute`` (already wired into
        ``RoutingEngine.decide``) vetoes/reroutes away from a local model
        with zero current capacity exactly as it does for any other
        unavailable offer, with no duplicated veto logic here.

        Commercial offers are returned unchanged.
        """
        annotated: list[ModelOffer] = []
        for offer in offers:
            if offer.location != "local":
                annotated.append(offer)
                continue
            endpoints = await self.endpoints_for(offer.model_name, backends)
            has_healthy = any(e.healthy for e in endpoints)
            if has_healthy == offer.available:
                annotated.append(offer)
            else:
                annotated.append(
                    ModelOffer(
                        model_name=offer.model_name,
                        capability_score=offer.capability_score,
                        supports_tools=offer.supports_tools,
                        supports_vision=offer.supports_vision,
                        context_window=offer.context_window,
                        cost_per_token=offer.cost_per_token,
                        location=offer.location,
                        available=has_healthy,
                    )
                )
        return annotated
