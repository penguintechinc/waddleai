"""Candidate-offer loading from ``model_configs`` (interim, spec §7.1).

Builds the ``ModelOffer`` universe RoutingEngine matches capability
requirements against. This is the canonical, reusable implementation of the
interim capability source documented on :class:`shared.routing.capability.ModelOffer`:
location is inferred from ``preferred_providers`` and cost from the mean of
the per-provider ``cost_per_token`` map. Migration 008 (``model_registry``)
has since landed on this branch, but replacing ``model_configs`` as the
capability source is a larger, separate change than fleet §10.4 placement
scope covers -- what fleet Tasks 6/7 add here is narrower: when a
``shared.fleet.placement.PlacementEngine`` (and the org's registered fleet
backends) are supplied, every ``location="local"`` offer's ``available``
flag is corrected against live fleet endpoint state before being returned,
so ``shared.routing.capability.veto_and_reroute`` (already wired into
``RoutingEngine.decide``) vetoes/reroutes away from a local model with zero
current capacity exactly as it would for any other unavailable offer.

``proxy.apps.proxy_server.pipeline.stages.RoutingStage._load_offers``
currently carries its own copy of this same logic rather than importing it,
because ``stages.py`` is a heavily contended file shared by several
in-flight branches and its existing method body must not be edited
non-additively. Once that contention clears, RoutingStage should delegate to
this function instead of duplicating it.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from shared.routing.capability import ModelOffer

if TYPE_CHECKING:
    from shared.fleet.base import InferenceFleetBackend
    from shared.fleet.placement import PlacementEngine

_LOCAL_PROVIDERS = frozenset({"ollama", "llamacpp"})


async def load_offers_from_model_configs(
    db: Any,
    placement: "PlacementEngine | None" = None,
    backends: "list[InferenceFleetBackend] | None" = None,
) -> list[ModelOffer]:
    """Build the candidate ``ModelOffer`` universe from ``model_configs``.

    Args:
        db: penguin-dal DB instance exposing an enabled-filterable
            ``model_configs`` table.
        placement: Optional fleet ``PlacementEngine`` (spec §10.4). When
            given together with ``backends``, local offers are annotated
            against live fleet endpoint state (see module docstring);
            omitted, behavior is byte-identical to before fleet placement
            existed.
        backends: The org's registered ``InferenceFleetBackend`` instances,
            required (and only consulted) when ``placement`` is given.

    Returns:
        One ``ModelOffer`` per enabled ``model_configs`` row.
    """
    rows = await asyncio.to_thread(
        lambda: db(db.model_configs.enabled == True).select()  # noqa: E712
    )
    offers: list[ModelOffer] = []
    for row in rows:
        providers = row.preferred_providers or []
        is_local = any(p in _LOCAL_PROVIDERS for p in providers)
        location = "local" if is_local else "commercial"
        costs = list((row.cost_per_token or {}).values())
        avg_cost = sum(costs) / len(costs) if costs else 0.0
        capabilities = row.capabilities or []
        offers.append(
            ModelOffer(
                model_name=row.model_name,
                context_window=row.context_length or 4096,
                cost_per_token=avg_cost,
                location=location,
                supports_tools=True,
                supports_vision="vision" in capabilities,
            )
        )
    if placement is not None:
        offers = await placement.annotate_offers(offers, backends or [])
    return offers
