"""RouteDecision.clamp_local field tests (failover spec §5.1)."""

from __future__ import annotations

from shared.routing.engine import RouteDecision


def test_route_decision_has_clamp_local_default_false():
    """Default RouteDecision leaves clamp_local False -- flag-off byte-identical."""
    d = RouteDecision(model="gpt-4")
    assert d.clamp_local is False


def test_clamp_local_can_be_set():
    """clamp_local is settable via the constructor for the reshaped-chain case."""
    d = RouteDecision(model="ollama:llama3", clamp_local=True)
    assert d.clamp_local is True
