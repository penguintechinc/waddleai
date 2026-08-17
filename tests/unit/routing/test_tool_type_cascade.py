"""Tool-type cascade orchestration tests: each stage only runs when the prior punts (spec §7.2)."""

import json

import pytest

from shared.routing.classifier import StubClassifierClient
from shared.routing.heuristics import HeuristicRule, RequestSignals
from shared.routing.tool_type import determine_tool_type


class TestCascadeStagePrecedence:
    """Explicit > heuristic > classifier, each consulted only when the prior punts."""

    @pytest.mark.asyncio
    async def test_explicit_short_circuits_everything(self):
        """An explicit tool type skips heuristics and the classifier entirely."""
        classifier_client = StubClassifierClient()
        rules = [HeuristicRule(priority=1, match={}, action={"tool_type": "should-not-fire"})]

        decision = await determine_tool_type(
            explicit="research",
            signals=RequestSignals(),
            rules=rules,
            classifier_client=classifier_client,
        )

        assert decision.tool_type == "research"
        assert decision.source == "explicit"
        assert classifier_client.call_count == 0

    @pytest.mark.asyncio
    async def test_heuristic_fires_when_no_explicit_signal(self):
        """Stage 1 resolves it without ever consulting the classifier."""
        classifier_client = StubClassifierClient()
        rules = [
            HeuristicRule(priority=1, match={"has_image": True}, action={"tool_type": "vision"})
        ]

        decision = await determine_tool_type(
            explicit=None,
            signals=RequestSignals(has_image=True),
            rules=rules,
            classifier_client=classifier_client,
        )

        assert decision.tool_type == "vision"
        assert decision.source == "heuristic"
        assert classifier_client.call_count == 0

    @pytest.mark.asyncio
    async def test_classifier_runs_only_when_heuristics_punt(self):
        """Stage 2 is consulted only after explicit and heuristics both punt."""
        classifier_client = StubClassifierClient(
            fixed_response=json.dumps({"tool_type": "analysis", "complexity": 3})
        )

        decision = await determine_tool_type(
            explicit=None,
            signals=RequestSignals(endpoint="/v1/chat/completions"),
            rules=[],  # no rules configured -> punt
            prompt_text="analyze this dataset",
            classifier_client=classifier_client,
        )

        assert decision.tool_type == "analysis"
        assert decision.source == "classifier"
        assert decision.classification.complexity == 3
        assert classifier_client.call_count == 1

    @pytest.mark.asyncio
    async def test_no_classifier_client_yields_safe_fallback(self):
        """When no classifier is configured, a safe general fallback is returned."""
        decision = await determine_tool_type(
            explicit=None,
            signals=RequestSignals(),
            rules=[],
            classifier_client=None,
        )
        assert decision.tool_type == "general"
        assert decision.source == "classifier"
