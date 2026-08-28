"""Tests for ModelAccessPolicyResolver: precedence, glob/exact matching, no-op paths."""

from __future__ import annotations

from shared.security.model_access import (
    ModelAccessDecision,
    ModelAccessPolicyResolver,
    ModelAccessPolicyRow,
)


def _row(
    id: int,  # noqa: A002 -- mirrors the DB column name, matches the module's field name
    scope_type: str,
    scope_ref: str | None,
    model_pattern: str,
    action: str = "reject",
    fallback_model: str | None = None,
    reason: str | None = None,
    enabled: bool = True,
) -> ModelAccessPolicyRow:
    """Build a `ModelAccessPolicyRow` fixture with sensible defaults for the field under test."""
    return ModelAccessPolicyRow(
        id=id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        model_pattern=model_pattern,
        action=action,
        fallback_model=fallback_model,
        reason=reason,
        enabled=enabled,
    )


class TestNoMatch:
    """Absence of any matching row is allowed by default -- deny-by-policy only."""

    def test_no_policies_at_all(self) -> None:
        """An empty policy set always resolves to allowed."""
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1, user_id=None, key_id=None, requested_model="gpt-4o", policies=[]
        )
        assert decision == ModelAccessDecision(allowed=True)

    def test_policies_exist_but_none_match_the_pattern(self) -> None:
        """Rows for other patterns never affect an unrelated requested model."""
        policies = [_row(1, "org", "1", "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1, user_id=None, key_id=None, requested_model="gpt-4o", policies=policies
        )
        assert decision.allowed is True

    def test_matching_pattern_but_wrong_org_scope_ref(self) -> None:
        """A deny scoped to a different org never applies to this request."""
        policies = [_row(1, "org", "99", "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5-20260501",
            policies=policies,
        )
        assert decision.allowed is True

    def test_disabled_row_never_matches(self) -> None:
        """A disabled row is inert, even when its scope and pattern both match."""
        policies = [_row(1, "org", "1", "claude-opus-5*", enabled=False)]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5-20260501",
            policies=policies,
        )
        assert decision.allowed is True


class TestExactAndGlobMatching:
    """fnmatch-based pattern matching: exact ids and glob wildcards."""

    def test_exact_pattern_matches_exact_model(self) -> None:
        """An exact-id pattern matches the identical requested model."""
        policies = [_row(1, "global", None, "claude-opus-5-20260501")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5-20260501",
            policies=policies,
        )
        assert decision.allowed is False
        assert decision.matched_policy == 1

    def test_exact_pattern_does_not_match_a_different_model(self) -> None:
        """An exact-id pattern never matches a differently-named model."""
        policies = [_row(1, "global", None, "claude-opus-5-20260501")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-4.8",
            policies=policies,
        )
        assert decision.allowed is True

    def test_glob_matches_opus_5_variant(self) -> None:
        """A glob pattern matches any model id it expands over."""
        policies = [_row(1, "global", None, "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is False

    def test_glob_does_not_match_opus_4_8(self) -> None:
        """The concrete use case: block opus-5.x, keep opus-4.8 workers unaffected."""
        policies = [_row(1, "global", None, "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-4.8",
            policies=policies,
        )
        assert decision.allowed is True


class TestScopePrecedence:
    """key > user > org > global -- narrowest matching scope wins outright."""

    def test_org_deny_applies_when_no_narrower_scope_has_a_row(self) -> None:
        """An org-level deny applies when no narrower scope has a matching row."""
        policies = [_row(1, "org", "1", "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=42,
            key_id=99,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is False
        assert decision.matched_policy == 1

    def test_user_deny_wins_over_org_deny_for_the_same_pattern(self) -> None:
        """A narrower user-scoped row overrides a broader org-scoped row for the same pattern."""
        policies = [
            _row(1, "org", "1", "claude-opus-5*", action="reject", reason="org-wide block"),
            _row(2, "user", "42", "claude-opus-5*", action="reroute", fallback_model="gpt-4o"),
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=42,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.matched_policy == 2
        assert decision.action == "reroute"
        assert decision.fallback_model == "gpt-4o"

    def test_key_deny_wins_over_user_and_org_deny(self) -> None:
        """The narrowest scope (key) wins over both user- and org-scoped rows."""
        policies = [
            _row(1, "org", "1", "claude-opus-5*"),
            _row(2, "user", "42", "claude-opus-5*"),
            _row(3, "key", "99", "claude-opus-5*", reason="pilot key locked to 4.8"),
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=42,
            key_id=99,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.matched_policy == 3
        assert decision.reason == "pilot key locked to 4.8"

    def test_narrower_scope_with_no_matching_pattern_falls_through_to_org(self) -> None:
        """A key-level row exists but for a different pattern -- org's deny still applies."""
        policies = [
            _row(1, "org", "1", "claude-opus-5*"),
            _row(2, "key", "99", "gemini-*"),
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=99,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is False
        assert decision.matched_policy == 1

    def test_global_deny_applies_when_no_org_user_key_row_exists(self) -> None:
        """A global deny applies when no narrower scope has any row at all."""
        policies = [_row(1, "global", None, "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=42,
            key_id=99,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is False
        assert decision.matched_policy == 1


class TestSameScopeTieBreak:
    """Multiple matching rows at the same scope: exact beats glob, then highest id."""

    def test_exact_pattern_beats_glob_at_the_same_scope(self) -> None:
        """An exact-id row wins over a glob row at the same scope, even with a lower id."""
        policies = [
            _row(1, "org", "1", "claude-opus-5*", action="reject"),
            _row(2, "org", "1", "claude-opus-5.1", action="reroute", fallback_model="gpt-4o"),
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.matched_policy == 2
        assert decision.action == "reroute"

    def test_highest_id_wins_among_equally_specific_globs(self) -> None:
        """Among equally-specific glob rows, the highest id (newest) wins."""
        policies = [
            _row(1, "org", "1", "claude-opus-5*", reason="older rule"),
            _row(2, "org", "1", "claude-opus-*", reason="newer rule"),
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.matched_policy == 2
        assert decision.reason == "newer rule"


class TestTeamScopeDeferred:
    """team_id is accepted (JWT teams claim forward-compat) but never matched."""

    def test_team_id_is_accepted_and_ignored(self) -> None:
        """Passing team_id does not change resolution -- it is accepted, not consulted."""
        policies = [_row(1, "org", "1", "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            team_id="some-team",
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is False
        assert decision.matched_policy == 1

    def test_a_row_scoped_to_team_never_matches_anything(self) -> None:
        """No 'team' entry in _SCOPE_ORDER -- a stray team-scoped row is inert, not an error."""
        policies = [_row(1, "team", "some-team", "claude-opus-5*")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.allowed is True


class TestRerouteAction:
    """action='reroute' carries a fallback_model; action='reject' never does."""

    def test_reject_decision_has_no_fallback_model(self) -> None:
        """A reject-action decision never carries a fallback_model."""
        policies = [_row(1, "global", None, "claude-opus-5*", action="reject")]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.action == "reject"
        assert decision.fallback_model is None

    def test_reroute_decision_carries_fallback_model_and_reason(self) -> None:
        """A reroute-action decision carries both the fallback_model and the policy's reason."""
        policies = [
            _row(
                1,
                "global",
                None,
                "claude-opus-5*",
                action="reroute",
                fallback_model="claude-opus-4.8",
                reason="customer prefers 4.8",
            )
        ]
        decision = ModelAccessPolicyResolver.resolve(
            org_id=1,
            user_id=None,
            key_id=None,
            requested_model="claude-opus-5.1",
            policies=policies,
        )
        assert decision.action == "reroute"
        assert decision.fallback_model == "claude-opus-4.8"
        assert decision.reason == "customer prefers 4.8"
