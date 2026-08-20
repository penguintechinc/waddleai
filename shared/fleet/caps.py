"""Free-tier fleet caps, VRAM capacity admission, and managed-node metering.

Implements the two count-based §2.4/§10.4 enforcement points -- *physical
node* (distinct K8s node UID, external nodes by registered endpoint) and
*registered model* (distinct ``model_registry`` entry with an active
placement, utility models excluded) -- plus a VRAM-capacity admission check
(``model_registry.min_vram`` vs. a candidate node's ``vram_free_mb``, both
now available now that migration 008 has landed on this branch) used by
``shared.fleet.placement`` before it ever calls a backend's ``place_model``.
``count_managed_nodes`` additionally feeds the §14.6 ``keepalive({"nodes":
M})`` Pro-metering checkin -- the same distinct-node counting rule, just
without a tier ceiling.

Every check is a no-op when ``waddleai.fleet_v2`` is off (the legacy
single-backend deployment path is unaffected) and fails safe to the
``community`` tier if the license client errors -- caps must never be
silently bypassed by a license-server outage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from shared.fleet.base import NodeInfo
from shared.utils.feature_flags import is_feature_enabled

logger = logging.getLogger(__name__)

_FLAG_KEY = "waddleai.fleet_v2"
_COMMUNITY_TIER = "community"
_FREE_NODE_LIMIT = 5
_FREE_MODEL_LIMIT = 3

# model_registry.min_vram is stored in GB (see its column docstring in
# models_sqlalchemy.py); NodeInfo.vram_free_mb is MB -- this is the single
# conversion point between the two units.
_MB_PER_GB = 1024


class CapExceededError(Exception):
    """Raised when a Free-tier node or registered-model cap is exceeded."""


class InsufficientCapacityError(Exception):
    """Raised when no fleet node has enough free VRAM for a model."""


@dataclass(slots=True)
class TierLimits:
    """Resolved node/model ceilings for one license tier (``None`` = unlimited)."""

    tier: str
    node_limit: int | None
    model_limit: int | None


def _limits_for_tier(tier: str) -> TierLimits:
    """Free/`community` is capped per §2.4; Professional/Enterprise are unlimited."""
    if tier == _COMMUNITY_TIER:
        return TierLimits(tier=tier, node_limit=_FREE_NODE_LIMIT, model_limit=_FREE_MODEL_LIMIT)
    return TierLimits(tier=tier, node_limit=None, model_limit=None)


def fits_capacity(node: NodeInfo, min_vram_gb: int | None) -> bool:
    """True when ``node`` has enough free VRAM for a ``min_vram_gb`` requirement.

    ``min_vram_gb`` of ``None`` (the registry column is nullable and
    operator-adjustable) always fits -- unset means "unknown," not "zero,"
    and must never block placement.
    """
    if min_vram_gb is None:
        return True
    return node.vram_free_mb >= min_vram_gb * _MB_PER_GB


def select_capable_node(nodes: list[NodeInfo], min_vram_gb: int | None) -> NodeInfo | None:
    """Return the first healthy node with enough free VRAM, or None if none qualify."""
    for node in nodes:
        if node.healthy and fits_capacity(node, min_vram_gb):
            return node
    return None


def count_managed_nodes(nodes_by_backend: list[list[NodeInfo]]) -> int:
    """Sum distinct nodes across every backend's ``list_nodes()`` result.

    Distinctness is keyed on ``node_uid`` (K8s nodes); external/cloud nodes
    with no stable UID fall back to ``kind:node_id`` (Q#7: "external nodes
    counted by registered endpoint"). Feeds both ``enforce_node_cap`` (the
    caller passes this count in) and the §14.6 Pro metering checkin
    (``keepalive({"nodes": M})``) -- the same distinct-node rule serves both,
    a cap ceiling being only the community-tier case of the same count.
    """
    seen: set[str] = set()
    for nodes in nodes_by_backend:
        for node in nodes:
            seen.add(node.node_uid or f"{node.kind}:{node.node_id}")
    return len(seen)


class CapEnforcer:
    """Enforces §2.4 Free-tier node/model caps for one organization.

    Args:
        db: penguin-dal DB instance exposing ``fleet_backends``,
            ``ollama_deployments``, ``ollama_models``, ``llamacpp_deployments``,
            and ``model_registry``.
        license_client: A ``penguin_licensing.LicenseClient`` (or any object
            exposing a synchronous ``validate()`` returning a ``.tier``
            attribute) -- calls are offloaded via ``asyncio.to_thread``.
        org_id: The organization these checks are scoped to.
    """

    def __init__(self, db: Any, license_client: Any, org_id: int) -> None:
        """Bind the org-scoped dependencies -- see the class docstring."""
        self.db = db
        self.license_client = license_client
        self.org_id = org_id

    def _flag_enabled(self) -> bool:
        return is_feature_enabled(_FLAG_KEY, distinct_id=str(self.org_id), default=False)

    async def _tier(self) -> str:
        """Resolve the org's license tier, failing safe to ``community`` on any error."""

        def _validate() -> str:
            try:
                info = self.license_client.validate()
                return getattr(info, "tier", None) or _COMMUNITY_TIER
            except Exception as exc:  # pragma: no cover - defensive, license I/O failure
                logger.warning(
                    "CapEnforcer: license validate() failed for org=%s, fail-safe to community: %s",
                    self.org_id,
                    exc,
                )
                return _COMMUNITY_TIER

        return await asyncio.to_thread(_validate)

    async def enforce_node_cap(self, count: int) -> None:
        """Raise ``CapExceededError`` when ``count`` exceeds this org's tier node ceiling.

        ``count`` is the prospective distinct-node count (typically
        ``count_managed_nodes(...)`` after including the node about to be
        added) -- this method only compares it against the tier limit, it
        does not compute it itself.
        """
        if not self._flag_enabled():
            return
        limits = _limits_for_tier(await self._tier())
        if limits.node_limit is not None and count > limits.node_limit:
            raise CapExceededError(f"Free tier limited to {limits.node_limit} inference nodes")

    async def enforce_model_cap(self, model: str) -> None:
        """Raise ``CapExceededError`` when placing ``model`` would exceed the model cap.

        A no-op for models already counted (re-placing an existing model
        never grows the set) and for utility models (routing classifier /
        security auditor / embeddings, ``model_registry.is_utility=True``),
        per §2.4 Q#7.
        """
        if not self._flag_enabled():
            return
        limits = _limits_for_tier(await self._tier())
        if limits.model_limit is None:
            return

        def _check() -> None:
            if self._is_utility_model_sync(model):
                return
            registered = self._exclude_utility_sync(self._placed_model_names_sync())
            if model in registered:
                return
            if len(registered) >= limits.model_limit:  # type: ignore[operator]
                raise CapExceededError(
                    f"Free tier limited to {limits.model_limit} registered models"
                )

        await asyncio.to_thread(_check)

    async def min_vram_for(self, model: str) -> int | None:
        """Return ``model_registry.min_vram`` (GB) for ``model``, or None if unregistered/unset."""

        def _lookup() -> int | None:
            row = self.db(self.db.model_registry.name == model).select().first()
            return row.min_vram if row is not None else None

        return await asyncio.to_thread(_lookup)

    async def is_utility_model(self, model: str) -> bool:
        """True when ``model`` is a ``model_registry`` row with ``is_utility=True``."""
        return await asyncio.to_thread(self._is_utility_model_sync, model)

    # -- synchronous DB helpers (always called via asyncio.to_thread) -------

    def _is_utility_model_sync(self, model: str) -> bool:
        row = self.db(self.db.model_registry.name == model).select().first()
        return bool(row.is_utility) if row is not None else False

    def _placed_model_names_sync(self) -> set[str]:
        """Distinct model names currently placed on this org's Ollama/llama.cpp fleet.

        Cloud (Vertex/Bedrock) and EXO backends have no persisted per-model
        row to query here -- their placements are only known live, via
        ``endpoints_for``/``list_nodes`` -- so this covers the two
        DB-tracked local backends, which is where the Free-tier caps apply
        (§2.4: Free deployment targets are "K8s/local only").
        """
        db = self.db
        backend_ids = {r.id for r in db(db.fleet_backends.org_id == self.org_id).select()}
        if not backend_ids:
            return set()

        names: set[str] = set()

        deployment_ids = {
            r.id
            for r in db(db.ollama_deployments.fleet_backend_id.belongs(backend_ids)).select()
        }
        if deployment_ids:
            for row in db(db.ollama_models.deployment_id.belongs(deployment_ids)).select():
                if row.status in (None, "available"):
                    names.add(row.model_name)

        for row in db(db.llamacpp_deployments.fleet_backend_id.belongs(backend_ids)).select():
            if row.status in (None, "running"):
                names.add(row.model_name)

        return names

    def _exclude_utility_sync(self, names: set[str]) -> set[str]:
        if not names:
            return names
        utility = {
            r.name
            for r in self.db(
                (self.db.model_registry.name.belongs(names))
                & (self.db.model_registry.is_utility == True)  # noqa: E712
            ).select()
        }
        return names - utility
