"""Cascade stage 1 heuristic-rule property tests (spec §7.2, §7.7)."""

from shared.routing.heuristics import HeuristicRule, RequestSignals, evaluate_rules, rule_matches


def _rule(priority, match, action):
    return HeuristicRule(priority=priority, match=match, action=action)


class TestRuleMatches:
    """rule_matches() predicate evaluation."""

    def test_tool_name_present_matches(self):
        """A rule matching on tool_name_present fires when the tool is present."""
        signals = RequestSignals(tool_names=["search", "calculator"])
        assert rule_matches({"tool_name_present": ["search"]}, signals) is True

    def test_tool_name_absent_does_not_match(self):
        """The rule does not fire when none of the listed tools are present."""
        signals = RequestSignals(tool_names=["calculator"])
        assert rule_matches({"tool_name_present": ["search"]}, signals) is False

    def test_endpoint_predicate_matches_exact(self):
        """Endpoint predicate matches on exact string equality."""
        signals = RequestSignals(endpoint="/v1/messages")
        assert rule_matches({"endpoint": "/v1/messages"}, signals) is True
        assert rule_matches({"endpoint": "/v1/chat/completions"}, signals) is False

    def test_has_image_predicate_matches(self):
        """has_image predicate matches boolean signal."""
        signals = RequestSignals(has_image=True)
        assert rule_matches({"has_image": True}, signals) is True
        assert rule_matches({"has_image": False}, signals) is False

    def test_multiple_predicates_are_all_required(self):
        """All keys in a compound match dict must hold (AND semantics)."""
        signals = RequestSignals(endpoint="/v1/messages", has_image=True)
        assert rule_matches({"endpoint": "/v1/messages", "has_image": True}, signals) is True
        assert rule_matches({"endpoint": "/v1/messages", "has_image": False}, signals) is False

    def test_empty_match_never_fires(self):
        """An empty match dict never matches (would otherwise fire on everything)."""
        assert rule_matches({}, RequestSignals()) is False

    def test_malformed_match_value_is_skipped_not_raised(self):
        """A wrong-typed match value fails that predicate instead of raising."""
        signals = RequestSignals(tool_names=["search"])
        # tool_name_present expects a list; a string here is malformed.
        assert rule_matches({"tool_name_present": "search"}, signals) is False

    def test_unknown_predicate_key_is_ignored(self):
        """An unrecognized predicate key doesn't block the rule (forward compat)."""
        signals = RequestSignals(endpoint="/v1/messages")
        assert rule_matches({"endpoint": "/v1/messages", "future_key": "x"}, signals) is True


class TestEvaluateRules:
    """evaluate_rules() -- priority order, first match wins, punt on no match."""

    def test_rules_evaluated_in_priority_order_first_match_fires(self):
        """Lower priority number wins when multiple rules would match."""
        signals = RequestSignals(endpoint="/v1/messages")
        rules = [
            _rule(50, {"endpoint": "/v1/messages"}, {"tool_type": "high-prio"}),
            _rule(10, {"endpoint": "/v1/messages"}, {"tool_type": "higher-prio"}),
        ]
        assert evaluate_rules(signals, rules) == {"tool_type": "higher-prio"}

    def test_non_matching_request_punts_to_classifier(self):
        """No rule matching returns None (stage 2 handles it)."""
        signals = RequestSignals(endpoint="/v1/chat/completions")
        rules = [_rule(10, {"endpoint": "/v1/messages"}, {"tool_type": "x"})]
        assert evaluate_rules(signals, rules) is None

    def test_empty_rule_table_punts(self):
        """No rules configured at all returns None."""
        assert evaluate_rules(RequestSignals(), []) is None

    def test_malformed_rule_is_skipped_not_crashing(self):
        """A rule with a bad match shape is skipped; evaluation continues."""
        signals = RequestSignals(endpoint="/v1/messages")
        rules = [
            _rule(1, {"tool_name_present": "not-a-list"}, {"tool_type": "bad"}),
            _rule(2, {"endpoint": "/v1/messages"}, {"tool_type": "good"}),
        ]
        assert evaluate_rules(signals, rules) == {"tool_type": "good"}

    def test_route_action_shape_is_returned_verbatim(self):
        """The action dict is returned as-is, whatever shape it carries."""
        signals = RequestSignals(has_image=True)
        rules = [_rule(1, {"has_image": True}, {"route": "vision-pool"})]
        assert evaluate_rules(signals, rules) == {"route": "vision-pool"}
