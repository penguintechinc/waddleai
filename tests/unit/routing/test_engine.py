"""RoutingEngine end-to-end composition tests (spec §7, §7.7 acceptance)."""

import pytest

from shared.routing.capability import ModelOffer
from shared.routing.engine import RoutingEngine, RoutingInput


def _body(text="hello", **overrides):
    body = {"messages": [{"role": "user", "content": text}]}
    body.update(overrides)
    return body


def _assignment_row(tool_type, model_name, escalation_model=None):
    return {
        "id": 1,
        "tool_type": tool_type,
        "model_name": model_name,
        "scope": "global",
        "scope_ref": None,
        "escalation_model": escalation_model,
        "fallback_models": None,
        "enabled": True,
    }


def _offer(name, location="local", score=3.0, context_window=200000, **kwargs):
    return ModelOffer(
        model_name=name,
        location=location,
        capability_score=score,
        context_window=context_window,
        **kwargs,
    )


class TestRoutingEngineBasicDecision:
    """decide() composes assignment + capability + policy into a final model."""

    @pytest.mark.asyncio
    async def test_no_assignment_falls_back_to_best_qualified_offer(self, fake_db):
        """With no assignment row, capability matching alone picks the best offer."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-a", score=3.0), _offer("local-b", score=4.5)]
        request = RoutingInput(
            org_id=1,
            request_id="req-1",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
        )

        decision = await engine.decide(request)

        assert decision.model == "local-b"

    @pytest.mark.asyncio
    async def test_assignment_row_is_honored_when_qualified(self, fake_db):
        """A qualified assignment row's model is chosen over higher-scoring alternatives."""
        fake_db.seed("model_assignments", [_assignment_row("research", "assigned-model")])
        engine = RoutingEngine(fake_db)
        offers = [_offer("assigned-model", score=2.0), _offer("higher-score", score=5.0)]
        request = RoutingInput(
            org_id=1, request_id="req-2", body=_body(), explicit_tool_type="research", offers=offers
        )

        decision = await engine.decide(request)

        assert decision.model == "assigned-model"


class TestRoutingEngineCapabilityVeto:
    """The co-equal veto: an assigned model failing a hard requirement re-routes."""

    @pytest.mark.asyncio
    async def test_image_request_against_text_only_assignment_is_rerouted_and_traced(self, fake_db):
        """Image request against a text-only assignment -> re-routed + trace records the veto."""
        fake_db.seed("model_assignments", [_assignment_row("chat", "text-only-model")])
        engine = RoutingEngine(fake_db)
        offers = [
            _offer("text-only-model", supports_vision=False),
            _offer("vision-model", location="commercial", supports_vision=True, score=4.0),
        ]
        image_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
                    ],
                }
            ]
        }
        request = RoutingInput(
            org_id=1, request_id="req-3", body=image_body, explicit_tool_type="chat", offers=offers
        )

        decision = await engine.decide(request)

        assert decision.model == "vision-model"
        assert decision.routed_from == {"cause": "capability_veto", "reason": "vision_unsupported"}
        assert decision.trace.capability_veto is True
        assert decision.trace.veto_reason == "vision_unsupported"


class TestRoutingEngineChaosFailover:
    """Provider unhealthy mid-conversation -> chain failover, no client-visible error."""

    @pytest.mark.asyncio
    async def test_unhealthy_primary_still_yields_a_model_via_fallback_chain(self, fake_db):
        """local_unhealthy triggers escalation to the assignment's escalation_model."""
        fake_db.seed(
            "model_assignments",
            [_assignment_row("chat", "local-primary", escalation_model="commercial-backup")],
        )
        engine = RoutingEngine(fake_db)
        offers = [_offer("local-primary"), _offer("commercial-backup", location="commercial")]
        request = RoutingInput(
            org_id=1,
            request_id="req-4",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            local_unhealthy=True,
        )

        decision = await engine.decide(request)

        assert decision.model == "commercial-backup"
        assert decision.trace.escalated is True

    @pytest.mark.asyncio
    async def test_fallback_chain_is_ordered_and_excludes_the_chosen_model(self, fake_db):
        """fallback_chain lists the remaining qualified candidates, chosen model excluded."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("first", score=5.0), _offer("second", score=3.0)]
        request = RoutingInput(
            org_id=1, request_id="req-5", body=_body(), explicit_tool_type="chat", offers=offers
        )

        decision = await engine.decide(request)

        assert decision.model == "first"
        assert "first" not in decision.fallback_chain
        assert "second" in decision.fallback_chain


class TestRoutingEngineTransparency:
    """routed_from always reflects the actual redirect cause; never silent substitution."""

    @pytest.mark.asyncio
    async def test_no_redirect_leaves_routed_from_none(self, fake_db):
        """When nothing redirected the request, routed_from stays None."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("only-option")]
        request = RoutingInput(
            org_id=1, request_id="req-6", body=_body(), explicit_tool_type="chat", offers=offers
        )

        decision = await engine.decide(request)

        assert decision.routed_from is None

    @pytest.mark.asyncio
    async def test_decision_trace_is_persisted(self, fake_db):
        """Every decide() call persists exactly one decision trace row."""
        engine = RoutingEngine(fake_db)
        offers = [_offer("only-option")]
        request = RoutingInput(
            org_id=9, request_id="req-7", body=_body(), explicit_tool_type="chat", offers=offers
        )

        await engine.decide(request)

        rows = fake_db._tables["routing_decision_traces"]
        assert len(rows) == 1
        assert rows[0]["organization_id"] == 9
        assert rows[0]["final_model"] == "only-option"

    @pytest.mark.asyncio
    async def test_persist_false_writes_no_decision_trace(self, fake_db):
        """persist=False (admin dry-run) computes a real decision but writes zero rows.

        Regression guard for the routing_dry_run admin endpoint: a what-if
        evaluation must never pollute the routing_decision_traces corpus
        (spec §7.4 treats it as first-class training/tuning data).
        """
        fake_db.seed("model_assignments", [_assignment_row("research", "assigned-model")])
        engine = RoutingEngine(fake_db)
        offers = [_offer("assigned-model", score=2.0), _offer("higher-score", score=5.0)]
        request = RoutingInput(
            org_id=9,
            request_id="dryrun-req",
            body=_body(),
            explicit_tool_type="research",
            offers=offers,
        )

        decision = await engine.decide(request, persist=False)

        # Still a genuine, fully-computed decision -- the assignment row is honored.
        assert decision.model == "assigned-model"
        assert decision.trace is not None
        assert decision.trace.final_model == "assigned-model"
        # ...but nothing durable was written (table never even created).
        assert fake_db._tables.get("routing_decision_traces", []) == []


class TestRoutingEngineSensitivityIntegration:
    """PII-flagged requests never dispatch commercial under local_only (security)."""

    @pytest.mark.asyncio
    async def test_pii_flagged_request_excludes_commercial_candidates(self, fake_db):
        """SECURITY: pii_detected=True clamps the chain to local-only candidates."""
        engine = RoutingEngine(fake_db)
        offers = [
            _offer("local-model", score=2.0),
            _offer("commercial-model", location="commercial", score=5.0),
        ]
        request = RoutingInput(
            org_id=1,
            request_id="req-8",
            body=_body(),
            explicit_tool_type="chat",
            offers=offers,
            pii_detected=True,
        )

        decision = await engine.decide(request)

        assert decision.model == "local-model"
        assert "commercial-model" not in decision.fallback_chain
