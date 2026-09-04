"""PipelineContext failover fields + RoutingStage clamp/pin wiring (failover spec §5)."""

from __future__ import annotations

import pytest

from proxy.apps.proxy_server.pipeline.stages import PipelineContext, RoutingStage


def test_pipeline_context_new_fields_default_noop():
    """New failover fields default to the flag-off/no-op value."""
    ctx = PipelineContext(user=object(), body={})
    assert ctx.local_only is False
    assert ctx.provider_pin is None
    assert ctx.bytes_flushed is False
    assert ctx.destination is None


@pytest.mark.asyncio
async def test_routing_stage_copies_clamp_local_and_pin(monkeypatch):
    """RoutingStage copies clamp_local to local_only and derives provider_pin from the model."""
    # A minimal RoutingStage whose engine returns a clamp_local decision and whose
    # requested model carries an ollama pin.
    from shared.routing.engine import RouteDecision

    class _Engine:
        async def decide(self, routing_input):
            return RouteDecision(model="llama3", fallback_chain=[], clamp_local=True)

    stage = RoutingStage.__new__(RoutingStage)
    stage.engine = _Engine()
    stage.rules = []
    stage.db = None
    stage.placement = None
    stage.backends_provider = None

    async def _offers(org_id=None):
        return []

    stage._load_offers = _offers  # type: ignore[assignment]

    ctx = PipelineContext(user=object(), body={"messages": []}, model="ollama:llama3")
    ctx.requested_model = "ollama:llama3"
    out = await stage(ctx)
    assert out.local_only is True
    assert out.provider_pin == "ollama"
