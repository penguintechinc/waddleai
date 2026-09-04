"""RouteDecision.clamp_local field tests (failover spec §5.1)."""

from __future__ import annotations

import pytest

from shared.routing.capability import ModelOffer
from shared.routing.engine import RouteDecision, RoutingEngine, RoutingInput


def test_route_decision_has_clamp_local_default_false():
    """Default RouteDecision leaves clamp_local False -- flag-off byte-identical."""
    d = RouteDecision(model="gpt-4")
    assert d.clamp_local is False


def test_clamp_local_can_be_set():
    """clamp_local is settable via the constructor for the reshaped-chain case."""
    d = RouteDecision(model="ollama:llama3", clamp_local=True)
    assert d.clamp_local is True


def _body(text="hello"):
    """Build a minimal chat body with a single user message."""
    return {"messages": [{"role": "user", "content": text}]}


def _offer(name, location="local", score=3.0):
    """Build a ModelOffer with sane capability/context defaults for these tests."""
    return ModelOffer(
        model_name=name, location=location, capability_score=score, context_window=200000
    )


class TestRoutingEngineClampLocalComputation:
    """decide()'s clamp_local computation.

    Reflects whether a clamp source actually narrowed the chain to
    local-only providers, not merely that the sensitivity block ran.
    """

    @pytest.mark.asyncio
    async def test_pii_with_org_policy_forcing_local_sets_clamp_local_true(self, fake_db):
        """PII detected + default (local_only) org policy narrows the chain -- True."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-model"), _offer("commercial-model", location="commercial")]
        request = RoutingInput(
            org_id=1,
            request_id="req-clamp-a",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            pii_detected=True,
        )

        decision = await engine.decide(request)

        assert decision.clamp_local is True
        assert "commercial-model" not in decision.fallback_chain

    @pytest.mark.asyncio
    async def test_budget_pressure_clamp_sets_clamp_local_true(self, fake_db):
        """Budget-pressure clamp_local (no PII) narrows the chain -- True."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-model"), _offer("commercial-model", location="commercial")]
        request = RoutingInput(
            org_id=2,
            request_id="req-clamp-b",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            token_consumed_fraction=0.96,
        )

        decision = await engine.decide(request)

        assert decision.clamp_local is True
        assert "commercial-model" not in decision.fallback_chain

    @pytest.mark.asyncio
    async def test_no_pii_no_pressure_leaves_clamp_local_false(self, fake_db):
        """Neither clamp source triggers -- False, chain unchanged."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-model"), _offer("commercial-model", location="commercial")]
        request = RoutingInput(
            org_id=3,
            request_id="req-clamp-c",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
        )

        decision = await engine.decide(request)

        assert decision.clamp_local is False
        assert "commercial-model" in decision.fallback_chain

    @pytest.mark.asyncio
    async def test_pii_with_ignore_policy_leaves_clamp_local_false(self, fake_db):
        """SECURITY regression: an org that opted out of PII clamping.

        An org with ``sensitivity_routing="ignore"`` must never get
        clamp_local=True -- the chain is left unchanged.
        """
        fake_db.seed("routing_policies", [{"organization_id": 4, "sensitivity_routing": "ignore"}])
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-model"), _offer("commercial-model", location="commercial")]
        request = RoutingInput(
            org_id=4,
            request_id="req-clamp-d",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            pii_detected=True,
        )

        decision = await engine.decide(request)

        assert decision.clamp_local is False
        assert "commercial-model" in decision.fallback_chain

    @pytest.mark.asyncio
    async def test_pii_with_redact_then_any_policy_leaves_clamp_local_false(self, fake_db):
        """redact_then_any keeps the full chain.

        Redaction happens at dispatch, not routing -- clamp_local stays False.
        """
        fake_db.seed(
            "routing_policies", [{"organization_id": 5, "sensitivity_routing": "redact_then_any"}]
        )
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-model"), _offer("commercial-model", location="commercial")]
        request = RoutingInput(
            org_id=5,
            request_id="req-clamp-e",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            pii_detected=True,
        )

        decision = await engine.decide(request)

        assert decision.clamp_local is False
        assert "commercial-model" in decision.fallback_chain
