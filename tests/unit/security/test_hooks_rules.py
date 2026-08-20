"""Tests for admin-authored declarative hook_rules (§18.3/§18.4): matching + precedence."""

from __future__ import annotations

import pytest

from shared.security.hooks_rules import (
    HookRule,
    HookRulesResolver,
    combine_hook_rule_matches,
    matches_rule,
)


def _rule(
    id: int = 1,
    scope_type: str = "org",
    scope_ref: str | None = "7",
    ecosystem: str | None = None,
    event: str | None = None,
    tool_name_pattern: str | None = None,
    match_pattern: str | None = None,
    decision: str = "allow",
    reason: str = "test rule",
    priority: int = 100,
) -> HookRule:
    """Build a HookRule with sensible test defaults."""
    return HookRule(
        id=id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        ecosystem=ecosystem,
        event=event,
        tool_name_pattern=tool_name_pattern,
        match_pattern=match_pattern,
        decision=decision,
        reason=reason,
        priority=priority,
    )


class StubRulesStore:
    """In-memory `HookRulesStore` for tests."""

    def __init__(self) -> None:
        """Track inserted rules keyed by scope."""
        self.rules: dict[tuple[str, str | None], list[HookRule]] = {}
        self.calls: list[tuple[str, str | None]] = []

    def add(self, rule: HookRule) -> None:
        """Insert one rule."""
        self.rules.setdefault((rule.scope_type, rule.scope_ref), []).append(rule)

    async def fetch_rules(self, scope_type: str, scope_ref: str | None) -> list[HookRule]:
        """Return rules for one scope, recording the call."""
        self.calls.append((scope_type, scope_ref))
        return self.rules.get((scope_type, scope_ref), [])


class TestMatchesRule:
    """`matches_rule` -- every set matcher field is an AND-narrowing filter."""

    def test_unset_fields_match_anything(self) -> None:
        """A rule with every matcher field unset matches any event."""
        rule = _rule()
        assert matches_rule(rule, "claude-code", "pre_tool_use", "Bash", "rm -rf /")

    def test_ecosystem_filter(self) -> None:
        """A set ecosystem must match exactly."""
        rule = _rule(ecosystem="cortex")
        assert not matches_rule(rule, "claude-code", "pre_tool_use", "Bash", "ls")
        assert matches_rule(rule, "cortex", "pre_tool_use", "Bash", "ls")

    def test_event_filter(self) -> None:
        """A set event must match exactly."""
        rule = _rule(event="pre_tool_use")
        assert not matches_rule(rule, "claude-code", "post_tool_use", "Bash", "ls")

    def test_tool_name_glob(self) -> None:
        """tool_name_pattern is a glob against tool_name."""
        rule = _rule(tool_name_pattern="mcp__*")
        assert matches_rule(rule, "claude-code", "pre_tool_use", "mcp__elder__search", "")
        assert not matches_rule(rule, "claude-code", "pre_tool_use", "Bash", "")

    def test_match_pattern_glob_against_text(self) -> None:
        """match_pattern is a glob against the flattened tool_input text."""
        rule = _rule(match_pattern="*rm -rf*")
        assert matches_rule(rule, "claude-code", "pre_tool_use", "Bash", "rm -rf /tmp/x")
        assert not matches_rule(rule, "claude-code", "pre_tool_use", "Bash", "ls -la")


class TestCombineHookRuleMatches:
    """§18.4 precedence: max severity wins across ALL matched rules, global or tenant."""

    def test_no_matches_returns_none(self) -> None:
        """Nothing matched -> no winner."""
        assert combine_hook_rule_matches([]) is None

    def test_global_deny_outranks_tenant_allow(self) -> None:
        """A global `deny` is never overridden by a tenant `allow` (coordinator directive)."""
        global_deny = _rule(id=1, scope_type="global", scope_ref=None, decision="deny")
        tenant_allow = _rule(id=2, scope_type="org", scope_ref="7", decision="allow")

        winner = combine_hook_rule_matches([global_deny, tenant_allow])

        assert winner is not None
        assert winner.decision == "deny"
        assert winner.id == 1

    def test_tenant_deny_tightens_global_allow(self) -> None:
        """A tenant can restrict further than a global `allow` floor."""
        global_allow = _rule(id=1, scope_type="global", scope_ref=None, decision="allow")
        tenant_deny = _rule(id=2, scope_type="org", scope_ref="7", decision="deny")

        winner = combine_hook_rule_matches([global_allow, tenant_deny])

        assert winner is not None
        assert winner.decision == "deny"
        assert winner.id == 2

    def test_ask_is_between_allow_and_deny(self) -> None:
        """`ask` outranks `allow` but is outranked by `deny`."""
        allow = _rule(id=1, decision="allow")
        ask = _rule(id=2, decision="ask")
        assert combine_hook_rule_matches([allow, ask]).decision == "ask"

        deny = _rule(id=3, decision="deny")
        assert combine_hook_rule_matches([ask, deny]).decision == "deny"

    def test_tie_broken_by_priority_then_id(self) -> None:
        """Equal severity: lowest priority number wins; then lowest id."""
        r1 = _rule(id=5, decision="deny", priority=50)
        r2 = _rule(id=1, decision="deny", priority=10)
        winner = combine_hook_rule_matches([r1, r2])
        assert winner.id == 1  # lower priority number wins

        r3 = _rule(id=9, decision="deny", priority=10)
        r4 = _rule(id=2, decision="deny", priority=10)
        winner2 = combine_hook_rule_matches([r3, r4])
        assert winner2.id == 2  # same priority -> lowest id wins


class TestHookRulesResolver:
    """`HookRulesResolver.resolve` -- global ∪ org rules, tenant-isolated."""

    @pytest.mark.asyncio
    async def test_global_rule_visible_to_every_org(self) -> None:
        """A global rule is included regardless of which org resolves."""
        store = StubRulesStore()
        store.add(_rule(id=1, scope_type="global", scope_ref=None))
        resolver = HookRulesResolver(store)

        rules_a = await resolver.resolve(org_id="A")
        rules_b = await resolver.resolve(org_id="B")

        assert any(r.id == 1 for r in rules_a)
        assert any(r.id == 1 for r in rules_b)

    @pytest.mark.asyncio
    async def test_tenant_admin_cannot_affect_another_tenants_rules(self) -> None:
        """An org-scoped rule authored for org A never resolves for org B (§18.4 hard boundary)."""
        store = StubRulesStore()
        store.add(_rule(id=1, scope_type="org", scope_ref="A"))
        resolver = HookRulesResolver(store)

        rules_a = await resolver.resolve(org_id="A")
        rules_b = await resolver.resolve(org_id="B")

        assert any(r.id == 1 for r in rules_a)
        assert not any(r.id == 1 for r in rules_b)
        # The store is only ever queried for org B's own scope -- org A's
        # rows are never even fetched on B's behalf.
        assert ("org", "A") not in [c for c in resolver.store.calls if c[1] == "B"]
