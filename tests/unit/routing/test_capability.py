"""Capability-veto + re-route + save-time-warning tests (spec §7.1.2, §7.7)."""

from shared.routing.capability import (
    ModelOffer,
    best_candidate,
    qualifies,
    validate_assignment,
    veto_and_reroute,
)
from shared.routing.requirements import RequirementsVector


def _reqs(**overrides):
    base = {
        "min_context": 1000,
        "needs_tools": False,
        "needs_vision": False,
        "structured_output": False,
    }
    base.update(overrides)
    return RequirementsVector(**base)


class TestQualifies:
    """qualifies() hard-requirement predicate."""

    def test_text_only_model_fails_vision_requirement(self):
        """A vision requirement vetoes a text-only offer -- security-relevant correctness."""
        offer = ModelOffer(model_name="text-only", supports_vision=False)
        assert qualifies(offer, _reqs(needs_vision=True)) is False

    def test_context_overflow_fails(self):
        """A too-small context window fails qualification."""
        offer = ModelOffer(model_name="small-ctx", context_window=500)
        assert qualifies(offer, _reqs(min_context=1000)) is False

    def test_tools_required_but_unsupported_fails(self):
        """Tool use requirement vetoes a model without tool support."""
        offer = ModelOffer(model_name="no-tools", supports_tools=False)
        assert qualifies(offer, _reqs(needs_tools=True)) is False

    def test_unavailable_offer_fails(self):
        """A fleet-unavailable offer never qualifies."""
        offer = ModelOffer(model_name="down", available=False)
        assert qualifies(offer, _reqs()) is False

    def test_fully_capable_offer_qualifies(self):
        """An offer meeting every requirement qualifies."""
        offer = ModelOffer(
            model_name="capable", supports_tools=True, supports_vision=True, context_window=200000
        )
        reqs = _reqs(needs_tools=True, needs_vision=True, min_context=1000)
        assert qualifies(offer, reqs) is True


class TestBestCandidate:
    """best_candidate() ranking among qualified offers."""

    def test_returns_highest_capability_score_among_qualified(self):
        """The best-scoring qualified offer wins; unqualified ones are excluded."""
        offers = [
            ModelOffer(model_name="low", capability_score=2.0, context_window=2000),
            ModelOffer(model_name="high", capability_score=4.5, context_window=2000),
            ModelOffer(model_name="too-small", capability_score=5.0, context_window=10),
        ]
        winner = best_candidate(offers, _reqs(min_context=1000))
        assert winner.model_name == "high"

    def test_returns_none_when_nothing_qualifies(self):
        """No qualified candidates yields None (never raises)."""
        offers = [ModelOffer(model_name="small", context_window=10)]
        assert best_candidate(offers, _reqs(min_context=1000)) is None


class TestVetoAndReroute:
    """veto_and_reroute() -- the co-equal veto."""

    def test_qualified_assignment_is_kept(self):
        """A qualified assigned model is kept unchanged, no veto reason."""
        assigned = ModelOffer(model_name="assigned", context_window=200000)
        chosen, reason = veto_and_reroute(assigned, [assigned], _reqs(min_context=1000))
        assert chosen is assigned
        assert reason is None

    def test_image_against_text_only_assignment_is_vetoed_and_rerouted(self):
        """Image request against a text-only assignment: veto + re-route (§7.7 acceptance item)."""
        text_only = ModelOffer(model_name="text-only", supports_vision=False, context_window=200000)
        vision_capable = ModelOffer(
            model_name="vision-model",
            supports_vision=True,
            context_window=200000,
            capability_score=4.0,
        )
        chosen, reason = veto_and_reroute(
            text_only, [text_only, vision_capable], _reqs(needs_vision=True, min_context=1000)
        )
        assert chosen.model_name == "vision-model"
        assert reason == "vision_unsupported"

    def test_context_overflow_assignment_is_vetoed(self):
        """A too-small assigned context window is vetoed with context_overflow."""
        small = ModelOffer(model_name="small", context_window=100)
        big = ModelOffer(model_name="big", context_window=200000)
        chosen, reason = veto_and_reroute(small, [small, big], _reqs(min_context=1000))
        assert chosen.model_name == "big"
        assert reason == "context_overflow"

    def test_no_assignment_falls_through_to_capability_matching_alone(self):
        """No assignment row (None) still resolves via capability matching."""
        offer = ModelOffer(model_name="only-option", context_window=200000)
        chosen, reason = veto_and_reroute(None, [offer], _reqs(min_context=1000))
        assert chosen is offer
        assert reason == "no_assignment"

    def test_veto_when_no_candidate_qualifies_returns_none_not_exception(self):
        """When nothing qualifies at all, veto_and_reroute degrades to (None, reason)."""
        bad = ModelOffer(model_name="bad", context_window=10)
        chosen, reason = veto_and_reroute(bad, [bad], _reqs(min_context=1000))
        assert chosen is None
        assert reason == "context_overflow"


class TestValidateAssignment:
    """validate_assignment() save-time warnings for the admin screen."""

    def test_unknown_model_warns(self):
        """A model absent from the registry produces a warning, not an error."""
        warnings = validate_assignment(None)
        assert warnings == ["model is not present in the registry"]

    def test_qualified_offer_has_no_warnings(self):
        """A fully qualified offer produces no warnings."""
        offer = ModelOffer(model_name="ok", context_window=200000)
        assert validate_assignment(offer, _reqs(min_context=1000)) == []

    def test_unavailable_offer_warns(self):
        """An unavailable fleet target is flagged even without a reqs check."""
        offer = ModelOffer(model_name="down", available=False)
        warnings = validate_assignment(offer)
        assert any("unavailable" in w for w in warnings)

    def test_mismatched_requirement_warns_with_reason(self):
        """A hard-requirement mismatch is surfaced with its veto reason."""
        offer = ModelOffer(model_name="text-only", supports_vision=False, context_window=200000)
        warnings = validate_assignment(offer, _reqs(needs_vision=True, min_context=1000))
        assert any("vision_unsupported" in w for w in warnings)
